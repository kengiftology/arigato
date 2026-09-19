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
  4. ここで文字にし（Google の音声認識）、音は捨てる。文字から呼び名だけを取り出し（Claude）、
     その人（faces/<ID>）に name として覚える
  5. 「◯◯……だね。おぼえた」をクラウドの VOICEVOX で声にして、次に鳴らす声に置く。
     返事に say=true を付け、ラズパイが C3 にもう一度鳴らさせる

残すもの：その人の呼び名と、聞いた時刻だけ。話した言葉そのものと音は残さない。
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

# 聞く側（ラズパイ／PC の橋渡し）がそろうまでは、聞かない（2026-09-19）。
# クラウドだけ先に入れると、問いかけが鳴っても誰も答えを取りに行かず、
# そのぶん迎えの言葉も出ない。使うときに Cloud Run の環境変数で 1 にする。
ASK_ON = os.environ.get("SPIRIT_ASK_NAME", "0") == "1"
ASK_KIND = "ask_name"            # 作り置きの問いかけ（ask_name_0 など）
ASK_GAP = 6 * 3600.0             # 聞けなかった人に、もう一度聞くまでの間
ASK_TTL = 90.0                   # 問いかけから、これより後に届いた答えは受け取らない
LISTEN_SEC = 5.0                 # ラズパイが取る音の長さ（ラズパイ側でも同じ値を使う）
NAME_MAX = 12                    # 呼び名の長さの上限（字）
SILENT_LEVEL = 25                # これより静かなら、声は入っていないとみなす（9/19の実測）
VOICEVOX_URL = os.environ.get("VOICEVOX_URL",
                              "https://voicevox-436112585717.asia-northeast1.run.app")
VOICEVOX_SPEAKER = 3             # ずんだもん（作り置きと同じ声）


# ---- 1. 迎えの場面で、聞くかどうか ----

def maybe_ask(st: dict, pid: str, doc: dict, now: float) -> bool:
    """呼び名が無い人なら、迎えの一言の代わりに問いかけを予約する。予約したら True。

    spirit.py の迎えから呼ぶ。たまに黙る（SILENT_CHANCE）は通さない。
    問いかけが鳴らないのに、ラズパイが聞きに行くことになるため。"""
    if not ASK_ON or not pid or pid == "unknown" or doc.get("name"):
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


@router.post("/name")
async def hear_name(request: Request, person: str, x_upload_key: str = Header(None)):
    """ラズパイが取った数秒の音を受け取り、呼び名を覚える。

    返事：{"ok", "name"（覚えた呼び名・無ければ null）, "say"（鳴らす声を置いたか）}"""
    if not key_ok(x_upload_key):
        raise HTTPException(status_code=401, detail="bad key")
    st = sp._load()
    now = time.time()
    if asking(st, now) != person:
        return {"ok": False, "why": "not_asking", "name": None, "say": False}
    st["name_ask"] = {}                      # 1回の問いかけに、答えは1回
    sp._save(st)
    try:
        pcm = _pcm16k(await request.body())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 音の大きさを先に見る（2026-09-19 の実測）。カメラに繋いでから数秒は音が出ず、
    # そのまま送ると、無音を文字にしようとして待たされるだけになる。
    level = _level(pcm)
    if level < SILENT_LEVEL:
        sp._log_event("name_heard", {"person": person, "level": level, "why": "silent",
                                     "got": False})
        return {"ok": True, "name": None, "say": False, "level": level}

    t0 = time.time()
    try:
        text = await _to_text(pcm)
    except Exception as e:
        sp._log_event("name_error", {"person": person, "step": "to_text",
                                     "err": ("%s: %s" % (type(e).__name__, e))[:160]})
        return {"ok": False, "why": "to_text", "name": None, "say": False}
    del pcm                                  # 音はここで捨てる
    t1 = time.time()
    name = None
    try:
        name = await _pick_name(text)
    except Exception as e:
        sp._log_event("name_error", {"person": person, "step": "pick",
                                     "err": ("%s: %s" % (type(e).__name__, e))[:160]})
    t2 = time.time()
    # 話した言葉は残さない。聞こえた字数と、呼び名が取れたかだけを残す。
    sp._log_event("name_heard", {"person": person, "chars": len(text), "got": bool(name),
                                 "ms": {"to_text": round((t1 - t0) * 1000),
                                        "pick": round((t2 - t1) * 1000)}})
    if not name:
        return {"ok": True, "name": None, "say": False}

    get_db().collection("faces").document(person).update({"name": name, "name_at": now})
    line = "named_%s_0" % person            # 持ち歌と同じ置き場・同じ名前の形
    try:
        upload_to(sp.LINES_PREFIX + line + ".pcm",
                  await _voice("%s……だね。おぼえた" % name), "application/octet-stream")
    except Exception as e:
        sp._log_event("name_error", {"person": person, "step": "voice",
                                     "err": ("%s: %s" % (type(e).__name__, e))[:160]})
        return {"ok": True, "name": name, "say": False}
    t3 = time.time()
    st = sp._load()
    st["speak_line"] = line
    st["speak_at"] = time.time() + sp.SPEAK_MIN
    sp._save(st)
    sp._log_event("name_learned", {"person": person, "voice_ms": round((t3 - t2) * 1000)})
    return {"ok": True, "name": name, "say": True}
