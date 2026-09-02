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
import asyncio
import base64
import logging

from fastapi import APIRouter, Request, Header, HTTPException, UploadFile, File
from fastapi.responses import PlainTextResponse, HTMLResponse, Response

from server.database import get_db
from server.storage import upload_to, list_prefix, delete_prefix, read_object

router = APIRouter(prefix="/spirit", tags=["spirit"])
logger = logging.getLogger("spirit")

UPLOAD_KEY = os.environ.get("TIMELAPSE_KEY", "")   # 目の認証はタイムラプスと同じ鍵
MODEL = "claude-haiku-4-5-20251001"                # 頻繁に呼ぶので軽く速く安く

M_HI = 0.30            # このスコア以上が続くと放置度Nが育つ
N_FULL_S = 600.0       # Nが0->1になる秒数
# AI判断の最短間隔（秒）。実測で22秒に1回＝1日4000回相当のペースになっていたため、
# 状況で使い分ける。変化が起きるのは人が去った前後だけで、無人の部屋を
# 何度見ても同じ答えしか返らない（2026-08-31）。
JUDGE_GAP_AFTER_VISIT = 15.0   # 人が去った直後（巡回3枚を通したい）
JUDGE_GAP_IDLE = 600.0         # 誰も来ていない間（10分に1回で十分）
JUDGE_MIN_GAP = JUDGE_GAP_AFTER_VISIT
JUDGE_DAILY_CAP = 400  # 1日のAI呼び出し上限（費用の絶対の歯止め）
SCORE_ALPHA = 0.4      # スコア平滑化（0=動かない〜1=生値）
MAX_COMMENT = 24

# 人格はFirestore(spirit/state.persona)から注入。無ければこの既定文。
# ルール部（score定義・JSON形式・指図禁止）は人格に関わらず常に適用する。
_DEFAULT_PERSONA = "あなたは共有キッチンに宿る『地霊』の感覚です。"

# 物の呼び名と場所は決まった語からしか選ばせない（2026-09-02）。
# 自由に書かせると、何も動いていない同じ景色を90秒で6回見ただけで
# 「かご/籠/ざる」「ボトル/ボトル類/スプレー缶」と毎回違う語が返り、
# 前後を比べても物が動いたのか言葉が変わっただけなのか区別できなかった。
OBJ_NAMES = ("皿", "コップ", "鍋", "フライパン", "ボウル", "かご", "ボトル",
             "袋", "箱", "タッパー", "布巾", "まな板", "包丁", "食材",
             "調理器具", "書類", "ケーブル", "ごみ")
OBJ_PLACES = ("テーブル", "シンク", "コンロ", "調理台", "棚", "床", "窓辺")

_SYSTEM = (
    "写真はあなたが見ている場所のいまの姿。"
    "『きれいに保たれているか／散らかっているか』を判断します。\n"
    "【人が写っていたら】その人の見た目・服装・行動は一切書かない。"
    "personフィールドに人数だけ整数で入れ、物の観察は通常どおり続ける。"
    "誰が何をしているかの描写は禁止（記録に残すのは物の状態だけ）。\n"
    "【scoreの定義】score は散らかり度。0.0=完全にきれい、0.3=少し物がある、"
    "0.6=それなりに散らかっている、1.0=ひどく散らかっている。"
    "きれいなほど0に近い。間違えないこと。\n"
    "【commentの掟】地霊が自分の気持ちをつぶやく独り言だけ。"
    "人に指図・お願い・提案は絶対にしない（『片付けましょう』『〜してね』は禁止）。"
    "『そわそわするなあ』『すっきりして気持ちいいなあ』のように自分の心もちだけ。"
    "責めない・皮肉らない・数字を言わない。\n"
    "【objectsの書き方】写真に写っている物を挙げる。"
    "各項目は {\"name\":\"もの\", \"where\":\"場所\", \"n\":個数} の形。"
    "nameは次の語だけを使う（言い換え・造語は禁止）: " + "・".join(OBJ_NAMES) + "。"
    "どれにも当てはまらなければ挙げない。"
    "ざる・籠は『かご』、瓶・缶・スプレーは『ボトル』と書く。"
    "whereも次の語だけを使う（『棚下』『左棚』のような細かい言い方は禁止）: "
    + "・".join(OBJ_PLACES) + "。"
    "備え付けの設備（冷蔵庫・シンクそのもの・棚そのもの）は挙げない。"
    "同じ名前・同じ場所のものは1項目にまとめ、nに個数を入れる。"
    "多くても8個まで。\n"
    "必ずJSONだけを返す。改行や字下げを入れず1行で書く: "
    "{\"score\": 0〜1の小数, \"comment\": \"15字以内の独り言\", "
    "\"objects\": [...], \"person\": 写っている人数}"
)

# 検閲: 責める・命令・提案の語（憲法違反）。含んだら穏当な既定文へ。
_BAD = ("汚い", "汚な", "片付", "片づけ", "掃除", "洗っ", "洗い", "戻し", "捨て",
        "しましょう", "ましょう", "ください", "してね", "しよう", "すべき", "たほうがいい",
        "だらしな", "ひどい", "最低", "ダメな人", "使えない", "気持ち悪", "サボ")

FACE_ROTATE = 0          # カメラの取り付け向きの補正（2026-08-31の実測で270度が正しいと判明）
FACE_ENABLED = os.environ.get("FACE_ENABLED", "") == "1"   # 掲示が済むまでは既定でオフ

_identify_err = [""]   # 顔検出の失敗理由（/spirit/facesで確認する）
# 人が写る写真を一時的に残す置き場（2026-09-02・研究室の承諾のもと）。
# 通常は残さない決まりだが、顔の分裂などは写真がないと詰められない。
# ・専用の置き場にまとめる（他の写真と混ざらないので、まとめて消せる）
# ・期限つきでしか入らない（消し忘れではなく、切り忘れが一番こわい）
VERIFY_PREFIX = "spirit/verify/"
VERIFY_GAP = 10.0      # 同じ滞在で撮りすぎないための間隔（秒）

_judge_err = [""]      # 判断の失敗理由（/spirit/fullで確認する）
                       # 判断が黙って失敗すると、古い一言が残り続けるだけで
                       # 表からは動いているように見える。実際に40時間気づけなかった。
_state_cache: dict | None = None   # Firestore読み書き削減用（同一インスタンス内）


def _doc():
    return get_db().collection("spirit").document("state")


_pending = {"vec": None, "t": 0.0}      # まだIDを与えていない「知らない顔」


def _confirm_new(vec: list) -> bool:
    """知らない顔にIDを出してよいかを決める（2026-09-02）。

    1コマ見ただけで卵を作っていたため、1時間で8つのIDが生まれた。
    うち少なくとも1つは、誰も居ない台所の棚を顔と見た誤検出だった。

    誤検出はその場限りのゴミなので、続けて似た顔がもう一度出ることはない。
    そこで「知らない顔を、短い間に2回、しかも互いに似た形で見た」ときだけ
    新しいIDを出す。本物の人はカメラの前に数秒は留まるので、この条件を通る。"""
    import numpy as np
    now = time.time()
    prev, prev_t = _pending["vec"], _pending["t"]
    _pending["vec"], _pending["t"] = vec, now
    if prev is None or now - prev_t > 30:            # 前の心当たりが古ければやり直し
        return False
    sim = float(np.dot(np.asarray(vec, dtype=np.float32),
                       np.asarray(prev, dtype=np.float32)))
    if sim < 0.42:                                   # 2回が別物＝たまたま拾ったゴミ
        return False
    _pending["vec"], _pending["t"] = None, 0.0
    return True


def _clean_objects(raw) -> list:
    """決めた語だけを残し、同じ名前・場所をまとめる。

    言い方の揺れを頼み込みだけで抑えるのは無理があるので、受け取った側でも
    ふるいにかける。ここを通ったものだけが前後の比較に使える。"""
    bucket = {}
    for o in raw or []:
        if not isinstance(o, dict):
            continue
        name, where = o.get("name"), o.get("where")
        if name not in OBJ_NAMES or where not in OBJ_PLACES:
            continue
        try:
            n = max(1, int(o.get("n") or 1))
        except (TypeError, ValueError):
            n = 1
        bucket[(name, where)] = bucket.get((name, where), 0) + n
    out = [{"name": k[0], "where": k[1], "n": v} for k, v in bucket.items()]
    out.sort(key=lambda x: (x["where"], x["name"]))
    return out[:10]


def _vec_list(raw) -> list:
    """保存された特徴量を、素の数値の並びに戻す。

    Firestoreは配列の中に配列を入れられない。特徴量は128個の数値の並びで、
    それを人ごとに何本か持つので、素直に書くと配列の入れ子になって拒否される
    （実際に本番で顔IDが一度も発行されず、原因が見えないままだった）。
    そこで1本ずつ {"v": [...]} という連想配列に包んで保存する。
    ここは古い形（素の並び）で入っているものも読めるようにしてある。"""
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            v = item.get("v")
            if v:
                out.append(v)
        elif isinstance(item, list):
            out.append(item)
    return out


def _log_event(kind: str, data: dict):
    """研究用の時系列ログ（spirit_log）。失敗しても本体を止めない。"""
    try:
        get_db().collection("spirit_log").add({"t": time.time(), "kind": kind, **data})
    except Exception as e:
        logger.warning("spirit log failed: %s", e)


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


async def _judge_image(image_bytes: bytes, persona: str = "") -> dict:
    """写真をClaudeに直接見せて {score, comment} か {skip} を得る。失敗は {}。
    persona＝そのキャラの人格。ルール部（_SYSTEM）は人格に関わらず常に適用。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _judge_err[0] = "ANTHROPIC_API_KEY が設定されていない"
        return {}
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        b64 = base64.standard_b64encode(image_bytes).decode()
        system = (persona or _DEFAULT_PERSONA) + "\n" + _SYSTEM
        msg = await client.messages.create(
            # 物の一覧を返させるようになってから、200では足りず返事が途中で
            # 切れていた。壊れたJSONは黙って捨てられ、古い一言が残るので
            # 表からは動いて見えたまま40時間気づけなかった（2026-09-02）。
            model=MODEL, max_tokens=1000, system=system,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": "いまのあなたの見た景色です。判断をJSONで。"},
            ]}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            _judge_err[0] = "JSONで返ってこなかった: " + text[:160]
            return {}
        _judge_err[0] = ""
        return json.loads(text[i:j + 1])
    except Exception as e:
        _judge_err[0] = "%s: %s" % (type(e).__name__, str(e)[:200])
        logger.warning("spirit judge failed: %s", e)
        return {}


def _identify(data: bytes):
    """写真から顔を探して匿名IDに結びつける。顔が無ければ None。
    実名は扱わない。初めての顔には新しい匿名IDを発行して「卵」にする。"""
    from server import face
    # 向きは橋渡しの段階で正しく直してから届くので、1通りだけ見る。
    # 5通り試していた頃は、そのぶん誤検出の機会も5倍あった。
    crop = face.detect_face(data, rotate=FACE_ROTATE)
    if crop is None:
        return None
    vec = face.embed(crop)
    if vec is None:
        return None
    known = _known_faces()
    pid, sim = face.match(vec, known)
    db = get_db()
    if pid is None:                                  # 初めて見る顔
        if not _confirm_new(vec):
            return None                              # 一度きりの見え方は信用しない
        pid = _new_person_id()
        db.collection("faces").document(pid).set(
            {"vecs": [{"v": vec}], "born": time.time(), "persona": "", "state": "egg"})
        _log_event("arrive", {"person": pid, "state": "new_egg", "sim": round(sim, 3)})
        return {"person": pid, "state": "egg"}
    doc = db.collection("faces").document(pid).get().to_dict() or {}
    vecs = doc.get("vecs", [])
    if len(vecs) < 5:                                # 見るたび少しずつ覚え直す（眼鏡・照明差に強くする）
        vecs.append({"v": vec})
        db.collection("faces").document(pid).update({"vecs": vecs})
    state = "ready" if doc.get("persona") else "egg"
    _log_event("arrive", {"person": pid, "state": state, "sim": round(sim, 3)})
    return {"person": pid, "state": state}


def _keep_shot(st: dict, now: float, data: bytes, pid: str) -> None:
    """確かめ期間中だけ、人の写った1枚を専用の置き場に残す。

    期限が切れていれば何もしない。撮りすぎないよう間隔を空ける
    （11分の滞在で100枚溜まっても、確かめの役には立たない）。"""
    if now >= st.get("verify_until", 0):
        return
    if now - st.get("last_shot", 0) < VERIFY_GAP:
        return
    try:
        name = VERIFY_PREFIX + "%d_%s.jpg" % (int(now), pid or "unknown")
        url = upload_to(name, data, "image/jpeg")
        st["last_shot"] = now
        _log_event("shot", {"person": pid, "url": url})
    except Exception as e:
        logger.warning("verify shot failed: %s", e)


@router.post("/verify")
async def verify_mode(minutes: int = 0, key: str = ""):
    """人の写った写真を残す期間を決める（0で即停止）。

    期限式にしてあるのは、切り忘れを防ぐため。承諾を得た確かめのために
    開けた窓が、そのまま開きっぱなしになるのが一番まずい。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    st = _load()
    minutes = max(0, min(int(minutes), 240))
    st["verify_until"] = time.time() + minutes * 60 if minutes else 0
    _save(st)
    _log_event("verify", {"minutes": minutes})
    return {"ok": True, "minutes": minutes,
            "until": st["verify_until"] or None,
            "note": "0にすると即座に止まります。撮った写真は /spirit/shots で見られます"}


@router.get("/shots")
async def list_shots():
    """確かめ用に残した写真の一覧。"""
    st = _load()
    try:
        shots = sorted(list_prefix(VERIFY_PREFIX), key=lambda x: -(x.get("at") or 0))
    except Exception as e:
        return {"shots": [], "error": str(e)}
    left = st.get("verify_until", 0) - time.time()
    return {"shots": shots, "count": len(shots),
            "recording": left > 0, "minutes_left": round(left / 60, 1) if left > 0 else 0}


@router.post("/shots/clear")
async def clear_shots(key: str = ""):
    """確かめ用の写真を全部消す。技術的な確認が済んだらこれを叩く。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    try:
        n = delete_prefix(VERIFY_PREFIX)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    st = _load()
    st["verify_until"] = 0
    _save(st)
    _log_event("verify_clear", {"deleted": n})
    return {"ok": True, "deleted": n}


@router.post("/frame")
async def receive_frame(request: Request, pose: str = "", raw: str = "", x_upload_key: str = Header(None)):
    """目からの写真1枚を、すべての判断に使う統合窓口（2026-08-31改訂）。

    以前は人感センサーが「人が居る」を判定していたが、座って動かない人を
    見失った（実測：二人が食事中に無人と誤判定）。写真を見れば人が居るかも
    誰かも同時に分かるので、判断の入口を写真に一本化する。

    順序:
      1) 顔が写っているか（無料・その場で）→ 写っていれば誰かを照合して終わり
      2) 顔が無ければAIに見せる → 人が写っていれば在室と記録（散らかりは測らない）
      3) 人も居なければ散らかりを判断する
    """
    if UPLOAD_KEY and x_upload_key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    if raw:                                    # 生の白黒（例 raw=640x480）はJPEGへ直す
        try:
            w, h = (int(x) for x in raw.lower().split("x"))
            data = _raw_gray_to_jpeg(data, w, h)
        except Exception as e:
            logger.warning("raw decode failed: %s", e)
            return {"ok": False, "why": "bad_raw"}

    st = _load()
    now = time.time()

    if FACE_ENABLED:                           # ① 顔があれば、それが在室の証拠かつ本人の手がかり
        try:
            res = _identify(data)
            if res:
                st["empty"] = False
                st["cur_person"] = res["person"]
                st["cur_state"] = res["state"]
                st["last_seen"] = now
                # 前回の判断からこちら、誰が居たかを溜めておく。
                # 判断の時点で cur_person を見ると、とうに帰った人の名が残り、
                # 無人の記録にまで同じIDが付いていた（2026-09-02に実際に起きた）。
                seen = st.get("seen_people") or []
                if res["person"] not in seen:
                    seen.append(res["person"])
                    st["seen_people"] = seen[-8:]
                vis = st.get("visit_people") or []       # 前後比較に添える顔ぶれ
                if res["person"] not in vis:
                    vis.append(res["person"])
                    st["visit_people"] = vis[-8:]
                _keep_shot(st, now, data, res["person"])
                _save(st)
                return {"ok": True, "person": res["person"], "state": res["state"],
                        "judged": False, "why": "person_seen"}
        except Exception as e:
            logger.warning("identify failed: %s", e)
            _identify_err[0] = "%s: %s" % (type(e).__name__, str(e)[:200])

    # 人が去った直後（5分以内）は細かく見る。それ以外は間隔を空けて無駄打ちを避ける
    recent_visit = (now - st.get("last_seen", 0)) < 300
    gap = JUDGE_GAP_AFTER_VISIT if recent_visit else JUDGE_GAP_IDLE
    if now - st["last_judge"] < gap:
        return {"ok": True, "judged": False, "why": "throttled"}
    if now - st["day_start"] > 86400:
        st["day_start"], st["day_calls"] = now, 0
    if st["day_calls"] >= JUDGE_DAILY_CAP:
        return {"ok": True, "judged": False, "why": "daily_cap"}

    try:                                  # 状態ページ用の最新1枚。人がいる間は保存しない
        if not st.get("empty", True):
            raise RuntimeError("person present: not saving photo")
        url = upload_to("spirit/latest.jpg", data, "image/jpeg")
        st["photo_url"] = url
        st["photo_at"] = now
    except Exception as e:
        logger.warning("latest photo save failed: %s", e)

    r = await _judge_image(data, st.get("persona", ""))
    st["last_judge"] = now
    st["day_calls"] += 1
    npeople = r.get("person")
    try:
        npeople = int(npeople) if npeople is not None else 0
    except (TypeError, ValueError):
        npeople = 0
    if npeople > 0:                       # 人がいても観察は続ける（2026-09-01改訂）
        st["empty"] = False               # 誰が何を動かしたかを知るため
        st["last_seen"] = now
        # 顔は取れなかったのに人は写っている＝顔検出が取りこぼした場面。
        # 確かめたいのはまさにここなので、期間中はこれも残す。
        _keep_shot(st, now, data, "noface")
    elif r.get("skip"):                   # 旧仕様の名残（人がいるとだけ返る場合）
        st["empty"] = False
        st["last_seen"] = now
        _log_event("judge", {"skip": True})
        _save(st)
        return {"ok": True, "judged": False, "why": "person_in_frame"}
    else:
        st["empty"] = True
    sc = r.get("score")
    try:
        sc = max(0.0, min(1.0, float(sc)))
    except (TypeError, ValueError):
        sc = None
    if sc is not None:
        # 方向(pose)ごとに最新値を持ち、全体スコア＝方向の平均。
        # 巡回で「カウンター0.7→床0.2」を時系列に混ぜると偽の急降下が生まれ
        # 世話イベントが暴発する（2026-08-30に実際に起きた）ため、方向は混ぜない。
        poses = st.get("poses", {})
        poses[pose or "michi"] = sc
        st["poses"] = poses
        overall = sum(poses.values()) / len(poses)
        st["raw_score"] = overall
        st["score"] = (1 - SCORE_ALPHA) * st["score"] + SCORE_ALPHA * overall
        c = _sanitize(r.get("comment", ""))
        if c:
            st["comment"] = c
    objs = r.get("objects")
    if isinstance(objs, list):
        st["objects"] = _clean_objects(objs)
    _save(st)                             # 物の一覧を入れてから保存する。
                                          # 逆順だと一覧はこの場限りで消え、
                                          # 状態ページには何も出ないままになる。
    if sc is not None:
        _log_event("judge", {"raw": sc, "score": round(st["score"], 3), "pose": pose,
                             "N": round(_calc_n(st, now), 3), "comment": st.get("comment", ""),
                             "objects": st.get("objects", []), "people": npeople,
                             "who": st.get("seen_people") or []})
    st["seen_people"] = []                # ここまでを1区間として締める
    _save(st)
    # 人が去って落ち着いてから突き合わせる。居る間の1枚を「後」にすると
    # 本人が写り込んでしまい、物の変化と見分けがつかない。
    if npeople == 0 and now - st.get("last_seen", 0) > VISIT_END_GAP:
        # 区画の数だけAIに問い合わせるので、返事を待たせると橋渡しが
        # 待ちきれずに切れる。返事は先に返し、突き合わせは裏で走らせる。
        if not _zone_busy[0]:
            _zone_busy[0] = True
            asyncio.create_task(_zone_cycle_bg(st, data, now))
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
        "objects": st.get("objects", []),        # いま見えているもの（状態ページ用）
        "photo_url": st.get("photo_url"), "photo_at": st.get("photo_at"),
        "person": st.get("cur_person"), "person_state": st.get("cur_state"),
        "last_judge_ago": round(now - st["last_judge"]) if st["last_judge"] else None,
        "judge_error": _judge_err[0],
    }


@router.get("/presence", response_class=PlainTextResponse)
async def presence(state: str | None = None):
    """C3が ?state=empty|occupied で報告。引数なしは現在状態を返す（目が撮る前の確認用）。"""
    st = _load()
    if state in ("empty", "occupied"):
        prev = st["empty"]
        st["empty"] = (state == "empty")
        if prev != st["empty"]:
            _log_event("presence", {"empty": st["empty"]})   # 在室の変化も研究データ
        _save(st)
        return "ok\n"
    return ("empty" if st["empty"] else "occupied") + "\n"


@router.get("/care", response_class=PlainTextResponse)
async def care(n: int = 0):
    """C3が世話イベント検出時に報告してくる。研究の主要指標なので必ず時刻つきで残す。"""
    _log_event("care", {"count": n})
    return "ok\n"


_PAGE = """<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>キッチンちゃんのようす</title><style>
body{font-family:sans-serif;max-width:560px;margin:0 auto;padding:16px;background:#faf6ec;color:#333}
h1{font-size:20px} .card{background:#fff;border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 1px 4px #0002}
.face{font-size:64px;text-align:center} .say{font-size:18px;text-align:center;margin:8px 0;color:#555}
.bar{height:10px;background:#eee;border-radius:5px;overflow:hidden}.bar>i{display:block;height:100%;background:#e8a33d}
.lbl{font-size:12px;color:#888;margin-top:10px} img{width:100%;border-radius:8px}
.ev{font-size:13px;border-bottom:1px solid #eee;padding:6px 0}.t{color:#aaa;margin-right:8px}
.tag{display:inline-block;background:#f1ede2;border-radius:8px;padding:3px 10px;margin:3px 4px;font-size:14px}
</style></head><body>
<h1>キッチンちゃんのようす</h1>
<div class="card"><div class="face" id="face">…</div><div class="say" id="say">よみこみちゅう…</div>
<div class="lbl">ちらかりぐあい</div><div class="bar"><i id="score" style="width:0%"></i></div>
<div class="lbl">ほったらかされど</div><div class="bar"><i id="nbar" style="width:0%;background:#7f77dd"></i></div>
<div class="lbl" id="meta"></div></div>
<div class="card"><div class="lbl">さいごに みたけしき（人がいないときだけ撮影）</div><img id="photo" alt="景色"></div>
<div class="card"><div class="lbl">いま見えているもの</div><div id="objs">…</div></div>
<div class="card"><div class="lbl">できごと</div><div id="log"></div></div>
<script>
async function forget(){
  if(!confirm('覚えた顔をすべて忘れます。元に戻せません。'))return;
  post('/spirit/faces/clear').then(function(j){
    if(j.detail){alert('合言葉がちがいます');return}
    alert(j.deleted+'件 忘れました'); load();
  });
}
function load(){
 const f=await (await fetch('/spirit/full')).json();
 const face = !f.empty ? '👀' : (f.N>=0.5 ? '😔' : (f.score<0.3 ? '😊' : '😐'));
 document.getElementById('face').textContent = face;
 document.getElementById('say').textContent = f.comment || '……';
 document.getElementById('score').style.width = Math.round(f.score*100)+'%';
 document.getElementById('nbar').style.width = Math.round(f.N*100)+'%';
 document.getElementById('meta').textContent =
   (f.empty?'いまは だれもいない':'いま だれかいる（撮影はお休み）')+'　/ きょうの判断 '+f.day_calls+'回';
 if(f.photo_url) document.getElementById('photo').src = f.photo_url+'?t='+(f.photo_at||Date.now());
 document.getElementById('objs').innerHTML = (f.objects&&f.objects.length) ? f.objects.map(function(o){return '<span class="tag">'+(o.name||'')+(o.n>1?('×'+o.n):'')+(o.where?('<small> '+o.where+'</small>'):'')+'</span>';}).join(' ') : 'とくに何も出ていないみたい';
 const lg=await (await fetch('/spirit/log?limit=30')).json();
 const jp={judge:'かんがえた',care:'おせわされた！',presence:'けはい'};
 document.getElementById('log').innerHTML=(lg.events||[]).map(e=>{
  const d=new Date(e.t*1000).toLocaleString('ja-JP',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
  let s=jp[e.kind]||e.kind;
  if(e.kind==='judge') s+= e.skip?'（ひとが写ったのでスキップ）':('：'+(e.comment||'')+'（'+Math.round((e.raw??0)*100)+'）');
  if(e.kind==='presence') s+= e.empty?'：いなくなった':'：だれかきた';
  if(e.kind==='care') s+='（'+(e.count||'?')+'回目）';
  return '<div class="ev"><span class="t">'+d+'</span>'+s+'</div>';}).join('');
}
load(); setInterval(load, 30000);
</script></body></html>"""

_NOTICE = """<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>この装置について</title><style>
body{font-family:sans-serif;max-width:560px;margin:0 auto;padding:16px;line-height:1.8;color:#333}
h1{font-size:20px}h2{font-size:16px;margin-top:24px}li{margin:4px 0}.sig{color:#888;margin-top:24px}
</style></head><body>
<h1>この装置について</h1>
<p>これは慶應義塾大学の修士研究の一環として設置している装置です。
共有スペースの整備が、命令や当番ではなく「ありがとう」や愛着によって続くかを観察しています。</p>
<h2>記録するもの</h2>
<ul><li>この場所の散らかりぐあい（カメラ画像をAIが判断した数値）</li>
<li>「片づけられた」というできごとの回数と時刻</li>
<li>人の気配があった/なくなったの切り替わり（人感センサー）</li></ul>
<h2>記録しないもの</h2>
<ul><li>人が写った写真（人がいるあいだはカメラは撮影を止めます）</li>
<li>顔・名前など個人を特定する情報</li><li>音声</li></ul>
<p>画像の判断にはAI（Anthropic社のClaude）を使用しています。データは研究終了時に破棄します。</p>
<p>装置を止めてほしい・気になることがある場合はご連絡ください。</p>
<p class="sig">連絡先：桒原（kengk0328@gmail.com）</p>
</body></html>"""


@router.get("/page", response_class=HTMLResponse)
async def status_page():
    """キッチンちゃんの状態ページ（気分・一言・最新の景色・できごと）。"""
    return _PAGE


@router.get("/notice", response_class=HTMLResponse)
async def notice_page():
    """掲示のQRの行き先（この装置についての説明）。"""
    return _NOTICE


NOTE_KEY = "40568478"   # 観察メモの書き込み合言葉（いたずら防止程度・研究者本人用）

_NOTES = """<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>フィールドノート</title><style>
body{font-family:sans-serif;max-width:560px;margin:0 auto;padding:16px;background:#faf6ec;color:#333}
h1{font-size:20px}.card{background:#fff;border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 1px 4px #0002}
select,input,textarea,button{font-size:16px;padding:10px;border-radius:8px;border:1px solid #ccc;width:100%;box-sizing:border-box;margin:4px 0}
button{background:#e8a33d;color:#fff;border:none;font-weight:bold}
.ev{font-size:14px;border-bottom:1px solid #eee;padding:8px 0}.t{color:#aaa;font-size:12px}
.tag{display:inline-block;background:#eee;border-radius:6px;padding:1px 8px;font-size:12px;margin-right:6px}
#msg{color:#2a7;font-size:14px}</style></head><body>
<h1>フィールドノート</h1>
<div class="card">
<select id="tag"><option>観察</option><option>口頭のありがとう</option><option>地霊が話題に</option>
<option>愛称・呼び名</option><option>違和感・嫌がり</option><option>答え合わせ</option><option>その他</option></select>
<textarea id="text" rows="3" placeholder="気づいたことを一行（例：◯◯さんが地霊に話しかけてた）"></textarea>
<input id="key" type="password" placeholder="あいことば（初回だけ）">
<button onclick="send()">記録する</button><div id="msg"></div></div>
<div class="card"><div id="list">よみこみちゅう…</div></div>
<script>
const K='spirit_note_key';
if(localStorage.getItem(K)) document.getElementById('key').style.display='none';
async function send(){
 const t=document.getElementById('text').value.trim(); if(!t){return}
 const key=localStorage.getItem(K)||document.getElementById('key').value;
 const r=await fetch('/spirit/note',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({text:t,tag:document.getElementById('tag').value,key})});
 if(r.ok){localStorage.setItem(K,key);document.getElementById('key').style.display='none';
   document.getElementById('text').value='';document.getElementById('msg').textContent='記録しました';
   setTimeout(()=>document.getElementById('msg').textContent='',2000);load();}
 else{document.getElementById('msg').textContent='あいことばが違うかも';}}
async function load(){
 const d=await (await fetch('/spirit/notes_data?limit=50')).json();
 document.getElementById('list').innerHTML=(d.notes||[]).map(n=>{
  const dt=new Date(n.t*1000).toLocaleString('ja-JP',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
  return '<div class="ev"><div class="t">'+dt+'</div><span class="tag">'+(n.tag||'')+'</span>'+n.text+'</div>';
 }).join('')||'まだ記録がありません';}
load();
</script></body></html>"""


@router.get("/notes", response_class=HTMLResponse)
async def notes_page():
    """研究者の観察メモ入力ページ（スマホでその場で1行）。"""
    return _NOTES


@router.post("/persona")
async def set_persona(request: Request):
    """キャラの人格を設定（誕生エージェントの出力を注入する口・合言葉つき）。"""
    body = await request.json()
    if body.get("key") != NOTE_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    st = _load()
    st["persona"] = str(body.get("persona", ""))[:2000]
    _save(st)
    _log_event("persona", {"len": len(st["persona"])})
    return {"ok": True, "len": len(st["persona"])}


@router.post("/note")
async def add_note(request: Request):
    body = await request.json()
    if body.get("key") != NOTE_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    text = str(body.get("text", ""))[:500].strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty")
    get_db().collection("fieldnotes").add(
        {"t": time.time(), "tag": str(body.get("tag", ""))[:30], "text": text})
    return {"ok": True}


@router.get("/notes_data")
async def notes_data(limit: int = 50):
    try:
        docs = get_db().collection("fieldnotes").order_by(
            "t", direction="DESCENDING").limit(min(limit, 500)).stream()
        return {"notes": [d.to_dict() for d in docs]}
    except Exception as e:
        return {"notes": [], "error": str(e)}


@router.get("/export")
async def export_all():
    """論文分析用：機械ログと観察メモをまとめてJSONで返す。"""
    out = {"spirit_log": [], "fieldnotes": []}
    try:
        out["spirit_log"] = [d.to_dict() for d in get_db().collection(
            "spirit_log").order_by("t").limit(20000).stream()]
        out["fieldnotes"] = [d.to_dict() for d in get_db().collection(
            "fieldnotes").order_by("t").limit(5000).stream()]
    except Exception as e:
        out["error"] = str(e)
    return out


# ---- C3の字幕窓（一言を 240x42 のRGB565画像にして配る。C3はこれをそのまま表示する）----
_FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansjp/NotoSansJP%5Bwght%5D.ttf"
_FONT_PATH = "/tmp/notojp.ttf"
_WIN_W, _WIN_H = 240, 42
_win_cache = {"text": None, "bin": None}


async def _ensure_font():
    if os.path.exists(_FONT_PATH):
        return True
    try:
        import httpx
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.get(_FONT_URL)
            r.raise_for_status()
            with open(_FONT_PATH, "wb") as f:
                f.write(r.content)
        return True
    except Exception as e:
        logger.warning("font download failed: %s", e)
        return False


def _render_win(text: str) -> bytes:
    """一言 → C3のblitWindow形式（[h][0][RGB565上位バイト先 240×42]）。"""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (_WIN_W, _WIN_H), (247, 240, 224))     # クリーム背景（本体と同じ）
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, _WIN_W - 1, _WIN_H - 1], outline=(120, 110, 90))
    try:
        font = ImageFont.truetype(_FONT_PATH, 18)
    except Exception:
        font = ImageFont.load_default()
    t = text or "……"
    while len(t) > 1 and d.textlength(t, font=font) > _WIN_W - 12:
        t = t[:-1]                                                # 収まるまで末尾を落とす
    w = d.textlength(t, font=font)
    d.text(((_WIN_W - w) // 2, 9), t, fill=(60, 50, 40), font=font)
    px = img.load()
    out = bytearray([_WIN_H, 0])
    for y in range(_WIN_H):
        for x in range(_WIN_W):
            r, g, b = px[x, y]
            v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out.append((v >> 8) & 0xFF)
            out.append(v & 0xFF)
    return bytes(out)


@router.get("/win.bin")
async def win_bin():
    """C3が定期的に取りに来る字幕窓。一言が変わった時だけ作り直す。"""
    st = _load()
    text = st.get("comment", "")
    if _win_cache["text"] != text or _win_cache["bin"] is None:
        if not await _ensure_font():
            raise HTTPException(status_code=503, detail="font not ready")
        _win_cache["bin"] = _render_win(text)
        _win_cache["text"] = text
    return Response(content=_win_cache["bin"], media_type="application/octet-stream")




def _known_faces() -> dict:
    """登録済みの特徴量 {匿名ID: [ベクトル,...]}。実名は一切持たない。"""
    try:
        docs = get_db().collection("faces").stream()
        return {d.id: _vec_list((d.to_dict() or {}).get("vecs", [])) for d in docs}
    except Exception as e:
        logger.warning("known faces load failed: %s", e)
        return {}


def _new_person_id() -> str:
    """匿名IDを発行（連番のみ・誰なのかは記録しない）。"""
    try:
        n = len(list(get_db().collection("faces").stream())) + 1
    except Exception:
        n = 1
    return "p%02d" % n


def _raw_gray_to_jpeg(data: bytes, w: int, h: int) -> bytes:
    """目から届いた生の白黒データ（1画素1バイト）を、扱いやすいJPEGに変換する。
    ESP側でJPEG圧縮すると十数秒かかるので、圧縮はサーバーで肩代わりする。"""
    from PIL import Image
    import io
    img = Image.frombytes("L", (w, h), data[:w * h])
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


@router.post("/arrive")
async def arrive(request: Request, raw: str = "", x_upload_key: str = Header(None)):
    """到着した人の写真を受け取り、匿名IDを返す。
    ・知っている顔 → そのID（人格ができていればキャラも返す）
    ・初めての顔   → 新しいIDを発行し「卵」を返す（人格は裏で創作）
    ・顔が読めない → unknown（代表キャラがとぼける）
    写真そのものは保存しない（較正期間中だけ latest_arrival として1枚上書き）。"""
    if UPLOAD_KEY and x_upload_key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    if not FACE_ENABLED:
        return {"person": "unknown", "state": "disabled"}
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    try:
        from server import face
        crop = face.detect_face(data, rotate=FACE_ROTATE)
        if crop is None:
            _log_event("arrive", {"person": "unknown", "why": "no_face"})
            return {"person": "unknown", "state": "no_face"}
        vec = face.embed(crop)
        if vec is None:
            return {"person": "unknown", "state": "embed_failed"}
        known = _known_faces()
        pid, sim = face.match(vec, known)
        db = get_db()
        if pid is None:                                  # 初めて見る顔
            if not _confirm_new(vec):                    # 一度きりの見え方は信用しない
                return {"person": "unknown", "state": "not_sure"}
            pid = _new_person_id()
            db.collection("faces").document(pid).set(
                {"vecs": [{"v": vec}], "born": time.time(), "persona": "", "state": "egg"})
            _log_event("arrive", {"person": pid, "state": "new_egg", "sim": round(sim, 3)})
            return {"person": pid, "state": "egg"}
        doc = db.collection("faces").document(pid).get().to_dict() or {}
        vecs = doc.get("vecs", [])
        if len(vecs) < 5:                                # 見るたび少しずつ覚え直す（眼鏡・照明差に強くする）
            vecs.append({"v": vec})
            db.collection("faces").document(pid).update({"vecs": vecs})
        state = "ready" if doc.get("persona") else "egg"
        _log_event("arrive", {"person": pid, "state": state, "sim": round(sim, 3)})
        return {"person": pid, "state": state}
    except Exception as e:
        logger.warning("arrive failed: %s", e)
        return {"person": "unknown", "state": "error"}


_BIRTH_PROMPT = """あなたは「地霊（じれい）」という小さな精霊の生みの親です。
これから生まれるのは、大学の研究室の共有キッチンで、ある一人の人にだけ会う地霊です。
その人が誰なのかは分かりません（この研究では名前を記録しないため）。
分かるのは「この場所に、この人が来る」ということだけ。

その人のための地霊を1体、世界に1つの個性として創作してください。
- 共有キッチンに宿り、場所がきれいだと嬉しく、放置されるとそわそわする性質は共通
- そこに載る固有の個性を: 口調のくせ・性格・好きなもの・小さなこだわり・感情の出し方
- テンプレ的な「元気な妖精」にしない。少し意外性のある、愛せる欠点を持つ子に
- 命令や説教は絶対にしない性格であること（この研究の憲法）
- 既にいる子と似せない（既存: フランス語かぶれで気取るが単語を間違えて照れる子）

出力: そのままAIのシステムプロンプトに使える人格記述文だけを、
「あなたは〜」で始まる300字以内の日本語で。前置きや解説は不要。"""


@router.post("/birth")
async def birth(request: Request):
    """卵のまま待っているIDに人格を吹き込む（誕生の儀式）。
    合言葉つき。引数なしなら、卵をひとつ見つけて生ませる。"""
    body = await request.json()
    if body.get("key") != NOTE_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    db = get_db()
    pid = body.get("person")
    if not pid:                                   # 指定が無ければ卵をひとつ探す
        for d in db.collection("faces").stream():
            if not (d.to_dict() or {}).get("persona"):
                pid = d.id
                break
    if not pid:
        return {"ok": False, "why": "no_egg"}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "why": "no_key"}
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        msg = await client.messages.create(
            model="claude-opus-4-8", max_tokens=500,
            messages=[{"role": "user", "content": _BIRTH_PROMPT}])
        persona = "".join(b.text for b in msg.content if b.type == "text").strip()
    except Exception as e:
        logger.warning("birth failed: %s", e)
        return {"ok": False, "why": str(e)}
    db.collection("faces").document(pid).update({"persona": persona, "state": "ready"})
    _log_event("birth", {"person": pid, "len": len(persona)})
    return {"ok": True, "person": pid, "persona": persona}


@router.get("/who", response_class=PlainTextResponse)
async def who():
    """C3が読む用。いま迎えるべき相手を1行で返す（例: 'p03 ready' / 'unknown'）。"""
    st = _load()
    return (st.get("cur_person", "unknown") + " " + st.get("cur_state", "none")) + "\n"



@router.post("/facetest")
async def facetest(request: Request, x_upload_key: str = Header(None)):
    """診断用：送った写真で顔が見つかるかだけを返す（記録も保存もしない）。"""
    if UPLOAD_KEY and x_upload_key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    data = await request.body()
    out = {"bytes": len(data), "enabled": FACE_ENABLED}
    try:
        from server import face
        out["cv2"] = True
        for rot in (0, 90, 180, 270):
            crop = face.detect_face(data, rotate=rot)
            if crop is not None:
                out["found_at_rotation"] = rot
                out["crop"] = list(crop.shape)
                vec = face.embed(crop)
                out["embed_ok"] = vec is not None
                out["vec_len"] = len(vec) if vec else 0
                break
        else:
            out["found_at_rotation"] = None
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
    return out


@router.get("/faces")
async def faces_summary():
    """登録状況の確認（特徴量そのものは返さない・件数と状態だけ）。"""
    out = []
    try:
        for d in get_db().collection("faces").stream():
            v = d.to_dict() or {}
            out.append({"id": d.id, "shots": len(v.get("vecs", [])),
                        "state": "ready" if v.get("persona") else "egg",
                        "born": v.get("born")})
    except Exception as e:
        return {"faces": [], "error": str(e)}
    return {"faces": out, "enabled": FACE_ENABLED, "last_error": _identify_err[0]}


@router.post("/faces/clear")
async def clear_faces(key: str = ""):
    """覚えた顔をすべて忘れる。

    誤検出でできたIDが混ざると、以後の照合がその分だけ狂う。
    数が少ないうちは、選んで消すより一度まっさらにするほうが確実。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    n = 0
    try:
        for d in get_db().collection("faces").stream():
            d.reference.delete()
            n += 1
    except Exception as e:
        return {"ok": False, "error": str(e)}
    st = _load()
    st["cur_person"] = None
    st["cur_state"] = None
    _save(st)
    _log_event("faces_clear", {"deleted": n})
    return {"ok": True, "deleted": n}


@router.get("/similar")
async def faces_similar():
    """発行済みのID同士がどれだけ似ているかを返す（2026-09-02）。

    人が写っている間は写真を残さない決まりなので、「このIDとこのIDは
    同じ人だったのか」を後から目で確かめることはできない。だが顔を覚えた
    数値そのものは残っているので、それ同士を比べれば写真なしで確かめられる。

    ここでも数値は外に出さない。出すのは似ている度だけ。
    1.0に近いほど同じ人、0.42が別人と判断される境目。"""
    import numpy as np
    try:
        known = _known_faces()
    except Exception as e:
        return {"pairs": [], "error": str(e)}
    ids = sorted(known)
    pairs = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            best = 0.0
            for va in known[a]:
                for vb in known[b]:
                    sc = float(np.dot(np.asarray(va, dtype=np.float32),
                                      np.asarray(vb, dtype=np.float32)))
                    best = max(best, sc)
            pairs.append({"a": a, "b": b, "similarity": round(best, 3),
                          "same_person": best >= 0.42})
    pairs.sort(key=lambda x: -x["similarity"])
    return {"pairs": pairs, "threshold": 0.42}



# ---- 日本語の声（2026-08-31）----
# クラウドで一言を音声に変換し、C3が取りに来て流す。
# C3のI2Sは 16kHz・16bit・モノラル なので、その形の生PCMで返す。
# 合成の中身は差し替え可能にしてある（質に不満が出たら別の方式へ移す）。
_voice_cache = {"text": None, "pcm": None}


def _synth_ja(text: str) -> bytes | None:
    """日本語の一言 → 16kHz/16bit/モノラルの生PCM。作れなければ None。"""
    if not text:
        return None
    try:
        import subprocess, tempfile, os as _os, wave
        with tempfile.TemporaryDirectory() as d:
            wav = _os.path.join(d, "v.wav")
            # espeak-ng: 軽く、追加費用なし。声は素朴だが日本語を読む。
            subprocess.run(
                ["espeak-ng", "-v", "ja", "-s", "150", "-p", "60", "-w", wav, text],
                check=True, timeout=20, capture_output=True)
            with wave.open(wav, "rb") as w:
                ch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
                frames = w.readframes(w.getnframes())
        import audioop
        if sw != 2:
            frames = audioop.lin2lin(frames, sw, 2)
        if ch != 1:
            frames = audioop.tomono(frames, 2, 0.5, 0.5)
        if sr != 16000:
            frames, _ = audioop.ratecv(frames, 2, 1, sr, 16000, None)
        return frames
    except Exception as e:
        logger.warning("ja synth failed: %s", e)
        _voice_cache["err"] = "%s: %s" % (type(e).__name__, str(e)[:200])
        return None


@router.get("/voice.pcm")
async def voice_pcm():
    """C3が取りに来る声。いまの一言を16kHz/16bit/モノラルの生PCMで返す。"""
    st = _load()
    text = st.get("comment", "")
    if _voice_cache["text"] != text or _voice_cache["pcm"] is None:
        pcm = _synth_ja(text)
        if pcm is None:
            raise HTTPException(status_code=503, detail=_voice_cache.get("err") or "no voice")
        _voice_cache["pcm"] = pcm
        _voice_cache["text"] = text
    return Response(content=_voice_cache["pcm"], media_type="application/octet-stream")


_PANEL = """<!doctype html><html lang=ja><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>確かめ用パネル</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#faf8f5;color:#333}
 h1{font-size:17px;margin:0 0 14px}
 h2{font-size:14px;margin:22px 0 8px;color:#666;font-weight:600}
 .card{background:#fff;border-radius:12px;padding:14px;margin-bottom:12px;
       box-shadow:0 1px 3px rgba(0,0,0,.07)}
 button{font-size:15px;padding:11px 14px;border-radius:9px;border:0;
        background:#4a7c59;color:#fff;margin:3px 3px 3px 0}
 button.off{background:#a0522d} button.gray{background:#888}
 input{font-size:15px;padding:9px;border:1px solid #ccc;border-radius:8px;width:100%;
       box-sizing:border-box;margin-bottom:8px}
 .st{font-size:14px;line-height:1.7}
 .rec{color:#c0392b;font-weight:700}
 .shots{display:grid;grid-template-columns:1fr 1fr;gap:7px}
 .shots img{width:100%;border-radius:8px;display:block}
 .shots div{font-size:11px;color:#777;margin-top:2px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td{padding:5px 3px;border-bottom:1px solid #eee}
 .same{color:#c0392b;font-weight:600}
</style>
<h1>確かめ用パネル</h1>

<div class=card>
  <input id=key placeholder="合言葉（1度入れれば覚えます）">
  <div class=st id=state>…</div>
</div>

<div class=card>
  <h2 style="margin-top:0">人の写る写真を残す</h2>
  <button onclick="rec(30)">30分</button>
  <button onclick="rec(60)">60分</button>
  <button onclick="rec(120)">120分</button>
  <button class=off onclick="rec(0)">いま止める</button>
</div>

<div class=card>
  <h2 style="margin-top:0">残っている写真</h2>
  <div class=shots id=shots></div>
  <button class=off onclick="wipe()" style="margin-top:10px">ぜんぶ消す</button>
</div>

<div class=card>
  <h2 style="margin-top:0">IDは同じ人か</h2>
  <table id=sim></table>
  <button class=gray onclick="load()">読み直す</button>
  <button class=off onclick="forget()">覚えた顔を忘れる</button>
</div>

<script>
var K=document.getElementById('key');
K.value=localStorage.getItem('k')||'';
K.onchange=function(){localStorage.setItem('k',K.value)};

function post(u){
  return fetch(u+(u.indexOf('?')<0?'?':'&')+'key='+encodeURIComponent(K.value),
               {method:'POST',headers:{'Content-Length':'0'}}).then(function(r){return r.json()});
}
function rec(m){
  if(m>0 && !confirm(m+'分のあいだ、人の写った写真を残します。よろしいですか？'))return;
  post('/spirit/verify?minutes='+m).then(function(j){
    if(j.detail){alert('合言葉がちがいます');return}
    load();
  });
}
function wipe(){
  if(!confirm('残っている写真をすべて消します。元に戻せません。'))return;
  post('/spirit/shots/clear').then(function(j){
    if(j.detail){alert('合言葉がちがいます');return}
    alert(j.deleted+'枚 消しました'); load();
  });
}
function load(){
  fetch('/spirit/shots').then(function(r){return r.json()}).then(function(j){
    document.getElementById('state').innerHTML =
      (j.recording ? '<span class=rec>記録中</span>　あと '+j.minutes_left+' 分'
                   : '記録していません')
      + '<br>残っている写真 '+j.count+' 枚';
    var h='';
    (j.shots||[]).slice(0,40).forEach(function(s){
      var n=s.name.split('/').pop().replace('.jpg','').split('_');
      var d=new Date(parseInt(n[0])*1000);
      h+='<div><img src="'+s.url+'" loading=lazy><div>'
        +('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2)+':'
        +('0'+d.getSeconds()).slice(-2)+'　'+(n[1]||'')+'</div></div>';
    });
    document.getElementById('shots').innerHTML = h || '<div style="color:#999">まだありません</div>';
  });
  fetch('/spirit/similar').then(function(r){return r.json()}).then(function(j){
    var h='';
    (j.pairs||[]).forEach(function(pp){
      h+='<tr><td>'+pp.a+' と '+pp.b+'</td><td>'+pp.similarity+'</td><td'
        +(pp.same_person?' class=same>同じ人':'>別人')+'</td></tr>';
    });
    document.getElementById('sim').innerHTML = h || '<tr><td>まだIDがありません</td></tr>';
  });
}
load(); setInterval(load, 20000);
</script></html>"""


@router.get("/panel", response_class=HTMLResponse)
async def panel_page():
    """出先から確かめを操作するページ（2026-09-02）。

    記録の入切と消去はcurlでしか叩けず、外に出ていると手が出せなかった。
    人の写る写真を扱う操作こそ、その場ですぐ止められる必要がある。"""
    return _PANEL


_COMPARE_SYSTEM = (
    "あなたは同じ場所を2枚の写真で見比べる係です。"
    "1枚目が『前』、2枚目が『後』。同じカメラ・同じ向きで撮られています。\n"
    "【最も大事な掟】変わっていないなら、変わっていないと言う。"
    "何か答えなければと思って、ありもしない変化を作らないこと。"
    "光の当たり方・影・画質のちらつき・撮る角度のわずかな差は変化ではない。\n"
    "【何を変化とみなすか】物が増えた・減った・別の場所へ移った、それだけ。\n"
    "【書き方】changesは各項目 {\"what\":\"もの\", \"how\":\"増えた|減った|移った\", "
    "\"where\":\"場所\", \"note\":\"ひとこと\"}。多くても5個。"
    "確信が持てないものは書かない。\n"
    "必ずJSONだけを1行で返す: "
    "{\"same\": 変化なしならtrue, \"changes\": [...], "
    "\"better\": 片づいた方向ならtrue・散らかった方向ならfalse・どちらでもなければnull}"
)


async def _compare_images(a: bytes, b: bytes, focus: str = "") -> dict:
    """同じ場所の前後2枚を見比べて、何が変わったかを返す。

    focus＝「シンク」のように見るべき場所の名前。切り出さずに名前で絞る。
    座標で切り出す方式は、区画の座標そのものが当てにならないので避けたい
    （AIに区画を描かせたら、調理台として床を、棚として窓を囲った）。

    一覧を2回作って引き算する方法は、誰も居ない台所でも欄の67%が動いて
    使いものにならなかった（2026-09-02実測）。数を言い当てるのは難しいが、
    2枚を並べて違いを探すのはずっとやさしい。人間も同じ。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"error": "no api key"}
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()

        def img(d):
            return {"type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg",
                               "data": base64.standard_b64encode(d).decode()}}

        ask = "違いをJSONで。無ければsameだけtrueに。"
        if focus:
            ask = ("写真の中の「" + focus + "」のあたりだけを見比べてください。"
                   "そこを拡大したつもりで、隅から隅まで一つずつ照らし合わせる。"
                   "それ以外の場所の違いは無視する。" + ask)
        msg = await client.messages.create(
            model=MODEL, max_tokens=700, system=_COMPARE_SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "前:"}, img(a),
                {"type": "text", "text": "後:"}, img(b),
                {"type": "text", "text": ask},
            ]}])
        text = "".join(x.text for x in msg.content if x.type == "text")
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            return {"error": "not json: " + text[:160]}
        return json.loads(text[i:j + 1])
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, str(e)[:200])}


@router.post("/compare")
async def compare(before: UploadFile = File(...), after: UploadFile = File(...),
                  focus: str = "", x_upload_key: str = Header(None)):
    """前後2枚を見比べる（試験用の窓口）。記録も保存もしない。
    focusに場所の名前を渡すと、そこだけを見比べる。"""
    if UPLOAD_KEY and x_upload_key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    a, b = await before.read(), await after.read()
    return await _compare_images(a, b, focus)


_ZONE_SYSTEM = (
    "あなたは台所の写真を見て、物が置かれる場所を区画に分ける係です。\n"
    "【区画の選び方】人がそこに物を置いたり片づけたりする面だけを選ぶ。"
    "壁・天井・窓の外・冷蔵庫の扉のような、物が乗らない面は選ばない。"
    "3〜6個。互いに重ならないようにする。\n"
    "【座標】写真の左上を(0,0)、右下を(1,1)とした割合で答える。"
    "boxは[左, 上, 右, 下]。その面がぜんぶ入るよう、少し広めに取る。\n"
    "【名前】その面の呼び名を短い日本語で（シンク・コンロ・調理台・棚・床・テーブル など）。\n"
    "必ずJSONだけを1行で返す: "
    "{\"zones\": [{\"name\":\"名前\", \"box\":[0.1,0.2,0.3,0.4], "
    "\"why\":\"そこを選んだ理由\"}]}"
)


@router.post("/mapzones")
async def map_zones(request: Request, x_upload_key: str = Header(None)):
    """写真を見て、見張るべき区画を自分で割り出す（試験用）。

    区画を人が手で決めると、カメラの向きを変えるたびに測り直しになる。
    写真から起こせるなら、置き直しにも付いていける。"""
    if UPLOAD_KEY and x_upload_key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"error": "no api key"}
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        msg = await client.messages.create(
            model=MODEL, max_tokens=900, system=_ZONE_SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/jpeg",
                 "data": base64.standard_b64encode(data).decode()}},
                {"type": "text", "text": "この台所の区画をJSONで。"},
            ]}])
        text = "".join(x.text for x in msg.content if x.type == "text")
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            return {"error": "not json: " + text[:200]}
        return json.loads(text[i:j + 1])
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, str(e)[:200])}


# ---- 区画の自動運用（2026-09-02）----
# 区画を人が決めると、カメラを動かすたびに決め直しになる。かといって
# AIに座標を描かせたら、調理台として床を、棚として窓を囲った。
# 見えてはいるが、どこにあるかは言えない。
#
# そこで座標を捨て、名前だけを使う。「シンクのあたりだけ見比べて」と
# 名前で頼めば、切り出したのと同じだけ当たることを実測で確かめた。
#
# 良し悪しの判定も自分でやる。誰も来ていない時間帯の前後を比べて出た
# 「変化」は、定義上すべて誤報である。それを数えれば、どの区画が
# 信用できるかは人が決めなくても分かる。
BASELINE_OBJ = "spirit/zonecheck/baseline.jpg"
VISIT_END_GAP = 90.0        # 最後に人を見てからこれだけ経てば「去った」
# 誰も来ないときの自己点検の間隔。1回につき1区画しか見ないので、
# 5区画あれば1周に2時間半かかる。判定に必要な6回分を貯めるには
# ここが2時間だと3日近くかかってしまうため、30分に詰めてある。
# 点検1回はAIへの問い合わせ1回きりで、1日48回にしかならない。
IDLE_CHECK_GAP = 1800.0
ZONE_MIN_TRIALS = 6         # これだけ試すまでは見送りにしない
ZONE_MAX_FALSE = 0.4        # 誤報がこの割合を超えたら見送り


def _live_zones(st: dict) -> list:
    return [z for z in st.get("zones", []) if z.get("state") != "見送り"]


async def _derive_zones(data: bytes) -> list:
    """画角を見て、見張るべき区画の名前を起こす。座標は受け取らない。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return []
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        msg = await client.messages.create(
            model=MODEL, max_tokens=500, system=_ZONE_SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/jpeg",
                 "data": base64.standard_b64encode(data).decode()}},
                {"type": "text", "text": "この台所の区画をJSONで。"},
            ]}])
        text = "".join(x.text for x in msg.content if x.type == "text")
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            return []
        zs = json.loads(text[i:j + 1]).get("zones") or []
        out = []
        for z in zs[:6]:
            nm = (z.get("name") or "").strip()
            if nm and nm not in [o["name"] for o in out]:
                out.append({"name": nm, "trials": 0, "hits": 0,
                            "false": 0, "state": "試用中"})
        return out
    except Exception as e:
        logger.warning("derive zones failed: %s", e)
        return []


def _score_zone(z: dict) -> None:
    """試した数と誤報の数から、その区画を続けるか決める。"""
    if z["trials"] < ZONE_MIN_TRIALS:
        z["state"] = "試用中"
        return
    rate = z["false"] / max(1, z["trials"])
    z["state"] = "見送り" if rate > ZONE_MAX_FALSE else "採用"


async def _zone_pass(st: dict, before: bytes, after: bytes,
                     who: list, quiet: bool) -> None:
    """区画ごとに前後を見比べて、結果を記録する。

    quiet＝この間、誰も来ていない。そこで出た変化はすべて誤報とみなす。"""
    zones = _live_zones(st)
    if quiet:                                  # 点検は1区画ずつ順ぐりに（費用のため）
        i = st.get("zone_rotate", 0) % max(1, len(zones))
        zones = zones[i:i + 1]
        st["zone_rotate"] = i + 1
    for z in zones:
        r = await _compare_images(before, after, z["name"])
        if r.get("error"):
            logger.warning("zone compare failed (%s): %s", z["name"], r["error"])
            continue
        changed = not r.get("same")
        z["trials"] = z.get("trials", 0) + 1
        if quiet:
            if changed:
                z["false"] = z.get("false", 0) + 1
        elif changed:
            z["hits"] = z.get("hits", 0) + 1
        _score_zone(z)
        if changed:
            _log_event("zone", {"zone": z["name"], "who": who,
                                "quiet": quiet, "better": r.get("better"),
                                "changes": r.get("changes") or []})
    _save(st)


_zone_busy = [False]       # 突き合わせが二重に走らないようにする札


async def _zone_cycle_bg(st: dict, data: bytes, now: float) -> None:
    """裏で突き合わせを回し、終わったら札を下ろす。"""
    try:
        await _zone_cycle(st, data, now)
    finally:
        _zone_busy[0] = False


async def _zone_cycle(st: dict, data: bytes, now: float) -> None:
    """人が去った直後、または長く静かなときに、前後を突き合わせる。

    「前」は最後に無人と確かめた1枚。「後」はいま届いた1枚。
    突き合わせが済んだら、いまの1枚が次の「前」になる。"""
    try:
        if not st.get("zones"):
            st["zones"] = await _derive_zones(data)
            if st["zones"]:
                _log_event("zones_set", {"zones": [z["name"] for z in st["zones"]]})
        base = read_object(BASELINE_OBJ)
        if base is None:                       # まだ「前」が無い→いまの1枚を置く
            upload_to(BASELINE_OBJ, data, "image/jpeg")
            st["baseline_at"] = now
            _save(st)
            return
        who = st.get("visit_people") or []
        quiet = not who
        if quiet and now - st.get("baseline_at", 0) < IDLE_CHECK_GAP:
            return                             # 静かな時は、そう何度も点検しない
        await _zone_pass(st, base, data, who, quiet)
        upload_to(BASELINE_OBJ, data, "image/jpeg")
        st["baseline_at"] = now
        st["visit_people"] = []
        _save(st)
    except Exception as e:
        logger.warning("zone cycle failed: %s", e)


@router.get("/zones")
async def zones_status():
    """区画の一覧と、それぞれの信用度。"""
    st = _load()
    out = []
    for z in st.get("zones", []):
        t = z.get("trials", 0)
        out.append({"name": z["name"], "state": z.get("state"),
                    "trials": t, "hits": z.get("hits", 0),
                    "false_alarms": z.get("false", 0),
                    "false_rate": round(z.get("false", 0) / t, 2) if t else None})
    return {"zones": out, "baseline_age": round(time.time() - st.get("baseline_at", 0))
            if st.get("baseline_at") else None}


@router.post("/zones/refresh")
async def zones_refresh(key: str = ""):
    """画角を変えたときに、区画を立て直す。成績もやり直す。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    data = read_object("spirit/latest.jpg")
    if data is None:
        return {"ok": False, "error": "まだ写真がありません"}
    st = _load()
    st["zones"] = await _derive_zones(data)
    st["zone_rotate"] = 0
    _save(st)
    _log_event("zones_set", {"zones": [z["name"] for z in st["zones"]]})
    return {"ok": True, "zones": [z["name"] for z in st["zones"]]}


@router.get("/log")
async def get_log(limit: int = 200):
    """研究データの取り出し口（judge/care/presenceの時系列）。"""
    try:
        docs = get_db().collection("spirit_log").order_by(
            "t", direction="DESCENDING").limit(min(limit, 1000)).stream()
        return {"events": [d.to_dict() for d in docs]}
    except Exception as e:
        return {"events": [], "error": str(e)}
