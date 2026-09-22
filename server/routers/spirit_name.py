# -*- coding: utf-8 -*-
"""地霊が、知っている人に呼び名を聞いて覚える（2026-09-17）。

本人の決定（9/17）：
  - まずはキャラから投げかける、いちばん簡単な形。会話はまだしない
  - 呼び名は何でもよい（本名でなくてよい）
  - 覚えた名前で呼ぶのは、この仕組みが通ってから

流れ：
  1. 顔で誰か分かり、その人を迎える場面になる（spirit.py の迎え）
  2. その人の呼び名がまだ無ければ、迎えの一言の代わりに「なんてよんだらいい？」を鳴らす
     （作り置き ask_name_*）。返事に ask_name=<ID> を付けて、ラズパイに聞く番だと知らせる
  3. ラズパイが C3 に鳴らさせ、鳴り終わるころから数秒だけ Tapo のマイクの音を取り、
     /spirit/name に送る（16kHz・16bit・モノラルの WAV）
  4. ここで文字にし（Google の音声認識）、音は捨てる。文字から呼び名の候補を取り出す（Claude）
  5. すぐには覚えず「◯◯……で、あってる？」と聞き返す（9/21・聞き間違いを残さないため）。
     「うん」なら faces/<ID> に name として覚え「えへへ……◯◯。おぼえた」。
     「ちがう」なら聞き直す。取れなかったときも聞き直す。1回の来訪で3回まで。
     声はその場でクラウドの VOICEVOX で作る。返事の say=true で C3 に鳴らさせ、listen=true でまた聞く
  6. 間違って残ったときや本人の申し出には /spirit/name/clear で消す（次に来たらまた聞く）

残すもの：その人の呼び名と時刻。9/22 から、各往復で聞き取った文字・候補・聞き返しへの答えも
記録（spirit_log の name_heard）に残す（本人決定「記録を残しましょう。了承は得ています」）。音は残さない。
"""
import base64
import io
import json
import logging
import os
import time
import wave

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

from server.database import get_db
from server.keys import key_ok
from server.storage import upload_to
from server.routers import spirit as sp

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/spirit", tags=["spirit"])

# 聞くかどうか。9/19 は切で出した（橋渡し側がそろっておらず、掲示も「音声は記録しない」だった）。
# 9/21：橋渡し側に録音が入り、本人が掲示を v3 に貼り替え、QRの先のページも v3 にしたので、既定を入にした。
# 止めたいときは環境変数 SPIRIT_ASK_NAME=0。ただし deploy.yml は入れ替えのたびに環境変数を
# 書き直す（--set-env-vars）ので、ずっと止めるならここの既定を変える。
ASK_ON = os.environ.get("SPIRIT_ASK_NAME", "1") == "1"
ASK_KIND = "ask_name"            # 作り置きの問いかけ（ask_name_0 など）
ASK_GAP = 6 * 3600.0             # 聞けなかった人に、もう一度聞くまでの間
ASK_TTL = 90.0                   # 問いかけから、これより後に届いた答えは受け取らない
NAME_MAX = 12                    # 呼び名の長さの上限（字）
SILENT_LEVEL = 25                # これより静かなら、声は入っていないとみなす（9/19の実測）
VOICEVOX_URL = os.environ.get("VOICEVOX_URL",
                              "https://voicevox-436112585717.asia-northeast1.run.app")
VOICEVOX_SPEAKER = 3             # ずんだもん（作り置きと同じ声）


# ---- 1. 迎えの場面で、聞くかどうか ----

def maybe_ask(st: dict, pid: str, doc: dict, now: float, alone: bool = True) -> bool:
    """呼び名が無い人なら、迎えの一言の代わりに問いかけを予約する。予約したら True。

    spirit.py の迎えから呼ぶ。たまに黙る（SILENT_CHANCE）は通さない。
    問いかけが鳴らないのに、ラズパイが聞きに行くことになるため。

    alone＝いまの滞在に居るのがこの人だけか（2026-09-22）。何人か居るときは聞かない。
    9/22 21:30、2人居たときに p02 に聞いて「ゆい」を覚えたが、答えたのが p02 本人か
    分からなかった。服で呼びかける作り（誰に聞いているか伝わる）が入るまでの暫定。"""
    if not ASK_ON or not alone or not pid or pid == "unknown" or doc.get("name"):
        return False
    if now - float(doc.get("name_asked_at") or 0) < ASK_GAP:
        return False
    line = sp._pick_line(ASK_KIND)
    if not line:
        return False                       # 作り置きがまだ置かれていない
    st["speak_line"] = line
    st["speak_at"] = now + sp.SPEAK_SLOW   # ためらってから聞く
    st["name_ask"] = {"person": pid, "at": now}
    try:
        get_db().collection("faces").document(pid).update({"name_asked_at": now})
    except Exception as e:
        logger.warning("name ask mark failed: %s", e)
    sp._log_event("name_ask", {"person": pid, "line": line})
    return True


def asking(st: dict, now: float) -> str | None:
    """いま聞いている相手の ID。写真の返事に添えて、ラズパイに知らせる。"""
    a = st.get("name_ask") or {}
    if a.get("person") and now - float(a.get("at") or 0) < ASK_TTL:
        return a["person"]
    return None


# ---- 2. 答えを受け取る ----

def _pcm16k(data: bytes) -> bytes:
    """WAV（16kHz・16bit・モノラル）から音の中身だけを取り出す。"""
    with wave.open(io.BytesIO(data)) as w:
        if w.getframerate() != 16000 or w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError("16kHz・16bit・モノラルの WAV を送ること（%d Hz / %d byte / %d ch）"
                             % (w.getframerate(), w.getsampwidth(), w.getnchannels()))
        return w.readframes(w.getnframes())


def _level(pcm: bytes) -> int:
    """音の大きさ（0〜32767）。静かな部屋は10以下、1mでの話し声は9/19の実測で200〜300。"""
    import array
    import math
    d = array.array("h")
    d.frombytes(pcm[:len(pcm) // 2 * 2])
    if not d:
        return 0
    return int(math.sqrt(sum(x * x for x in d) / len(d)))


def _gcp_token() -> str:
    import google.auth
    import google.auth.transport.requests
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


async def _to_text(pcm: bytes) -> str:
    """音 → 文字（Google Speech-to-Text・日本語）。聞き取れなければ空。"""
    body = {"config": {"encoding": "LINEAR16", "sampleRateHertz": 16000,
                       "languageCode": "ja-JP", "model": "latest_short"},
            "audio": {"content": base64.b64encode(pcm).decode()}}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://speech.googleapis.com/v1/speech:recognize", json=body,
                         headers={"Authorization": "Bearer " + _gcp_token()})
    r.raise_for_status()
    res = r.json().get("results") or []
    return "".join((x.get("alternatives") or [{}])[0].get("transcript", "") for x in res).strip()


_PICK_SYSTEM = """共有キッチンに住む小さな精霊が、そこに来た人に「なんてよんだらいい？」と聞きました。
その人の返事を文字にしたものが届きます（聞き間違いが混ざることがあります）。
頭に精霊自身の問いかけ（「なんてよんだらいい」「なまえおしえて」など）が混ざっていることがあります。それは無視してください。
返事から、その人を呼ぶときの呼び名だけを取り出してください。
- あだ名・名字・下の名前・「〜さん」付きなど、本人が名乗ったものをそのまま使う（敬称は外してよい）
- 呼び名が含まれていない、断っている、聞き取れていない、関係のない話、のときは null
- 呼び名は12字以内
JSONだけで答える：{"name": "けんちゃん"} または {"name": null}"""


async def _pick_name(text: str) -> str | None:
    """返事の文から呼び名だけを取り出す。無ければ None。"""
    if not text or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    from anthropic import AsyncAnthropic
    msg = await AsyncAnthropic().messages.create(
        model=sp.MODEL, max_tokens=60, system=_PICK_SYSTEM,
        messages=[{"role": "user", "content": text}])
    out = "".join(b.text for b in msg.content if b.type == "text")
    i, j = out.find("{"), out.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        name = json.loads(out[i:j + 1]).get("name")
    except Exception:
        return None
    if not isinstance(name, str):
        return None
    name = name.strip()
    return name[:NAME_MAX] or None


# ---- 3. 覚えたと返す ----

async def _voice(text: str) -> bytes:
    """クラウドの VOICEVOX で、16kHz・16bit・モノラルの生PCMにする。"""
    import google.auth.transport.requests
    import google.oauth2.id_token
    tok = google.oauth2.id_token.fetch_id_token(
        google.auth.transport.requests.Request(), VOICEVOX_URL)
    h = {"Authorization": "Bearer " + tok}
    async with httpx.AsyncClient(timeout=60) as c:
        q = await c.post(VOICEVOX_URL + "/audio_query",
                         params={"speaker": VOICEVOX_SPEAKER, "text": text}, headers=h)
        q.raise_for_status()
        query = q.json()
        query["outputSamplingRate"] = 16000   # C3 の I2S に合わせる
        query["outputStereo"] = False
        w = await c.post(VOICEVOX_URL + "/synthesis", params={"speaker": VOICEVOX_SPEAKER},
                         json=query, headers=h)
        w.raise_for_status()
    return _pcm16k(w.content)


# ---- 2. 答えを受け取る（聞き返して確かめる・2026-09-21）----
#
# 本人の希望（9/21）：聞き取り間違いをそのまま覚えない。
#   「へんちゃん……で、あってる？」と聞き返し、「うん」なら覚える。「ちがう」なら覚えずに聞き直す。
# 1回目の実機（9/21 21:17）は答えが取れなかった（聞こえたのは8字＝問いかけの後ろ半分くらい）。
# 取れなかったときも、聞き直す。
#
# やりとりは状態 st["name_ask"] で持つ：
#   phase = "ask"（呼び名を聞いた）／"confirm"（候補を言って、あってるか聞いた）
#   cand  = 確かめている候補、round = 何回目か（ROUND_MAX で打ち切る）
# 返事の listen=true は「もう一度しゃべるので、鳴り終わったらまた聞いて送って」という合図。

ROUND_MAX = 3                    # 1回の来訪で、聞き直すのはここまで

_YESNO_SYSTEM = """共有キッチンに住む小さな精霊が、来た人に「◯◯……で、あってる？」と、呼び名が合っているかを聞きました。
その人の返事を文字にしたものが届きます（聞き間違いが混ざることがあります。頭に精霊自身の問いかけが混ざることもあります。それは無視）。
- 合っている（うん・はい・そう・あってる など）→ "yes"
- 違う（ちがう・いいえ・ううん など）→ "no"。違うと言いながら正しい呼び名を言っていれば、それを name に入れる
- どちらとも取れない・聞き取れていない → "unclear"
JSONだけで答える：{"answer": "yes"} / {"answer": "no", "name": "けんちゃん"} / {"answer": "no", "name": null} / {"answer": "unclear"}"""


async def _yes_no(text: str) -> tuple:
    """聞き返しへの返事 → ("yes"|"no"|"unclear", 言い直した呼び名 or None)"""
    if not text or not os.environ.get("ANTHROPIC_API_KEY"):
        return "unclear", None
    from anthropic import AsyncAnthropic
    msg = await AsyncAnthropic().messages.create(
        model=sp.MODEL, max_tokens=60, system=_YESNO_SYSTEM,
        messages=[{"role": "user", "content": text}])
    out = "".join(b.text for b in msg.content if b.type == "text")
    i, j = out.find("{"), out.rfind("}")
    try:
        d = json.loads(out[i:j + 1]) if 0 <= i < j else {}
    except Exception:
        d = {}
    ans = d.get("answer") if d.get("answer") in ("yes", "no", "unclear") else "unclear"
    name = d.get("name") if isinstance(d.get("name"), str) and d["name"].strip() else None
    return ans, (name.strip()[:NAME_MAX] if name else None)


async def _say(st: dict, person: str, text: str) -> bool:
    """その場で声にして、次に鳴らす声に置く。置けたら True。"""
    line = "talk_%s_0" % person            # 持ち歌と同じ置き場。1人1本を上書きして使う
    try:
        upload_to(sp.LINES_PREFIX + line + ".pcm", await _voice(text), "application/octet-stream")
    except Exception as e:
        _err(person, "voice", e)
        return False
    st["speak_line"] = line
    st["speak_at"] = time.time() + sp.SPEAK_MIN
    return True


def _err(person: str, step: str, e: Exception) -> None:
    sp._log_event("name_error", {"person": person, "step": step,
                                 "err": ("%s: %s" % (type(e).__name__, e))[:160]})


@router.post("/name")
async def hear_name(request: Request, person: str, x_upload_key: str = Header(None)):
    """ラズパイが取った数秒の音を受け取る。呼び名を聞いた答えか、聞き返しへの答えか。

    返事：{"ok", "name"（覚えた呼び名・まだなら null）,
           "say"（鳴らす声を置いた＝C3 に mur を送る）,
           "listen"（鳴らしたあと、もう一度聞いて送る）}"""
    if not key_ok(x_upload_key):
        raise HTTPException(status_code=401, detail="bad key")
    st = sp._load()
    now = time.time()
    if asking(st, now) != person:
        return {"ok": False, "why": "not_asking", "name": None, "say": False, "listen": False}
    a = dict(st.get("name_ask") or {})
    phase, cand, rnd = a.get("phase") or "ask", a.get("cand"), int(a.get("round") or 1)
    st["name_ask"] = {}                      # この答えで、いったん聞く番を閉じる
    try:
        pcm = _pcm16k(await request.body())
    except Exception as e:
        sp._save(st)
        raise HTTPException(status_code=400, detail=str(e))

    # 音の大きさを先に見る（9/19 の実測）。無音は文字にしない。
    level = _level(pcm)
    text = ""
    t0 = time.time()
    if level >= SILENT_LEVEL:
        try:
            text = await _to_text(pcm)
        except Exception as e:
            _err(person, "to_text", e)
    del pcm                                  # 音はここで捨てる
    t1 = time.time()

    def again(next_phase: str, next_cand=None) -> None:
        st["name_ask"] = {"person": person, "at": time.time(), "phase": next_phase,
                          "cand": next_cand, "round": rnd + 1}

    say, listen, learned, result = False, False, None, ""
    picked, answer = None, None               # 記録用：取り出した候補／聞き返しへの答え
    if phase == "ask":
        name = None
        if text:
            try:
                name = await _pick_name(text)
                picked = name
            except Exception as e:
                _err(person, "pick", e)
        if name:                             # 候補が取れた → 覚える前に聞き返す
            # 候補が取れたら、確かめる往復は上限を越えても必ず1回は残す（9/22 13:16：
            # 本人の3往復目で候補が取れたのに、聞き返しの答えが「うん」以外受け付けられず終わった）
            # 確かめの往復で「ちがう、◯◯」や聞き取れなかったときの確かめ直しも1回できるよう、2つ戻す。
            rnd = min(rnd, ROUND_MAX - 2)
            result = "cand"
            say = await _say(st, person, "%s……で、あってる？" % name)
            if say:
                again("confirm", name)
                listen = True
        elif rnd < ROUND_MAX:                # 取れなかった → 聞き直す
            result = "none_retry"
            say = await _say(st, person, "……もういっかい、いって？")
            if say:
                again("ask")
                listen = True
        else:
            result = "none_giveup"
    else:                                    # phase == "confirm"
        ans, fixed = "unclear", None
        if text:
            try:
                ans, fixed = await _yes_no(text)
                answer, picked = ans, fixed
            except Exception as e:
                _err(person, "yes_no", e)
        if ans == "yes" and cand:
            learned, result = cand, "yes"
            get_db().collection("faces").document(person).update({"name": cand, "name_at": now})
            say = await _say(st, person, "えへへ……%s。おぼえた" % cand)
        elif ans == "no" and fixed and rnd < ROUND_MAX:   # 違う、と言いながら言い直してくれた
            result = "no_fixed"
            say = await _say(st, person, "%s……で、あってる？" % fixed)
            if say:
                again("confirm", fixed)
                listen = True
        elif ans == "no" and rnd < ROUND_MAX:             # 違う → 聞き直す
            result = "no_retry"
            say = await _say(st, person, "ごめんね……なんて、よんだらいい？")
            if say:
                again("ask")
                listen = True
        elif ans == "unclear" and cand and rnd < ROUND_MAX:   # どちらか分からない → もう一度確かめる
            result = "unclear_retry"
            say = await _say(st, person, "%s……で、いい？" % cand)
            if say:
                again("confirm", cand)
                listen = True
        else:
            result = "giveup"
    sp._save(st)
    # 話した言葉は残さない。大きさ・字数・何が起きたかだけを残す。
    # 聞き取った文字は研究の記録として残す（9/22 本人決定「記録を残しましょう。了承は得ています」）。
    # 音は残さない。どの往復で何と聞こえ、何を候補にし、聞き返しにどう答えたかを、誰の・いつ、と一緒に。
    sp._log_event("name_heard", {"person": person, "phase": phase, "round": rnd,
                                 "level": level, "chars": len(text), "result": result,
                                 "text": text, "cand": cand, "picked": picked, "answer": answer,
                                 "ms": {"to_text": round((t1 - t0) * 1000),
                                        "rest": round((time.time() - t1) * 1000)}})
    if learned:
        sp._log_event("name_learned", {"person": person, "round": rnd})
        if sp.CALL_NAME:                     # 覚えた呼び名で呼べるよう、その人の一言を作り直す（9/22）
            try:
                await sp.remake_lines(person)
            except Exception as e:
                _err(person, "remake", e)
    return {"ok": True, "name": learned, "say": say, "listen": listen}


# ---- 4. 呼び名を消す（2026-09-21）----
# 聞き取り間違いで残ってしまったとき・本人から「消して」と言われたとき（掲示 v3 に書いた）。
# 消すと、次に来たときにまた聞く。

@router.post("/name/set")
async def set_name(person: str, name: str, x_upload_key: str = Header(None)):
    """呼び名を手で付ける（2026-09-22）。付け間違いを正しい人へ付け直すとき。

    例：p02 に付いた「ゆい」が本当は p13 のものだったら、p02 を clear して p13 に set する。"""
    if not key_ok(x_upload_key):
        raise HTTPException(status_code=401, detail="bad key")
    name = name.strip()[:NAME_MAX]
    if not name:
        raise HTTPException(status_code=400, detail="empty name")
    ref = get_db().collection("faces").document(person)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="no such person")
    ref.update({"name": name, "name_at": time.time()})
    sp._log_event("name_set", {"person": person})
    if sp.CALL_NAME:
        try:
            await sp.remake_lines(person)
        except Exception as e:
            _err(person, "remake", e)
    return {"ok": True, "person": person, "name": name}


@router.post("/name/clear")
async def clear_name(person: str, x_upload_key: str = Header(None)):
    if not key_ok(x_upload_key):
        raise HTTPException(status_code=401, detail="bad key")
    from google.cloud import firestore
    ref = get_db().collection("faces").document(person)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="no such person")
    ref.update({"name": firestore.DELETE_FIELD, "name_at": firestore.DELETE_FIELD,
                "name_asked_at": firestore.DELETE_FIELD})
    sp._log_event("name_clear", {"person": person})
    return {"ok": True, "person": person}


