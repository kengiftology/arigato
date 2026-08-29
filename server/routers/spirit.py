"""地霊の脳（クラウド版）。

目(WROVER)が写真を送ってくる → Claudeが写真を直接見て「散らかり具合」を判断 →
地霊C3がスコア・放置度N・一言を取りに来る。

エンドポイント:
  POST /spirit/frame?  … 目からJPEG（body生バイト・X-Upload-Keyで認証）
  GET  /spirit/m       … "score N flag" プレーンテキスト（C3互換・先頭2列を読む）
  GET  /spirit/full    … 状態まるごとJSON（デバッグ・将来のセリフ用）
  GET  /spirit/presence?state=empty|occupied … C3から在室/不在の報告
  GET  /spirit/presence … 現在の在室状態を返す（目が撮る前に確認する用）

設計の原則（地霊の憲法）:
  ・人が写っていたら判断しない（評価もしない・スコアも動かさない）
  ・コメントは地霊の独り言だけ。指図・説教・皮肉は検閲して通さない
  ・スコアは平滑化して表情が暴れないようにする
  ・AI呼び出しは最短間隔と1日上限で費用に絶対の歯止め
状態はFirestore(doc: spirit/state)に永続化（Cloud Runの再起動でも消えない）。
"""
import json
import os
import re
import time
import base64
import logging

from fastapi import APIRouter, Request, Header, HTTPException
from fastapi.responses import PlainTextResponse

from server.database import get_db

router = APIRouter(prefix="/spirit", tags=["spirit"])
logger = logging.getLogger("spirit")

UPLOAD_KEY = os.environ.get("TIMELAPSE_KEY", "")   # 目の認証はタイムラプスと同じ鍵
MODEL = "claude-haiku-4-5-20251001"                # 頻繁に呼ぶので軽く速く安く

M_HI = 0.30            # このスコア以上が続くと放置度Nが育つ
N_FULL_S = 600.0       # Nが0->1になる秒数
JUDGE_MIN_GAP = 90.0   # AI判断の最短間隔（秒）
JUDGE_DAILY_CAP = 400  # 1日のAI呼び出し上限（費用の絶対の歯止め）
SCORE_ALPHA = 0.4      # スコア平滑化（0=動かない〜1=生値）
MAX_COMMENT = 24

_SYSTEM = (
    "あなたは共有キッチンに宿る『地霊』の感覚です。写真はあなたが見ている場所のいまの姿。"
    "『きれいに保たれているか／散らかっているか』を判断します。\n"
    "【最初に確認】写真に人が写っていたら、何も判断せず {\"skip\": true} だけを返す。\n"
    "【scoreの定義】score は散らかり度。0.0=完全にきれい、0.3=少し物がある、"
    "0.6=それなりに散らかっている、1.0=ひどく散らかっている。"
    "きれいなほど0に近い。間違えないこと。\n"
    "【commentの掟】地霊が自分の気持ちをつぶやく独り言だけ。"
    "人に指図・お願い・提案は絶対にしない（『片付けましょう』『〜してね』は禁止）。"
    "『そわそわするなあ』『すっきりして気持ちいいなあ』のように自分の心もちだけ。"
    "責めない・皮肉らない・数字を言わない。\n"
    "必ずJSONだけを返す: {\"score\": 0〜1の小数, \"comment\": \"15字以内の独り言\"} "
    "または {\"skip\": true}"
)

# 検閲: 責める・命令・提案の語（憲法違反）。含んだら穏当な既定文へ。
_BAD = ("汚い", "汚な", "片付", "片づけ", "掃除", "洗っ", "洗い", "戻し", "捨て",
        "しましょう", "ましょう", "ください", "してね", "しよう", "すべき", "たほうがいい",
        "だらしな", "ひどい", "最低", "ダメな人", "使えない", "気持ち悪", "サボ")

_state_cache: dict | None = None   # Firestore読み書き削減用（同一インスタンス内）


def _doc():
    return get_db().collection("spirit").document("state")


def _load() -> dict:
    global _state_cache
    if _state_cache is not None:
        return _state_cache
    try:
        snap = _doc().get()
        _state_cache = snap.to_dict() if snap.exists else {}
    except Exception as e:
        logger.warning("spirit state load failed: %s", e)
        _state_cache = {}
    _state_cache.setdefault("score", 0.0)
    _state_cache.setdefault("raw_score", 0.0)
    _state_cache.setdefault("comment", "")
    _state_cache.setdefault("empty", True)
    _state_cache.setdefault("t_high", None)      # スコア高が始まった時刻(epoch)
    _state_cache.setdefault("last_judge", 0.0)
    _state_cache.setdefault("day_start", 0.0)
    _state_cache.setdefault("day_calls", 0)
    return _state_cache


def _save(st: dict):
    global _state_cache
    _state_cache = st
    try:
        _doc().set(st)
    except Exception as e:
        logger.warning("spirit state save failed: %s", e)


def _calc_n(st: dict, now: float) -> float:
    if st["score"] >= M_HI:
        if not st.get("t_high"):
            st["t_high"] = now
        return min(1.0, (now - st["t_high"]) / N_FULL_S)
    st["t_high"] = None
    return 0.0


def _sanitize(c) -> str:
    if not isinstance(c, str):
        return ""
    c = re.sub(r"[{}\"\\\n]", " ", c).strip()
    if any(b in c for b in _BAD):
        return "きょうもおつかれさま"
    return c[:MAX_COMMENT]


async def _judge_image(image_bytes: bytes) -> dict:
    """写真をClaudeに直接見せて {score, comment} か {skip} を得る。失敗は {}。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {}
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        b64 = base64.standard_b64encode(image_bytes).decode()
        msg = await client.messages.create(
            model=MODEL, max_tokens=200, system=_SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": "いまのあなたの見た景色です。判断をJSONで。"},
            ]}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        i, j = text.find("{"), text.rfind("}")
        return json.loads(text[i:j + 1]) if i >= 0 and j > i else {}
    except Exception as e:
        logger.warning("spirit judge failed: %s", e)
        return {}


@router.post("/frame")
async def receive_frame(request: Request, x_upload_key: str = Header(None)):
    """目からのJPEG。在室中は捨てる。スロットル内なら受け取るだけで判断しない。"""
    if UPLOAD_KEY and x_upload_key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")

    st = _load()
    now = time.time()
    if not st["empty"]:
        return {"ok": True, "judged": False, "why": "occupied"}
    if now - st["last_judge"] < JUDGE_MIN_GAP:
        return {"ok": True, "judged": False, "why": "throttled"}
    if now - st["day_start"] > 86400:
        st["day_start"], st["day_calls"] = now, 0
    if st["day_calls"] >= JUDGE_DAILY_CAP:
        return {"ok": True, "judged": False, "why": "daily_cap"}

    r = await _judge_image(data)
    st["last_judge"] = now
    st["day_calls"] += 1
    if r.get("skip"):
        _save(st)
        return {"ok": True, "judged": False, "why": "person_in_frame"}
    sc = r.get("score")
    try:
        sc = max(0.0, min(1.0, float(sc)))
    except (TypeError, ValueError):
        sc = None
    if sc is not None:
        st["raw_score"] = sc
        st["score"] = (1 - SCORE_ALPHA) * st["score"] + SCORE_ALPHA * sc
        c = _sanitize(r.get("comment", ""))
        if c:
            st["comment"] = c
    _save(st)
    logger.info("spirit judge: raw=%s smoothed=%.2f comment=%s", sc, st["score"], st.get("comment"))
    return {"ok": True, "judged": sc is not None, "score": st["score"]}


@router.get("/m", response_class=PlainTextResponse)
async def get_m():
    """C3互換: 'score N flag'（flag 1=無人）。"""
    st = _load()
    n = _calc_n(st, time.time())
    return "%.3f %.3f %d\n" % (st["score"], n, 1 if st["empty"] else 0)


@router.get("/full")
async def get_full():
    st = _load()
    now = time.time()
    return {
        "score": st["score"], "raw_score": st["raw_score"],
        "N": _calc_n(st, now), "comment": st["comment"], "empty": st["empty"],
        "day_calls": st["day_calls"],
        "last_judge_ago": round(now - st["last_judge"]) if st["last_judge"] else None,
    }


@router.get("/presence", response_class=PlainTextResponse)
async def presence(state: str | None = None):
    """C3が ?state=empty|occupied で報告。引数なしは現在状態を返す（目が撮る前の確認用）。"""
    st = _load()
    if state in ("empty", "occupied"):
        st["empty"] = (state == "empty")
        _save(st)
        return "ok\n"
    return ("empty" if st["empty"] else "occupied") + "\n"
