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
# 何人か居るときに聞かないか。9/22 22:0x 本人「ほかの人が居ても聞いてよい。どの服装の人に聞いているかを
# 言ってほしい」→ 既定は聞く。服で呼びかける作りが入るまでは、何人か居ても区別せずに聞く。
ASK_ONLY_ALONE = False
ASK_GAP = 6 * 3600.0             # 聞けなかった人に、もう一度聞くまでの間
ASK_TTL = 90.0                   # 問いかけから、これより後に届いた答えは受け取らない
NAME_MAX = 12                    # 呼び名の長さの上限（字）
SILENT_LEVEL = 25                # これより静かなら、声は入っていないとみなす（9/19の実測）
VOICEVOX_URL = os.environ.get("VOICEVOX_URL",
                              "https://voicevox-436112585717.asia-northeast1.run.app")
VOICEVOX_SPEAKER = 3             # ずんだもん（作り置きと同じ声）


# ---- 1. 迎えの場面で、聞くかどうか ----

def _may_ask(pid: str, doc: dict, now: float, alone: bool) -> bool:
    if not ASK_ON or (ASK_ONLY_ALONE and not alone) or not pid or pid == "unknown" or doc.get("name"):
        return False
    return now - float(doc.get("name_asked_at") or 0) >= ASK_GAP


def _mark_asked(st: dict, pid: str, now: float, line: str, sec: float, desc: str = "") -> None:
    st["name_ask"] = {"person": pid, "at": now, "sec": sec}
    # いつまで聞いているか。C3 が「聞いている顔」になる元（2026-09-23・研究トークD）
    st["listen_until"] = now + ASK_TTL
    try:
        get_db().collection("faces").document(pid).update({"name_asked_at": now})
    except Exception as e:
        logger.warning("name ask mark failed: %s", e)
    sp._log_event("name_ask", {"person": pid, "line": line, "desc": desc})


def maybe_ask(st: dict, pid: str, doc: dict, now: float, alone: bool = True) -> bool:
    """呼び名が無い人なら、迎えの一言の代わりに問いかけを予約する。予約したら True。

    spirit.py の迎えから呼ぶ。たまに黙る（SILENT_CHANCE）は通さない。
    問いかけが鳴らないのに、ラズパイが聞きに行くことになるため。
    何人か居るときに服で呼びかけるのは maybe_ask_async（こちらは作り置きの問いかけだけ）。"""
    if not _may_ask(pid, doc, now, alone):
        return False
    line = sp._pick_line(ASK_KIND)
    if not line:
        return False                       # 作り置きがまだ置かれていない
    st["speak_line"] = line
    st["speak_at"] = now + sp.SPEAK_SLOW   # ためらってから聞く
    _mark_asked(st, pid, now, line, ASK_LINE_SEC)
    return True


# ---- 1b. 何人か居るときは、服で呼びかける（2026-09-22）----
# 本人「ほかの人が居ても名前を聞いてよい。どの服装の人に聞いているかを言ってほしい」。
# 相手の顔の下（体のあたり）を切り出して Claude に服の特徴をひらがな10字以内で言わせ、
# 「あかい ふくの ひと、なんて よんだらいい？」をその場で声にする。1人のときは今までどおり。
# 顔の位置は照合（研究トークA）が返す boxes（回した後の写真の画素）。
ASK_BY_LOOK = True               # 何人か居るとき服で呼びかけるか。本人が見本を見て「よい」と言ったので 9/23 09:2x に入にした
ASK_LINE_SEC = 3.1               # 作り置きの問いかけの長さ（ask_name_0/1：3.0・3.08秒）

_LOOK_SYSTEM = """写真には1人の人の体（首から下のあたり）が写っています。
その人を、ほかの人と見分けるための見た目の特徴を1つだけ、子どもが言うような短いひらがなで答えてください。
- 服の色と種類がいちばんよい（例：「あかい ふくの」「しろい しゃつの」「くろい ぱーかーの」）
- 服が分からなければ、めがね・ぼうし・かみ など（例：「めがねの」「ぼうしの」）
- ひらがなとスペースだけ、10字以内、最後は「の」で終わる。カタカナ・アルファベットの服の名前も、読みをひらがなで書く（Tシャツ→「てぃーしゃつ」、パーカー→「ぱーかー」）
- 体つき・年齢・性別・肌の色には触れない
- 人が写っていない・分からないときは null
JSONだけで答える：{"look": "あかい ふくの"} または {"look": null}"""


def _body_crop(data: bytes, box: dict) -> bytes | None:
    """写真から、その人の顔の下（体のあたり）を切り出して JPEG で返す。"""
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        k = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
             270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(sp.FACE_ROTATE)
        if k is not None:
            img = cv2.rotate(img, k)          # 照合と同じ向き（boxes はこの向きの座標）
        cx, cy, w = int(box["box_cx"]), int(box["box_cy"]), int(box["box_w"])
        h, W = img.shape[:2]
        x0, x1 = max(0, cx - int(w * 1.8)), min(W, cx + int(w * 1.8))
        y0, y1 = max(0, cy + int(w * 0.6)), min(h, cy + int(w * 4.0))   # あごの下から胸・お腹のあたり
        if y1 - y0 < w or x1 - x0 < w:        # 体がほとんど写っていない（顔が画面の下の端）
            y0 = max(0, cy - w)               # 顔まわりも入れて、めがね・ぼうし・かみで言わせる
        crop = img[y0:y1, x0:x1]
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buf.tobytes() if ok else None
    except Exception as e:
        logger.warning("body crop failed: %s", e)
        return None


async def _look(jpg: bytes) -> str | None:
    """切り出した体の写真 → 「あかい ふくの」。分からなければ None。"""
    if not jpg or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    from anthropic import AsyncAnthropic
    msg = await AsyncAnthropic(timeout=15.0, max_retries=0).messages.create(
        model=sp.MODEL, max_tokens=60, system=_LOOK_SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(jpg).decode()}},
            {"type": "text", "text": "この人の見た目の特徴を1つ。JSONで。"}]}])
    out = "".join(b.text for b in msg.content if b.type == "text")
    i, j = out.find("{"), out.rfind("}")
    try:
        look = json.loads(out[i:j + 1]).get("look") if 0 <= i < j else None
    except Exception:
        return None
    if not isinstance(look, str):
        return None
    look = look.strip()
    # ひらがな・長音・スペースだけを通す（9/23 の見本で「tしゃつ」が出た。声が読み違える）
    import re
    if not re.fullmatch(r"[ぁ-んー 　]+", look) or not look.endswith("の"):
        return None
    return look[:12]


async def maybe_ask_async(st: dict, pid: str, doc: dict, now: float, alone: bool,
                          boxes: list, data: bytes) -> bool:
    """何人か居て、その人の顔の位置が分かるときは、服で呼びかけて聞く。
    それ以外（1人・位置が無い・服が分からない・声が作れない）は作り置きの問いかけ（maybe_ask）。"""
    if not _may_ask(pid, doc, now, alone):
        return False
    box = next((b for b in (boxes or []) if b.get("person") == pid and b.get("box_cx") is not None), None)
    if alone or not ASK_BY_LOOK or not box:
        return maybe_ask(st, pid, doc, now, alone)
    look = None
    try:
        look = await _look(_body_crop(data, box))
    except Exception as e:
        _err(pid, "look", e)
    if not look:
        return maybe_ask(st, pid, doc, now, alone)
    text = "%s ひと、なんて よんだらいい？" % look
    line = "talk_%s_0" % pid
    try:
        pcm = await _voice(text)
        upload_to(sp.LINES_PREFIX + line + ".pcm", pcm, "application/octet-stream")
    except Exception as e:
        _err(pid, "voice", e)
        return maybe_ask(st, pid, doc, now, alone)
    now2 = time.time()
    st["speak_line"] = line
    st["speak_at"] = now2 + sp.SPEAK_MIN   # 服を見て声を作るのに数秒かかったので、ためは短く
    _mark_asked(st, pid, now2, line, len(pcm) / 32000.0, look)
    return True


def asking(st: dict, now: float) -> str | None:
    """いま聞いている相手の ID。写真の返事に添えて、ラズパイに知らせる。"""
    a = st.get("name_ask") or {}
    if a.get("person") and now - float(a.get("at") or 0) < ASK_TTL:
        return a["person"]
    return None


def asking_sec(st: dict) -> float:
    """いまの問いかけの声の長さ（秒）。服で呼びかけると作り置きより長いので、橋渡しに知らせる。"""
    return float((st.get("name_ask") or {}).get("sec") or ASK_LINE_SEC)


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


# 聞き返しへの答えに出てくることば。あらかじめ渡しておくと拾いやすくなる（2026-09-23）。
# 「うん」のような短い返事は、カメラの粗いマイク（8kHz）だと空で返ることが多かった。
_YES_NO_WORDS = ["うん", "はい", "そう", "そうそう", "あってる", "あってるよ", "おっけー",
                 "ちがう", "ちがうよ", "ううん", "いいえ", "ちょっとちがう"]


async def _stt(pcm: bytes, model: str, hints: list | None = None) -> str:
    cfg = {"encoding": "LINEAR16", "sampleRateHertz": 16000,
           "languageCode": "ja-JP", "model": model}
    if hints:
        cfg["speechContexts"] = [{"phrases": hints, "boost": 15.0}]
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://speech.googleapis.com/v1/speech:recognize",
                         json={"config": cfg, "audio": {"content": base64.b64encode(pcm).decode()}},
                         headers={"Authorization": "Bearer " + _gcp_token()})
    r.raise_for_status()
    res = r.json().get("results") or []
    return "".join((x.get("alternatives") or [{}])[0].get("transcript", "") for x in res).strip()


async def _to_text(pcm: bytes, phase: str = "ask") -> tuple:
    """音 → (文字, どの設定で取れたか)。聞き取れなければ ("", "")。

    短い発話向け（latest_short）で空のときは、長め向け（latest_long）でもう一度試す。
    9/22〜23 の実機では、音の大きさが100〜700あるのに0字で返る回が続いた。
    聞き返しの場面では「うん」「ちがう」などを先に渡して拾いやすくする。"""
    hints = _YES_NO_WORDS if phase == "confirm" else None
    text = await _stt(pcm, "latest_short", hints)
    if text:
        return text, "short"
    text = await _stt(pcm, "latest_long", hints)
    return (text, "long") if text else ("", "")


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


_said_sec = [0.0]                    # 直前に _say で置いた声の長さ（秒）。橋渡しが「鳴り終わり」を見積もるのに使う


async def _say(st: dict, person: str, text: str) -> bool:
    """その場で声にして、次に鳴らす声に置く。置けたら True。"""
    line = "talk_%s_0" % person            # 持ち歌と同じ置き場。1人1本を上書きして使う
    try:
        pcm = await _voice(text)
        upload_to(sp.LINES_PREFIX + line + ".pcm", pcm, "application/octet-stream")
    except Exception as e:
        _err(person, "voice", e)
        return False
    st["speak_line"] = line
    st["speak_at"] = time.time() + sp.SPEAK_MIN
    _said_sec[0] = len(pcm) / 32000.0      # 16kHz・16bit・モノラル
    return True


# ---- 答えを受けたらすぐ「ん……」を鳴らす（2026-09-22）----
# 答えてから次の声まで、文字にする・判断する・声を作るで約5秒かかる。そのあいだ黙っていると
# 「変な間」になる（本人）。橋渡しは録音を締めたらまずここを叩き、C3 に短いつなぎを鳴らさせてから
# 答えの音を送る。つなぎは作り置き（filler_*）。
FILLER_KIND = "filler"


@router.post("/name/hmm")
async def hmm(person: str, x_upload_key: str = Header(None)):
    """つなぎ（相づち）を1つ置く。橋渡しは答えを送っている間、2.5秒おきにここを叩く（9/23）。"""
    if not key_ok(x_upload_key):
        raise HTTPException(status_code=401, detail="bad key")
    st = sp._load()
    now = time.time()
    if asking(st, now) != person:
        return {"ok": False, "say": False}
    # 聞き返しの声が置かれていたら、つなぎで上書きしない（9/23：上書きすると返事が消える）
    cur = st.get("speak_line") or ""
    if cur.startswith("talk_") and now < float(st.get("speak_at") or 0) + sp.SPEAK_TTL:
        return {"ok": True, "say": False, "why": "答えの声が先にある"}
    names = [n for n in sp._line_names() if n.rsplit("_", 1)[0] == FILLER_KIND]
    names = [n for n in names if n != st.get("last_hmm")] or names   # 同じ相づちを続けない
    if not names:
        return {"ok": True, "say": False}
    import random
    line = random.choice(names)
    st["speak_line"], st["last_hmm"] = line, line
    st["speak_at"] = now                  # すぐ鳴らす（つなぎなので間は置かない）
    sp._save(st)
    return {"ok": True, "say": True, "line": line}


# ---- その人の言い方を覚えておく（2026-09-23）----
# 本人「人ごとに個性を出したい」→ 声も型も変えず、**その人のしゃべり方を真似る**。
# 聞き取った文字を、その人の faces に短い一覧で残し、一言を作るときに見せる。
# 記録（spirit_log）から引くと索引が要るので、その人の欄にも置いておく。
SAID_KEEP = 10                   # 覚えておく件数（新しいものから）
SAID_MAX = 40                    # 1件の長さ
SAID_MIN = 4                     # これより短い聞き取りは入れない（0字や崩れた聞き取りを混ぜない）


def _keep_said(person: str, text: str) -> None:
    text = (text or "").strip()[:SAID_MAX]
    if len(text) < SAID_MIN:
        return
    try:
        ref = get_db().collection("faces").document(person)
        said = [s for s in ((ref.get().to_dict() or {}).get("said") or []) if isinstance(s, str)]
        if text in said:
            return
        ref.update({"said": ([text] + said)[:SAID_KEEP]})
    except Exception as e:
        logger.warning("keep said failed: %s", e)


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
    st["listen_until"] = 0                   # 聞き返すなら again() で入れ直す
    try:
        pcm = _pcm16k(await request.body())
    except Exception as e:
        sp._save(st)
        raise HTTPException(status_code=400, detail=str(e))

    # 音の大きさを先に見る（9/19 の実測）。無音は文字にしない。
    level = _level(pcm)
    text, stt = "", ""
    t0 = time.time()
    if level >= SILENT_LEVEL:
        try:
            text, stt = await _to_text(pcm, phase)
        except Exception as e:
            _err(person, "to_text", e)
    del pcm                                  # 音はここで捨てる
    t1 = time.time()

    def again(next_phase: str, next_cand=None) -> None:
        st["name_ask"] = {"person": person, "at": time.time(), "phase": next_phase,
                          "cand": next_cand, "round": rnd + 1}
        st["listen_until"] = time.time() + ASK_TTL

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
                                 "stt": stt,
                                 "ms": {"to_text": round((t1 - t0) * 1000),
                                        "rest": round((time.time() - t1) * 1000)}})
    _keep_said(person, text)
    if learned:
        sp._log_event("name_learned", {"person": person, "round": rnd})
        if sp.CALL_NAME:                     # 覚えた呼び名で呼べるよう、その人の一言を作り直す（9/22）
            try:
                await sp.remake_lines(person)
            except Exception as e:
                _err(person, "remake", e)
    return {"ok": True, "name": learned, "say": say, "listen": listen,
            "speak_sec": round(_said_sec[0], 2) if say else 0.0}


# ---- 4. 呼び名を消す（2026-09-21）----
# 聞き取り間違いで残ってしまったとき・本人から「消して」と言われたとき（掲示 v3 に書いた）。
# 消すと、次に来たときにまた聞く。

@router.post("/look_preview")
async def look_preview(request: Request, x_upload_key: str = Header(None)):
    """写真を1枚渡すと、写っている顔ごとに「服の言い方」と問いかけの文を返す（2026-09-22・本人に見せる用）。
    何も覚えず、何も鳴らさない。"""
    if not key_ok(x_upload_key):
        raise HTTPException(status_code=401, detail="bad key")
    from server import face
    data = await request.body()
    out = []
    # 本番と同じ足切りを通った顔だけ見る（9/23・研究トークA）。ガラスの映り込みは
    # 「小さい・大きくうつむいている」で落ちる。見本にだけ映り込みが出ると紛らわしい。
    for f in face.detect_faces(data, rotate=sp.FACE_ROTATE):
        if not f.get("up") or f.get("edge") or not face.big_enough_to_match(f["px"]):
            continue
        box = {"box_cx": f["pos"][0], "box_cy": f["pos"][1], "box_w": f["px"]}
        look = None
        try:
            look = await _look(_body_crop(data, box))
        except Exception as e:
            look = "（失敗：%s）" % type(e).__name__
        out.append({"face_px": f["px"], "pos": list(f["pos"]), "look": look,
                    "ask": ("%s ひと、なんて よんだらいい？" % look) if look and look.endswith("の") else None})
    return {"faces": out}


@router.post("/said/import")
async def said_import(x_upload_key: str = Header(None), limit: int = 1000):
    """記録に残っている聞き取りの文字を、その人の欄（faces.said）へ一度だけ移す（2026-09-23）。

    しゃべり方を写す仕組みは faces.said を見る。9/22〜23 のぶんは spirit_log にしかないので、
    これを一度叩いて移す。以後は聞き取るたびに両方へ入る。"""
    if not key_ok(x_upload_key):
        raise HTTPException(status_code=401, detail="bad key")
    db = get_db()
    rows = [d.to_dict() for d in db.collection("spirit_log")
            .order_by("t", direction="DESCENDING").limit(min(limit, 2000)).stream()]
    got = {}
    for r in sorted((r for r in rows if r.get("kind") == "name_heard"), key=lambda r: r.get("t") or 0):
        t = (r.get("text") or "").strip()[:SAID_MAX]
        if r.get("person") and len(t) >= SAID_MIN:
            got.setdefault(r["person"], [])
            if t not in got[r["person"]]:
                got[r["person"]].insert(0, t)
    out = {}
    for pid, said in got.items():
        try:
            ref = db.collection("faces").document(pid)
            old = [s for s in ((ref.get().to_dict() or {}).get("said") or []) if isinstance(s, str)]
            merged = said + [s for s in old if s not in said]
            ref.update({"said": merged[:SAID_KEEP]})
            out[pid] = merged[:SAID_KEEP]
        except Exception as e:
            logger.warning("said import failed (%s): %s", pid, e)
    sp._log_event("said_import", {"people": list(out)})
    return {"ok": True, "people": out}


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


