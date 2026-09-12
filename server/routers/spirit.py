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
import sys
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
    "誰が何をしているかの描写は禁止（記録に残すのは物の状態だけ）。"
    "人だと言えるのは、頭と胴体がはっきり見えているときだけ。"
    "椅子・クッション・かけてある服・カバン・人形・影・木目は人ではない。"
    "迷ったら0にする。居ない人を数える方が、見落とすよりずっと困る"
    "（実際に、誰も居ない床の写真に『1人』と答えた）。\n"
    "【scoreの定義】score は散らかり度。0.0=完全にきれい、0.3=少し物がある、"
    "0.6=それなりに散らかっている、1.0=ひどく散らかっている。"
    "きれいなほど0に近い。間違えないこと。\n"
    "【備え付け】ステンレスの水切りかご・壁の包丁立てと包丁・壁のフックに掛かっている道具・"
    "蛇口・排水口の網は備え付けで、物として挙げず、散らかりにも数えない。"
    "このキッチンに食洗機・食器乾燥機は無い（水切りかごを見間違えない）。\n"
    "【commentの掟】地霊が自分の気持ちをつぶやく独り言だけ。"
    "口調は、ちいさな子どものひとりごと（ひらがな多め。『あのね』『〜なあ』『〜かなあ』『〜だね』）。"
    "ていねい語や、『あら』『〜わ』『〜ですわ』のような大人の口調・店員の口調は使わない。"
    "人格の設定に別の口調が書いてあっても、こちらを優先する。"
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

FACE_ROTATE = 180        # カメラの取り付け向きの補正。待つ向き(-0.50_0.30)では人が逆さに写る（2026-09-10 実測：切り抜き15枚が全部逆さ）
FACE_ENABLED = os.environ.get("FACE_ENABLED", "") == "1"   # 掲示が済むまでは既定でオフ

_identify_err = [""]   # 顔検出の失敗理由（/spirit/facesで確認する）
# 人が写る写真を一時的に残す置き場（2026-09-02・研究室の承諾のもと）。
# 通常は残さない決まりだが、顔の分裂などは写真がないと詰められない。
# ・専用の置き場にまとめる（他の写真と混ざらないので、まとめて消せる）
# ・期限つきでしか入らない（消し忘れではなく、切り忘れが一番こわい）
VERIFY_PREFIX = "spirit/verify/"
VERIFY_GAP = 10.0      # 同じ滞在で撮りすぎないための間隔（秒）

# 顔の切り抜きだけを集める置き場（2026-09-06）。
#
# 角度によって何がどう変わるかを、推測ではなく実物で確かめるために作った。
# 全画面の写真だと1枚400KBあり、10秒に1枚が限度で、しかも顔を探し直す
# 手間がかかる。切り抜きなら15KB前後なので、細かく残せる。
#
# 弾いた顔も残す。ここが肝心で、いまは帯から外れた顔がその場で消えるため
# 「弾きすぎていないか」を確かめる術がない。
#
# 測った値はファイル名に入れる。あとで名前を読むだけで分布が出る:
#   <時刻>_<幅>px_r<起き具合×100>_<up|dn>_<結びついたID>.jpg
FACES_PREFIX = "spirit/faces_raw/"
COLLECT_GAP = 1.5      # 集める間隔（秒）
_last_collect = [0.0]


def _collect_face(f: dict, pid: str = "", sim=None) -> None:
    """顔の切り抜きを1枚、測った値つきで残す。期間中だけ。

    人の写る写真を残す窓は期限式で、切り忘れが一番まずいという考えは
    ここでも同じ。verify の期限をそのまま使う。"""
    st = _load()
    now = time.time()
    if now >= st.get("verify_until", 0):
        return
    if now - _last_collect[0] < COLLECT_GAP:
        return
    _last_collect[0] = now
    try:
        import cv2
        ok, buf = cv2.imencode(".jpg", f["crop"],
                               [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            return
        r = f.get("ratio")
        name = FACES_PREFIX + "%d_%dpx_r%s_%s%s_%s.jpg" % (
            int(now), f.get("px", 0),
            ("%03d" % round(r * 100)) if r else "___",
            "up" if f.get("up") else "dn",
            "_edge" if f.get("edge") else "",
            pid or "unknown")
        upload_to(name, buf.tobytes(), "image/jpeg")
    except Exception as e:
        logger.warning("collect face failed: %s", e)

_judge_err = [""]      # 判断の失敗理由（/spirit/fullで確認する）
                       # 判断が黙って失敗すると、古い一言が残り続けるだけで
                       # 表からは動いているように見える。実際に40時間気づけなかった。
_state_cache: dict | None = None   # Firestore読み書き削減用（同一インスタンス内）


def _doc():
    return get_db().collection("spirit").document("state")


_pending = []      # まだIDを与えていない「知らない顔」の心当たり [(特徴量, 時刻, 位置)]


def _moved(a, b, px: int) -> bool:
    """2つの位置は、顔の大きさから見て「動いた」と言えるほど離れているか。

    人の頭は数秒あれば顔の幅の半分くらいは動く。置いてある物は動かない。
    土鍋の位置は1分のあいだ17pxしかぶれなかった（顔の幅は180px）。"""
    if not a or not b:
        return False
    return max(abs(a[0] - b[0]), abs(a[1] - b[1])) >= MOVE_MIN * max(px, 1)


def _confirm_new(vec: list, pos=None, px: int = 0) -> bool:
    """知らない顔にIDを出してよいかを決める（2026-09-02 / 2026-09-04改訂）。

    1コマ見ただけで卵を作っていたため、1時間で8つのIDが生まれた。
    うち少なくとも1つは、誰も居ない台所の棚を顔と見た誤検出だった。

    誤検出はその場限りのゴミなので、続けて似た顔がもう一度出ることはない。
    そこで「知らない顔を、短い間に2回、しかも互いに似た形で見た」ときだけ
    新しいIDを出す。本物の人はカメラの前に数秒は留まるので、この条件を通る。

    心当たりを1つしか覚えていなかった頃は、初対面が2人同時に写ると
    互いを上書きし合い、別人どうしを見比べて、どちらも卵になれなかった。
    数人ぶん覚えておき、自分に似た心当たりだけを探す。"""
    import numpy as np
    now = time.time()
    v = np.asarray(vec, dtype=np.float32)
    _pending[:] = [x for x in _pending if now - x[1] <= 30][-6:]
    for i, (prev, _t, ppos) in enumerate(_pending):
        sim = float(np.dot(v, np.asarray(prev, dtype=np.float32)))
        if sim >= 0.42:                              # 同じ顔をもう一度見た
            _pending.pop(i)
            return True
    _pending.append((vec, now, pos))                 # 心当たりとして覚えておく
    return False


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


_last_small = [0.0]    # 「顔が小さい」を最後に記録した時刻


def _log_small(why: str, px: int, **extra):
    """顔が小さすぎた、を記録する。ただし間引く。

    人が3秒おきに写りつづける間ずっと書くと、10分の滞在で200件になり、
    肝心の出来事がその中に埋もれる。30秒に1件で、傾向は十分に読める。"""
    now = time.time()
    if now - _last_small[0] < 30:
        return
    _last_small[0] = now
    _log_event("arrive", {"person": "unknown", "why": why, "px": px, **extra})


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


def _shrink_for_judge(data: bytes, max_w: int = 1280) -> bytes:
    """AIに見せる前に写真を小さくする。

    人が動いている間は2304幅の写真が届く。散らかり具合を見るのに
    その細かさは要らず、そのまま渡すと1枚あたりの費用が3倍になる。
    顔の照合は元の大きさのままで行うので、ここで縮めても影響はない。"""
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.shape[1] <= max_w:
            return data
        h = int(img.shape[0] * max_w / img.shape[1])
        img = cv2.resize(img, (max_w, h), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return buf.tobytes() if ok else data
    except Exception as e:
        logger.warning("shrink failed: %s", e)
        return data


async def _judge_image(image_bytes: bytes, persona: str = "") -> dict:
    """写真をClaudeに直接見せて {score, comment} か {skip} を得る。失敗は {}。
    persona＝そのキャラの人格。ルール部（_SYSTEM）は人格に関わらず常に適用。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _judge_err[0] = "ANTHROPIC_API_KEY が設定されていない"
        return {}
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        b64 = base64.standard_b64encode(_shrink_for_judge(image_bytes)).decode()
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


# 動かない「顔」は、置いてある物である。
#
# 2026-09-05、テーブルの土鍋が幅180〜200pxの顔と判定され、新しい人として
# 登録された（p02）。一晩で28回「その人が来た」ことになっていた。
#
# 新しいIDを出す条件は「短い間に2回、似た顔を見たら本物」だった。根拠は
# 「誤検出はその場限りのゴミなので、続けて同じ顔は出ない」。置いてある物には
# この理屈がそのまま逆に働く。土鍋は何百回でも同じ顔を出しつづける。
#
# 人の頭は、じっとしていても1分のうちには顔の幅ぶんくらい動く。動かない場所は
# 物として、照合からも新規発行からも外す。カメラが固定なので成り立つ。
_spots = []              # [[x, y, 最初に見た時刻, 最後に見た時刻, 回数], ...]
SPOT_NEAR = 0.35         # 顔の幅に対してこの割合より近ければ「同じ場所」
SPOT_MIN_N = 5           # この回数以上
SPOT_MIN_SEC = 60.0      # この時間ずっと同じ場所なら、それは物
SPOT_FORGET = 900.0      # 15分見かけなければ忘れる（片づけられたかもしれない）


def _is_furniture(pos, px: int) -> bool:
    """この場所の「顔」は、動かない物か。

    同じ場所に居つづけた回数と時間だけで決める。見た目は使わない
    （土鍋どうしの一致度は0.795で、人の顔どうしの0.399より高かった。
    見た目で「物らしさ」を測ろうとすると、物のほうが人らしく見える）。"""
    if not pos:
        return False
    now = time.time()
    _spots[:] = [s for s in _spots if now - s[3] <= SPOT_FORGET][-40:]
    near = SPOT_NEAR * max(px, 1)
    for s in _spots:
        if abs(pos[0] - s[0]) <= near and abs(pos[1] - s[1]) <= near:
            s[3], s[4] = now, s[4] + 1
            return s[4] >= SPOT_MIN_N and now - s[2] >= SPOT_MIN_SEC
    _spots.append([pos[0], pos[1], now, now, 1])
    return False


# 直近の顔を数枚ためておく器。1枚で「誰か」を決めるのをやめるため。
# 実測（2026-09-05）で、1枚あたりの一致度は同じ人でも0.09〜0.52に散った。
# 数枚の平均をとれば、1枚ごとの当たり外れは打ち消し合う。
_face_buf = []            # [(特徴量, 顔の幅, 時刻, 位置), ...]
MOVE_MIN = 0.5           # 顔の幅に対して、これだけ離れたら「動いた」
FACE_BUF_SEC = 25.0       # これより古い顔は忘れる（別の人が来ているかもしれない）
FACE_BUF_MAX = 8          # ためる枚数の上限
FACE_BUF_MIN_NEW = 2      # 新しいIDを出すのに、最低これだけの枚数が要る
# ただし、十分に大きく起きた顔なら1枚で足りる。
# 実測（同一人物・正面・200px以上）で、本人を認める91%・
# 他人を誤る0%。一方で、大きい写真は1枚送るのに3.9秒かかり、
# 人は数秒で通り過ぎるので、そもそも1枚しか撮れない。
# 279px・259px・155pxの正面顔が3つ、枚数だけを理由に捨てられていた
# （2026-09-07）。130pxの顔三枚より、279pxの正面顔一枚のほうが確か。
FACE_SOLO_PX = 200
FACE_MEMORY = 8           # 1人につきおぼえる見え方の枚数（2026-09-12：5→8）
FACE_SAME_TRACK = 0.35    # 直前の数秒のコマのうち、これ以上似ていれば「同じ顔」として束ねる
# 1コマでは名前を呼ばない（2026-09-12 夜）。
# 22:38、別の人が sim 0.318 で p02 と呼ばれた。境目0.30のすぐ上。
# そこで線を上げてみたが、実測では効かない——1コマ判定の「別人と間違える」は
#   線0.30 → 8.6% ／ 0.40 → 8.4% ／ 0.55 → 7.9%
# ほとんど下がらず、本人を当てる率だけ 88.9%→42.8% に落ちる。
# 間違えるときは高い一致度で間違えているので、線では止まらない。
# 2コマまとめれば 別人1.4%・本人96.6%。だから1コマのときは決めない。
# face.py 冒頭の原則「分からない時は分からないと答える」に従う。
FACE_MIN_FRAMES = 2
# 覚えの8本は「違う見え方」で埋める（2026-09-12 夜）。
# 22:01、本人が自分の p01 と 0.131 しか合わず、新しいIDになった。p01 の8本は
# 全部 14:53〜15:31 の38分間で埋まっていて、昼の光の顔しか持っていなかった。
# 時間帯を1つ伏せて測ると（仕分け済み・6時間帯ある2人）：
#   来た順・満杯で打ち止め（いまの作り） 中央0.354 ／ 結べない 43%
#   似すぎなら入れず、違えば一番かぶった1本と交換  中央0.439 ／ 結べない 13%
# 「時間帯をばらす」だけでは効かない（43%のまま）。効くのは見え方の違い。
FACE_SAME_LOOK = 0.70     # これ以上似た覚えを既に持っていたら、入れない
# 「人ではないもの」の覚え（2026-09-12 夜）。鍋・五徳・棚を顔と見てしまうのは
# 顔認識では解けない——重いモデルほどひどく、glint360k_r100 は24枚中23枚を
# 人に結びつけた。代わりに「これは人ではない」を覚えておいて弾く。
# 仕分け済みの実測（鍋を8枚おぼえた場合・線0.45）：
#   覚えていない鍋を弾ける 73.5% ／ Tシャツのプリントを巻き込む 0.0%
#   **人を誤って弾く 0.2%**（線を0.30まで下げると3.2%に跳ねるので下げない）
JUNK_SIM = 0.45
_junk_cache = [0.0, []]   # (取り直した時刻, 特徴量たち)
JUNK_TTL = 300.0


def _junk_vecs() -> list:
    """「人ではないもの」の覚えを読む。5分だけ手元に持つ。"""
    now = time.time()
    if now - _junk_cache[0] < JUNK_TTL:
        return _junk_cache[1]
    out = []
    try:
        for d in get_db().collection("notfaces").stream():
            # 顔の側と同じ形（[{"v": 特徴量}, ...]）。Firestore は配列の中に
            # 配列を置けないので、必ず1段くるむ（2026-09-12 夜、ここで500を出した）。
            out.extend([v.get("v") for v in ((d.to_dict() or {}).get("vecs") or [])
                        if isinstance(v, dict) and v.get("v")])
    except Exception as e:
        logger.warning("junk read failed: %s", e)
        _log_event("junk_error", {"text": ("%s: %s" % (type(e).__name__, e))[:120]})
        return _junk_cache[1]
    _junk_cache[0], _junk_cache[1] = now, out
    return out


def _looks_like_object(vec: list) -> float:
    """覚えている「人ではないもの」に、どれだけ似ているか。"""
    import numpy as np
    js = _junk_vecs()
    if not js:
        return 0.0
    v = np.asarray(vec, dtype=np.float32)
    best = 0.0
    for w in js:
        a = np.asarray(w, dtype=np.float32)
        if a.shape == v.shape:
            best = max(best, float(np.dot(v, a)))
    return best


def _blend(vecs: list) -> list:
    """特徴量を平均して、長さを1に揃え直す。"""
    import numpy as np
    m = np.mean([np.asarray(v, dtype=np.float32) for v in vecs], axis=0)
    return [float(x) for x in (m / (float(np.linalg.norm(m)) + 1e-9))]


def _remember_face(vec: list, px: int, pos=None) -> tuple:
    """この顔をためて、「同じ顔」だけを選んで返す。

    返すのは (同じ顔のコマたち, その枚数, 一番大きく写った幅,
    位置がどれだけ広がったか)。人が数秒おきに写るので、10秒立っていれば
    2〜4枚たまる。位置の広がりは「本当に動いたか」を測るために使う。

    2026-09-12 夜：以前は「1人しか写っていないとき」しかためられなかった。
    台所に2人いると毎回1コマで決めることになり、一番効くはずの
    「2コマまとめる」（別人の取り違え 7.8%→1.2%）がほとんど働かなかった
    ——実測：この日の26回のうち25回が1コマ判定。

    人数で分けるのをやめ、**似ている顔だけを束ねる**ようにした。
    仕分け済み710枚の実測（25秒以内に写った顔どうし）：
      同じ人 1651組  中央 0.580
      別人      5組  最大 0.140
    0.35 で切ると、同じ人の77.7%を拾って、別人は1組も巻き込まない。"""
    import numpy as np
    now = time.time()
    _face_buf[:] = [x for x in _face_buf if now - x[2] <= FACE_BUF_SEC][-(FACE_BUF_MAX * 3):]
    _face_buf.append((vec, px, now, pos))
    v = np.asarray(vec, dtype=np.float32)
    mine = []
    for x in _face_buf:
        w = np.asarray(x[0], dtype=np.float32)
        if w.shape == v.shape and float(np.dot(v, w)) >= FACE_SAME_TRACK:
            mine.append(x)
    mine = mine[-FACE_BUF_MAX:]
    ps = [x[3] for x in mine if x[3]]
    spread = 0
    for i in range(len(ps)):
        for j in range(i + 1, len(ps)):
            spread = max(spread, abs(ps[i][0] - ps[j][0]), abs(ps[i][1] - ps[j][1]))
    # コマはそのまま返す。照合の側（face.match_frames）で「人ごとに一番似た覚え」を
    # 出してからコマ全体で平均する。
    return ([x[0] for x in mine], len(mine),
            max(x[1] for x in mine), spread)


_ms_count = [0]


def _log_ms(kind: str, ms: dict, nbytes: int) -> None:
    """1コマの処理時間の内訳を、10コマに1回だけ記録に残す（2026-09-12 夜）。

    毎コマ書くと記録が時間の話で埋まる。傾向が見たいだけなので間引く。"""
    _ms_count[0] += 1
    if _ms_count[0] % 10 == 1:
        _log_event("frame_ms", dict(ms, kind=kind, kb=round(nbytes / 1024),
                                    total=sum(ms.values())))


def _refresh_memory(vecs: list, vec: list):
    """覚えが満杯のとき、新しい見え方を入れるべきか決める。

    入れるなら、一番かぶっている1本を捨てた新しい並びを返す。
    入れないなら None（もう似た見え方を持っている）。"""
    import numpy as np
    try:
        M = np.asarray([v["v"] for v in vecs], dtype=np.float32)
        v = np.asarray(vec, dtype=np.float32)
        if M.shape[1] != v.shape[0]:
            return None
        if float((M @ v).max()) >= FACE_SAME_LOOK:
            return None                       # もう似た見え方がある
        S = M @ M.T
        np.fill_diagonal(S, -1.0)
        drop = int(np.argmax(S.max(axis=1)))  # 他のどれかと一番似ている1本
        out = [x for i, x in enumerate(vecs) if i != drop]
        return out + [{"v": vec}]
    except Exception as e:
        logger.warning("memory refresh failed: %s", e)
        return None


def _identify(data: bytes):
    """写真から顔を探して匿名IDに結びつける。顔が無ければ None。
    実名は扱わない。初めての顔には新しい匿名IDを発行して「卵」にする。

    顔は見えたのに小さすぎて誰とも結べなかった時は、person を空のまま
    px（一番大きく写った顔の幅）だけ返す。呼ぶ側はそれを合図に、
    その場で大きく撮り直させる。"""
    from server import face
    # 向きは橋渡しの段階で正しく直してから届くので、1通りだけ見る。
    # 5通り試していた頃は、そのぶん誤検出の機会も5倍あった。
    found = face.detect_faces(data, rotate=FACE_ROTATE)
    if not found:
        return None
    px = max(f["px"] for f in found)
    # 弾く前に1枚ずつ残す。IDは決まる前なので、このあと結果が出てから書き足す
    # （2026-09-12：798枚ぜんぶ unknown で、あとから誰の顔か追えなかった）。
    # 人数では分けない。似ている顔だけを束ねるので、2人写っていても
    # それぞれの顔が別々にまとまる（2026-09-12 夜）。
    people = []
    for f in found:
        r = _identify_one(f["crop"], f["px"], f.get("edge"), f.get("pos"),
                          f.get("up"), f.get("ratio"), f.get("pts"), f.get("front"))
        _collect_face(f, (r or {}).get("person", ""))
        if r:
            people.append(r)
    if not people:
        return {"person": None, "px": px}
    # 先頭＝一番大きく写っている人。いま目の前に居る相手として扱う。
    head = people[0]
    return {"person": head["person"], "state": head["state"], "px": px,
            "all": [x["person"] for x in people]}


def _identify_one(crop, px: int, edge: bool = False, pos=None,
                  up: bool = True, ratio=None, pts=None, front: bool = True):
    """切り抜き1つを匿名IDに結びつける。

    この1枚だけでは決めない。直近25秒ぶんの顔から「同じ顔」だけを束ね、
    まとめて照合する。記録には「1枚だけで決めた場合の値(sim1)」も残すので、
    束ねたことが効いたか後で測れる。"""
    from server import face
    if edge:
        # 画面の端で切れた顔。写っていない半分は読めないので、
        # 誰かを決めるには使わない。人が居る合図には使う。
        _log_small("cut_off", px)
        return None
    if not face.big_enough_to_match(px):
        # 照合できる大きさではない。ここで誰かを決めると別人に結びつく。
        # 「顔は見えた」ことだけ残して、呼ぶ側に撮り直しを任せる。
        _log_small("too_small_to_match", px)
        return None
    if not up:
        # 大きく傾いた顔（起き具合1.70超）。誰かを決めるのにも、覚えるのにも使わない。
        # 記憶に混ぜると、そのIDが誰でも吸い込む網になる（2026-09-05の実測）。
        # 「人が居る」の合図としては、このあとも変わらず使われる。
        _log_small("looking_down", px, ratio=round(ratio, 2) if ratio else None)
        return None
    if _is_furniture(pos, px):
        # 同じ場所から動かない「顔」。置いてある物なので、
        # 誰かを決めるのにも、新しいIDを出すのにも使わない。
        _log_small("furniture", px)
        return None
    one = face.embed(crop, pts)
    if one is None:
        return None
    known = _known_faces()
    _, sim1 = face.match(one, known)                 # 1枚だけで決めた場合の値（比べる用）
    junk = _looks_like_object(one)
    if junk >= JUNK_SIM:
        # 覚えている「人ではないもの」（鍋・五徳・棚）とよく似ている。
        # 人が居る合図には使うが、誰かを決めるのにも覚えるのにも使わない。
        _log_small("looks_like_object", px, sim=round(junk, 3))
        return None
    frames, n, best_px, spread = _remember_face(one, px, pos)
    if n < FACE_MIN_FRAMES and known:
        # まだ1コマしか無い。人が居ることは確かなので、そう伝えるだけにして、
        # 誰かは決めない（次のコマが届けば2枚揃って決まる・数秒後）。
        _log_small("one_frame", px, sim=round(sim1, 3))
        return None
    pid, sim = face.match_frames(frames, known)
    vec = one                                        # 覚えに足すのは、いまの1枚
    note = {"sim": round(sim, 3), "sim1": round(sim1, 3), "n": n, "px": best_px}
    db = get_db()
    if pid is None:                                  # 初めて見る顔
        if not front:
            # 照合には使えるが、新しいIDを出すには傾きすぎ（1.30〜1.70）。
            # 傾いた顔から卵を作ると、そのIDが誰でも吸い込む網になる。
            _log_small("not_front", px, ratio=round(ratio, 2) if ratio else None)
            return None
        if not face.big_enough_to_enroll(best_px):
            # 小さく写った顔からは卵を作らない。同じ人でも一致度が下がり、
            # 知っている人の隣に新しいIDが並んでしまう（2026-09-03に発生）。
            _log_small("too_small", best_px, sim=round(sim, 3), sim1=round(sim1, 3), n=n)
            return None
        # 動いたところを見ていないものにIDは出さない。
        # 「短い間に2回同じ顔を見たら本物」は、置いてある物には通じない。
        # 土鍋は何百回でも同じ顔を出し、そのまま人として登録された。
        # 動いたかどうかは問わない（2026-09-06に撤回）。
        # 土鍋を防ぐために入れた条件だったが、顔の起き具合だけで土鍋は
        # 15/15すべて弾けると分かった（土鍋0.80〜0.95・人の正面1.03〜1.22）。
        # 一方この条件は、カメラをじっと見ている人——いちばん識別したい
        # 相手——を弾いていた（実測：240pxの正面顔が did_not_move で流れた）。
        # 残る守りは、起きた顔であること・120px以上・数枚そろうこと、
        # そして同じ場所に居つづける「顔」を物として外す仕組み。
        # 2026-09-12 夜：大きく写っていれば1コマで卵を作ってよい、という抜け道を
        # 塞いだ。22:01、本人が278pxで写り、自分の p01 と 0.131 しか合わずに
        # p03 として登録された。1コマで決めないという決まりは、照合だけでなく
        # 登録にも要る。大きさは「小さい顔から作らない」の役だけに戻す。
        if n < FACE_BUF_MIN_NEW and not _confirm_new(vec, pos, px):
            _log_small("not_enough", best_px, n=n)
            return None
        pid = _new_person_id()
        db.collection("faces").document(pid).set(
            {"vecs": [{"v": vec}], "born": time.time(), "persona": "", "state": "egg"})
        _log_event("arrive", dict(note, person=pid, state="new_egg"))
        return {"person": pid, "state": "egg"}
    doc = db.collection("faces").document(pid).get().to_dict() or {}
    vecs = doc.get("vecs", [])
    # 2026-09-12：5枚→8枚。同じ4人・同じ境目での実測で、新しいIDが
    # 生まれる率が 8.2% → 3.8% と半分以下になった。計算は増えない。
    if len(vecs) < FACE_MEMORY:                      # 見るたび少しずつ覚え直す（眼鏡・照明差に強くする）
        vecs.append({"v": vec})
        db.collection("faces").document(pid).update({"vecs": vecs})
    else:
        # 満杯。ここで打ち止めにすると、最初の数十分で埋まった見え方のまま
        # 一生変わらない。違う見え方が来たら、一番かぶっている1本と入れ替える。
        kept = _refresh_memory(vecs, vec)
        if kept is not None:
            db.collection("faces").document(pid).update({"vecs": kept})
            _log_event("memory_swap", {"person": pid, "shots": len(kept)})
    state = "ready" if doc.get("persona") else "egg"
    _log_event("arrive", dict(note, person=pid, state=state))
    return {"person": pid, "state": state}


def _touch_visit(st: dict, now: float) -> None:
    """滞在の始まりを覚える。前の気配から VISIT_MERGE_GAP 以上あいていれば新しい滞在。

    「一瞬しか居なかった人」と「出たり入ったりした人」を分けるため、
    滞在の長さ＝最後の気配 − 始まり、で測る。気配は顔・人感・動きのどれでもよい。"""
    prev = max(st.get("last_seen", 0), st.get("last_motion", 0))
    if not st.get("visit_start") or now - prev > VISIT_MERGE_GAP:
        st["visit_start"] = now


def _stay_seconds(st: dict) -> float:
    """いまの滞在の長さ（秒）。始まりが無ければ0。"""
    if not st.get("visit_start"):
        return 0.0
    last = max(st.get("last_seen", 0), st.get("last_motion", 0))
    return max(0.0, last - st["visit_start"])


def _mark_seen(st: dict, now: float, by: str = "") -> None:
    """この前後比較のあいだに、人が居たことを記す。

    「誰か」が分かったかどうかとは別に持つ。ここを visit_people（顔で
    分かった人の一覧）で兼ねていたため、顔が取れない日は「誰も来ていない」
    ことになり、実際に片づいた変化まで誤報として数えられていた
    （2026-09-06までの36時間で、棚と床が片づいた2件がそう処理された）。

    台帳#5（表情だけで手が出る）は世話イベントの数で測る。あれは
    「誰が」が無くても成立する主張なので、実装のほうもそう分ける。"""
    _touch_visit(st, now)
    st["empty"] = False
    st["last_seen"] = now
    st["visit_seen"] = True
    if by:
        # 何が「人が居る」と言ったのかを残す。人感だけが根拠の記録と、
        # 目で見えている記録は、あとで分けて読めるようにしておく。
        src = st.get("seen_by") or []
        if by not in src:
            st["seen_by"] = (src + [by])[-4:]


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
    # 上限は7日。もとは4時間だったが、実験の期間ぶん開けたいと言われて延ばした
    # （2026-09-03・研究室の同意あり）。期限式そのものはやめない。
    # 消し忘れより切り忘れのほうが起きやすく、放っておけば閉じる形を保つ。
    minutes = max(0, min(int(minutes), 10080))
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
    try:
        faces = list_prefix(FACES_PREFIX)
    except Exception:
        faces = []
    left = st.get("verify_until", 0) - time.time()
    return {"shots": shots, "count": len(shots),
            "faces": len(faces),          # 顔の切り抜き。消すときは一緒に消える
            "recording": left > 0, "minutes_left": round(left / 60, 1) if left > 0 else 0}


@router.post("/shots/clear")
async def clear_shots(key: str = ""):
    """確かめ用の写真を全部消す。技術的な確認が済んだらこれを叩く。

    置き場が2つある（全画面の写真と、顔の切り抜き）。
    片方だけ消す道を残すと、消したつもりで残る。必ず両方消す。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    try:
        n = delete_prefix(VERIFY_PREFIX) + delete_prefix(FACES_PREFIX)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    st = _load()
    st["verify_until"] = 0
    _save(st)
    _log_event("verify_clear", {"deleted": n})
    return {"ok": True, "deleted": n}


@router.post("/frame")
async def receive_frame(request: Request, pose: str = "", raw: str = "", big: int = 0,
                        check: int = 0, x_upload_key: str = Header(None)):
    """目からの写真1枚を、すべての判断に使う統合窓口（2026-08-31改訂）。

    以前は人感センサーが「人が居る」を判定していたが、座って動かない人を
    見失った（実測：二人が食事中に無人と誤判定）。写真を見れば人が居るかも
    誰かも同時に分かるので、判断の入口を写真に一本化する。

    順序:
      1) 顔が写っているか（無料・その場で）→ 写っていれば誰かを照合して終わり
      2) 顔は見えたが小さすぎた → その場で大きく撮り直させる（AIには聞かない）
      3) 顔が無ければAIに見せる → 人が写っていれば在室と記録（散らかりは測らない）
      4) 人も居なければ散らかりを判断する

    big=1 は「これはもう大きく撮り直した1枚」の印。これ以上大きくは
    撮れないので、同じ写真でまた撮り直しを頼まない。
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

    # 1コマにかかる時間の内訳（2026-09-12 夜）。
    # 「動きがあるあいだ3秒に1枚」の設定に対し、実測は6.5秒だった
    # （18:41〜18:43、人が立ちっぱなしの2分間で20枚）。送るのはラズパイだが、
    # 待たせているのがクラウドなのか回線なのか、推測しかできなかった。
    _t0 = time.perf_counter()
    _ms = {}
    def _lap(name):
        nonlocal _t0
        _ms[name] = round(1000 * (time.perf_counter() - _t0))
        _t0 = time.perf_counter()

    st = _load()
    _lap("state")
    now = time.time()
    if pose:
        st["last_pose"] = pose            # いまカメラが向いている先。待機位置を決める元

    if FACE_ENABLED:                           # ① 顔があれば、それが在室の証拠かつ本人の手がかり
        try:
            res = _identify(data)
            _lap("face")
            if res and not res.get("person"):
                # 顔は見えたのに小さすぎて誰とも結べなかった。
                # ここで大きく撮り直させる。AIの判断待ちにすると、
                # 順番が回ってくる頃には人が去っている（実測：今日の
                # 17:55、幅107pxの顔が誰にも結ばれないまま流れた）。
                _mark_seen(st, now, "顔(小)")
                if not big:
                    st["want_hires"] = now + 30
                _keep_shot(st, now, data, "small")
                _lap("shot")
                _save(st)
                _lap("save")
                _log_ms("small", _ms, len(data))
                return {"ok": True, "person": None, "judged": False,
                        "why": "face_too_small", "px": res.get("px"),
                        "hires": not big, "ms": _ms}
            if res:
                _mark_seen(st, now, "顔")
                st["want_hires"] = 0          # 取れたので、もう大きく撮らなくてよい
                if st.get("cur_person") != res["person"]:
                    st["greet_for"] = st["greet_line"] = None   # 相手が変われば言い直す
                st["cur_person"] = res["person"]
                st["cur_state"] = res["state"]
                st["last_seen"] = now
                st["face_at"] = now              # 最後に顔で確かめた時刻
                # 同じ滞在で同じ人には1回だけ（2026-09-10）。前は「予定が空なら」で、
                # 鳴らし終えるたびに次のコマでまた挨拶を予定し、居るあいだ何度も鳴っていた。
                gkey = "%s@%d" % (res["person"], int(st.get("visit_start") or 0))
                if st.get("greeted_key") != gkey:
                    st["greeted_key"] = gkey
                    try:
                        doc = get_db().collection("faces").document(
                            res["person"]).get().to_dict() or {}
                    except Exception:
                        doc = {}
                    b = _bond_now(doc)
                    alone = len(st.get("visit_people") or []) <= 1
                    if res["state"] == "egg" and len(doc.get("vecs") or []) <= 1:
                        kind, slow = "hello_new", True     # 初対面はためらう
                        ready = _ready_line("new", doc, st)
                    elif b >= 6:                       # なついている／べったり
                        kind, slow = "hello_close", False
                        ready = _ready_line(res["person"], doc, st)
                    else:                              # 見たことある／顔見知り
                        kind, slow = "hello_known", False
                        ready = _ready_line(res["person"], doc, st)
                    if ready:                          # その人向けに先に作ってあった一言
                        kind = ready
                    _plan_speech(st, kind, slow)
                # 前回の判断からこちら、誰が居たかを溜めておく。
                # 判断の時点で cur_person を見ると、とうに帰った人の名が残り、
                # 無人の記録にまで同じIDが付いていた（2026-09-02に実際に起きた）。
                seen = st.get("seen_people") or []
                vis = st.get("visit_people") or []       # 前後比較に添える顔ぶれ
                for pid in res.get("all") or [res["person"]]:
                    if pid not in seen:
                        seen.append(pid)
                    if pid not in vis:
                        vis.append(pid)
                st["seen_people"], st["visit_people"] = seen[-8:], vis[-8:]
                _keep_shot(st, now, data, res["person"])
                _lap("shot")
                _save(st)
                _lap("save")
                _log_ms("face", _ms, len(data))
                return {"ok": True, "person": res["person"], "state": res["state"],
                        "people": res.get("all") or [res["person"]],
                        "judged": False, "why": "person_seen", "ms": _ms}
        except Exception as e:
            logger.warning("identify failed: %s", e)
            _identify_err[0] = "%s: %s" % (type(e).__name__, str(e)[:200])

    # 動きがあって送られてきた1枚（big=1）は「誰かが動いている」証拠として時刻だけ残す。
    # AIには見せない。人が居る間に何度見ても、物は片づかないし散らからない。
    # ただし見回りの1枚そのもの（check）と、見回りの直後 CHECK_SELF_SEC の間の動きは、
    # カメラ自身が首を振った跡なので数えない。数えていたせいで滞在が途切れず、
    # 13:44の記録で「滞在56512秒（15時間）」になっていた（2026-09-10）。
    if big and not check and now - st.get("checked_at", 0) > CHECK_SELF_SEC:
        _touch_visit(st, now)
        st["last_motion"] = now
    # 2026-09-09 本人の方針：人が居ない間はAIを一切呼ばない。人が去ったあとに
    # 1回撮って判断すれば、次に人が来るまで何も変わらないので撮り直しは要らない。
    # それまでは「人が居る間、動きのたびに15秒に1回」呼んでいて、1日378回・
    # 上限400回に張り付いていた（9/1〜9/9で$13〜15）。判断するのは見回り(check)の
    # 1枚だけ。見回りは hint() が「人が去って静かになった」ときにだけ出す。
    if not check:
        _save(st)
        return {"ok": True, "judged": False, "why": "wait_for_check",
                "hires": now < st.get("want_hires", 0)}
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
        if check:
            # 見回りの1枚は時刻つきでも残す（1枚150KB・1日50枚ほど）。
            # 本人「写真を基本的に残したい」（2026-09-10）。人が居ない1枚だけなので
            # 写真を残さない決まりには触れない。
            stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime(now + JST))
            st["patrol_url"] = upload_to("spirit/patrol/%s.jpg" % stamp, data, "image/jpeg")
            st["patrol_bytes"] = len(data)
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
        _mark_seen(st, now, "AI")         # 誰が何を動かしたかを知るため
        # 人は写っているのに顔が取れなかった。もっと大きく写せば取れるかもしれない。
        # 送られてくる1280x720では、顔が135pxで確信度0.53と、あと一歩だった。
        st["want_hires"] = now + 30
        # 顔は取れなかったのに人は写っている＝顔検出が取りこぼした場面。
        # 確かめたいのはまさにここなので、期間中はこれも残す。
        _keep_shot(st, now, data, "noface")
    elif r.get("skip"):                   # 旧仕様の名残（人がいるとだけ返る場合）
        _mark_seen(st, now)
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
    # 顔が取れなくても、直前に誰か分かっていて、まだ同じ滞在の中なら、その人が居る。
    # 全部のコマで顔を取るのは無理がある。1回はっきり見えれば、その滞在は足りる。
    if npeople > 0 and not st.get("seen_people"):
        held = st.get("cur_person")
        if held and now - st.get("face_at", 0) < VISIT_HOLD:
            st["seen_people"] = [held]
            if held not in (st.get("visit_people") or []):
                st["visit_people"] = (st.get("visit_people") or []) + [held]
            _log_event("hold", {"person": held,
                                "since": round(now - st.get("face_at", 0))})
    if sc is not None:
        _log_event("judge", {"raw": sc, "score": round(st["score"], 3), "pose": pose,
                             "N": round(_calc_n(st, now), 3), "comment": st.get("comment", ""),
                             "objects": st.get("objects", []), "people": npeople,
                             "who": st.get("seen_people") or []})
    st["seen_people"] = []                # ここまでを1区間として締める
    _save(st)
    # 人が去って落ち着いてから突き合わせる。居る間の1枚を「後」にすると
    # 本人が写り込んでしまい、物の変化と見分けがつかない。
    check = st.get("check_pose") or ""
    right_place = (not check) or (pose == check)   # 見に行く先で撮った1枚か
    if right_place and npeople == 0 and now - st.get("last_seen", 0) > VISIT_END_GAP:
        # 区画の数だけAIに問い合わせるので、返事を待たせると橋渡しが
        # 待ちきれずに切れる。返事は先に返し、突き合わせは裏で走らせる。
        if not _zone_busy[0]:
            _zone_busy[0] = True
            asyncio.create_task(_zone_cycle_bg(st, data, now, pose))
    logger.info("spirit judge: raw=%s smoothed=%.2f comment=%s", sc, st["score"], st.get("comment"))
    return {"ok": True, "judged": sc is not None, "score": st["score"],
            "hires": now < st.get("want_hires", 0)}


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
        if state == "occupied":
            _mark_seen(st, time.time(), "人感")  # 顔が取れなくても、人は来ていた
        if state == "occupied" and prev:
            # 人感は部屋のどこで動いても反応するが、カメラは一方向しか
            # 見ていない。実際、人感が気づいた5分後にようやく顔が取れた。
            # 目に「探しに行け」と伝える札を立てる。
            #
            # ただし「居なかった→居る」に変わった瞬間だけにする（prev＝直前は無人）。
            # 人感は居るあいだ鳴りつづけるので、条件を付けないと札が立ちっぱなしになり、
            # 今日は79回も首を振ってしまった。動くたびに景色が変わり、
            # 前後の比較が成り立たなくなる。
            st["hint_until"] = time.time() + SWEEP_HINT_SEC
        _save(st)
        return "ok\n"
    return ("empty" if st["empty"] else "occupied") + "\n"


@router.get("/home", response_class=PlainTextResponse)
async def home_get():
    """待機位置。目が読みに来る。空なら既定のまま。

    止めているときは向きのうしろに paused を付ける。目はこれを見て
    定位置へ戻すのをやめる。設置の最中に勝手に戻られると、
    向きを決めて押す前にカメラが逃げてしまう（実際に起きた）。"""
    st = _load()
    line = st.get("home_pose") or ""
    if st.get("sweep_paused"):
        line = (line + " paused").strip()
    return line + "\n"


@router.post("/home")
async def home_set(key: str = "", pause: int = -1, pose: str = ""):
    """いまカメラが向いている先を、待機位置として覚える。

    カメラを動かすたびにコードを書き直していては追いつかない。
    移動した本人が、その場で向きを決めて押せるようにする。
    pause=1 で首振りを止める（設置作業の間、勝手に動かれると困る）。
    pose を渡せばその向きをそのまま覚える（目がまだ動いていない時用）。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    st = _load()
    if pause >= 0:
        st["sweep_paused"] = bool(pause)
    else:
        st["home_pose"] = pose or st.get("last_pose") or ""
        st["zones"] = []              # 画角が変われば区画も立て直す
        st["baseline_at"] = 0
        _log_event("home", {"pose": st["home_pose"]})
    _save(st)
    return {"ok": True, "home": st.get("home_pose"),
            "paused": bool(st.get("sweep_paused")),
            "note": "区画は次に人が去ったときに立て直します"}


@router.get("/hint", response_class=PlainTextResponse)
async def hint():
    """目が数秒おきに覗きにくる札。探すべきなら sweep、でなければ空。

    人感が鳴ったことを目へ伝える手立てが他にない。クラウドから宅内へは
    押し込めないので、目のほうから軽く覗きにくる形にした。
    返すのは数バイトなので、3秒おきでも負担にならない。"""
    st = _load()
    # 「在室」は人感が立てるものでもあるので、それを理由に探すのをやめると
    # 人感が鳴った瞬間に札が自分で消えてしまう。顔が取れているかで判断する。
    if st.get("sweep_paused"):
        return ""                              # 設置作業中などは動かさない
    now = time.time()
    # 誰も居ないと分かってから、キッチンを見に行く。
    # 目はここを3秒おきに覗きにくるので、札を立てるだけで伝わる。
    check = st.get("check_pose") or ""
    # 「人が来た」の手がかりは3つ：顔・人感・動きのあるコマ(big=1)。どれかの最後の時刻。
    checked = st.get("checked_at", 0)
    # 見回りでカメラが動くと、その動き自体が「動きのあるコマ」として届き、
    # 3分後にまた見回りが出る（2026-09-10 朝：誰も居ないのに5分おきに判断が走り、
    # 10:45で118回）。見回りの直後 CHECK_SELF_SEC 以内の動きは、人ではなくカメラ自身。
    motion = st.get("last_motion", 0)
    if motion < checked + CHECK_SELF_SEC:
        motion = 0
    active = max(st.get("last_seen", 0), motion)
    # 出す条件（2026-09-09）：静かになってから CHECK_QUIET_SEC 経った ＋
    # 前回の見回りのあとに人が来ている ＋ 見回り同士は CHECK_GAP 以上あける。
    # 誰も来なければ何度見ても同じなので出さない（AIも呼ばれない）。
    if (check and now - active > CHECK_QUIET_SEC
            and active > checked
            and now - checked > CHECK_GAP):
        return "check " + check + "\n"
    # 人を探して首を振る仕組みは止めた（2026-09-06）。
    # カメラは入り口を向いて待っているので、探しに行く先がもう無い。
    # 振れば景色が変わり、そのたび静かさの基準と前後比較が壊れる。
    return ""


@router.get("/checkpose", response_class=PlainTextResponse)
async def checkpose_get():
    """見に行く先（キッチンの向き）。空なら見回りをしない。"""
    return (_load().get("check_pose") or "") + "\n"


@router.post("/checkpose")
async def checkpose_set(pose: str = "", key: str = ""):
    """見に行く先を決める。いまカメラが向いている先を渡すのが早い。

    pose を空にすると見回りをやめる（1つの向きに留まる）。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    st = _load()
    st["check_pose"] = pose or ""
    st["checked_at"] = 0                       # すぐ1回目を見に行かせる
    st["zones"] = []                           # 区画は見に行く先で立て直す
    _log_event("checkpose", {"pose": st["check_pose"]})
    _save(st)
    return {"ok": True, "check_pose": st["check_pose"],
            "note": "誰も居なくなってから見に行きます"}


@router.post("/checked", response_class=PlainTextResponse)
async def checked():
    """見回りが済んだことを目が伝える。次は CHECK_GAP 後まで行かない。"""
    st = _load()
    st["checked_at"] = time.time()
    _save(st)
    return "ok\n"


@router.post("/hint/clear", response_class=PlainTextResponse)
async def hint_clear():
    """探し終わったら目が札を下ろす。"""
    st = _load()
    st["hint_until"] = 0
    _save(st)
    return "ok\n"


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
function setHome(){
  if(!confirm('いまカメラが向いている先を待機位置にします。区画も立て直します。'))return;
  post('/spirit/home').then(function(j){
    if(j.detail){alert('合言葉がちがいます');return}
    alert('待機位置を '+(j.home||'?')+' にしました'); load();
  });
}
function pause(v){
  post('/spirit/home?pause='+v).then(function(j){
    if(j.detail){alert('合言葉がちがいます');return}
    load();
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
        if len(vecs) < FACE_MEMORY:                  # 見るたび少しずつ覚え直す（眼鏡・照明差に強くする）
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



# 2026-09-09: 「いらっしゃいませ」のような店員の言葉が出て本人に「キモい」と言われた。
# 作り置きの持ち歌（voice/make_lines.py）と同じ子どもの声に揃える。
_GREET_SYSTEM = (
    "あなたは『きっちんちゃん』。共有キッチンに棲みついている、小さな子どものような地霊です。"
    "いま目の前に人が来ました。"
    "【話し方】ちいさな子どもが、ひとりごとのように。ひらがな多め。"
    "『あのね』『えーとね』『〜なあ』『〜かなあ』『〜だね』のような言い方。"
    "ていねい語（です・ます・いらっしゃいませ・こんにちは）は使わない。店員のようには絶対に言わない。"
    "点々（……）でためらってよい。15字以内。"
    "【手本】『あ、きた。』『あのね、まってたんだよ。』『あれ。えーと……どなたかなあ。』"
    "『あ、きてくれたね。うれしいなあ。』"
    "【いちばん大事な掟】命令しない・お願いしない・提案しない・責めない。"
    "『片付けて』『〜してね』の類は絶対に言わない。数や回数も口にしない。"
    "相手を評価する言葉（えらい・すごい・だめ）も言わない。"
    "【この相手への接し方】ここに書かれた気分のとおりに振る舞う。"
    "ただし、その理由（相手が何をした・しなかった）には決して触れない。"
    "返すのは声に出す一言だけ。かぎかっこも説明も、ト書きもいらない。"
)


async def _greet_line(persona: str, manner: str, thanks: bool = False,
                      news: bool = False) -> str:
    """その人へ向けた一言をつくる。

    thanks＝この人が前に片づけていた（ありがとうを言う）。
    news＝最近シンクがきれいになっていた（場所の様子として伝える。誰がやったかは言わない）。
    2026-09-10 本人決定：「だれかがきれいにしてくれた」は負債感になるので言わない。
    「シンクがきれいになってた」と場所の様子を言うのはよい。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ""
    ask = "【この相手への接し方】" + manner
    if thanks:
        ask += ("\n【伝えたいこと】このまえ、この人が帰ったあと、シンクがきれいになっていた。"
                "ありがとう・うれしかった、という気持ちをこの人に伝えたい。"
                "何をしたかは言わない。評価する言葉（えらい・すごい）は使わない。"
                "このときだけ25字まで使ってよい。")
    elif news:
        ask += ("\n【伝えたいこと】さっき見たら、シンクがきれいになっていた。うれしい。"
                "場所の様子だけを言う。誰がやったかは言わない。『だれかが』『だれだろう』とも言わない。"
                "このときだけ25字まで使ってよい。")
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        msg = await client.messages.create(
            model=MODEL, max_tokens=120,
            system=(persona or _DEFAULT_PERSONA) + "\n" + _GREET_SYSTEM,
            messages=[{"role": "user", "content": ask}])
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        return _sanitize(text)
    except Exception as e:
        logger.warning("greet failed: %s", e)
        return ""


@router.get("/greet", response_class=PlainTextResponse)
async def greet():
    """いま居る人へ向けた一言。C3が読む。

    台帳#12（2026-09-03改訂）の4規則に従う:
      良い知らせは名前を出さずに第三者へ／不満は本人だけ・ひとりのときだけ／
      使って放置したかで測る／数は見せない。"""
    st = _load()
    pid = st.get("cur_person")
    if not pid or time.time() - st.get("last_seen", 0) > 120:
        return ""
    if st.get("greet_for") == pid and st.get("greet_line"):
        return st["greet_line"] + "\n"        # 同じ滞在で言い直さない
    try:
        doc = get_db().collection("faces").document(pid).get().to_dict() or {}
    except Exception:
        doc = {}
    alone = len(st.get("visit_people") or []) <= 1
    thanks = _own_care(pid)               # 本人が片づけていたときだけ、ありがとう
    news = (not thanks) and _recent_care()   # そうでなければ、場所の様子として
    line = await _greet_line(st.get("persona", ""), _manner(doc, alone), thanks, news)
    if not line:
        return ""
    st["greet_for"], st["greet_line"] = pid, line
    _save(st)
    _log_event("greet", {"person": pid, "alone": alone, "thanks": thanks})
    return line + "\n"


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


@router.get("/people")
async def people():
    """人ごとの積み重ね（研究用の取り出し口）。

    ここは研究者が読むためのもので、地霊はここの数を口に出さない。
    数や順位が本人や周りに見えた時点で、ありがとうは制度になる。"""
    out = []
    try:
        for d in get_db().collection("faces").stream():
            v = d.to_dict() or {}
            out.append({"id": d.id,
                        "cares": v.get("cares", 0),      # 片づいた方向の変化に居合わせた
                        "uses": v.get("uses", 0),        # 散らかった方向の変化に居合わせた
                        "shots": len(v.get("vecs", [])),
                        "bond": _bond_now(v),            # いまのなつき度（0〜10）
                        "stage": _bond_stage(_bond_now(v))[0],
                        "born": v.get("born"), "last_at": v.get("last_at")})
    except Exception as e:
        return {"people": [], "error": str(e)}
    out.sort(key=lambda x: x.get("born") or 0)
    return {"people": out}


@router.post("/merge")
async def merge_people(keep: str, drop: str, key: str = ""):
    """割れてしまった2つのIDを1つにまとめる。

    dropの見え方をkeepへ移し、dropを消す。世話・利用の数も足し合わせる。
    /spirit/similar で「同じ人と判定」と出た組に対して使う。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    db = get_db()
    a = db.collection("faces").document(keep)
    b = db.collection("faces").document(drop)
    da, dbb = a.get().to_dict(), b.get().to_dict()
    if not da or not dbb:
        return {"ok": False, "error": "そのIDが見つかりません"}
    # 2026-09-09: 消す側(drop)の見え方を前に置く。割れるのは、カメラの向きが
    # 変わって新しい角度の顔が古い顔と結べなかったときなので、新しい角度の
    # 見え方を残さないと、まとめた翌日にまた割れる（p01は古い向きの5枚で
    # 埋まっていて、今日の見下ろす角度の p02〜p04 が全部別人になった）。
    vecs = (dbb.get("vecs") or []) + (da.get("vecs") or [])
    a.update({"vecs": vecs[:FACE_MEMORY],
              "cares": (da.get("cares") or 0) + (dbb.get("cares") or 0),
              "uses": (da.get("uses") or 0) + (dbb.get("uses") or 0),
              "bond": max(float(da.get("bond") or 0), float(dbb.get("bond") or 0))})
    b.delete()
    st = _load()
    if st.get("cur_person") == drop:
        st["cur_person"] = keep
        _save(st)
    _log_event("merge", {"keep": keep, "drop": drop})
    return {"ok": True, "keep": keep, "dropped": drop, "shots": len(vecs[:FACE_MEMORY])}


@router.post("/bond")
async def bond_set(who: str = "", value: int = 0, key: str = ""):
    """なつき度を手で書き換える（0〜10で止める）。試験と手直し用。

    2026-09-09: 段階を変えたときに地霊の言い方が変わるかを確かめるための口。
    数そのものは地霊が口に出さない（台帳#12）。研究者だけが触る。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    if not who:
        raise HTTPException(status_code=400, detail="who is required")
    value = max(0, min(BOND_MAX, int(value)))
    try:
        ref = get_db().collection("faces").document(who)
        if not ref.get().exists:
            return {"ok": False, "error": "そのIDが見つかりません"}
        ref.update({"bond": value, "last_at": time.time()})
    except Exception as e:
        return {"ok": False, "error": str(e)}
    _log_event("bond_set", {"person": who, "bond": value})
    return {"ok": True, "person": who, "bond": value, "stage": _bond_stage(value)[0]}


@router.post("/people/reset")
async def people_reset(key: str = "", who: str = ""):
    """世話・利用・なつき度をゼロに戻す（顔は覚えたまま）。

    比べてはいけない2枚から作られた記録が10件残った。中身は光と
    カメラの向きの変化で、誰の手でもない。論文の記録として置いておくと
    そのまま嘘になるので、消せる手を用意する。顔まで忘れる必要はない。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    n = 0
    try:
        db = get_db()
        for d in db.collection("faces").stream():
            if who and d.id != who:
                continue
            d.reference.update({"cares": 0, "uses": 0, "bond": 0})
            n += 1
    except Exception as e:
        return {"ok": False, "error": str(e)}
    _log_event("people_reset", {"count": n, "who": who or "全員"})
    return {"ok": True, "reset": n}


@router.post("/faces/drop")
async def drop_face(who: str = "", key: str = ""):
    """指定した1つのIDだけを忘れる。

    まっさらにすると、正しく覚えている人まで巻き添えになる。
    誤ってできた1つ（2026-09-05のp02＝テーブルの土鍋）を抜くための口。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    if not who:
        raise HTTPException(status_code=400, detail="who is required")
    try:
        get_db().collection("faces").document(who).delete()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    st = _load()
    if st.get("cur_person") == who:
        st["cur_person"] = st["cur_state"] = None
    for k in ("seen_people", "visit_people"):
        st[k] = [p for p in (st.get(k) or []) if p != who]
    _save(st)
    _log_event("face_drop", {"person": who})
    return {"ok": True, "dropped": who}


@router.post("/notfaces/add")
async def notfaces_add(request: Request, key: str = ""):
    """写真を1枚もらって、そこに写っている「顔らしきもの」を人ではないものとして覚える。

    鍋・五徳・棚を顔と見てしまう分を、ここに貯めて弾く（2026-09-12 夜）。
    顔の覚え（faces）とは別の場所に置く。混ぜると照合が狂う。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    # 送られてくるのは、すでに切り抜かれた1枚。もう一度顔を探させると、
    # 自信度0.80では見つからず何も登録できない（2026-09-12 実測：8枚中0枚）。
    # 切り抜きをそのまま特徴量にする。face.embed の中で目・鼻・口を取り直し、
    # だめなら引き伸ばしに落ちる——測定に使った道すじと同じ（一致度 1.0000 で確認）。
    import cv2
    import numpy as np
    from server import face
    data = await request.body()
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return {"ok": False, "error": "写真として読めません"}
    vec = face.embed(img)
    if vec is None:
        return {"ok": False, "error": "特徴量が作れません"}
    db = get_db()
    n = len(list(db.collection("notfaces").stream()))
    db.collection("notfaces").document("o%02d" % (n + 1)).set(
        {"vecs": [{"v": vec}], "born": time.time(), "px": int(img.shape[1]), "why": "手で登録"})
    _junk_cache[0] = 0.0
    return {"ok": True, "id": "o%02d" % (n + 1), "px": int(img.shape[1]), "count": n + 1}


@router.post("/notfaces/from_person")
async def notfaces_from_person(who: str, key: str = ""):
    """「このIDは人ではなかった」を、覚えに移す。

    パネルから押せるようにしておくと、使うほど鍋に強くなる。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    db = get_db()
    doc = db.collection("faces").document(who).get().to_dict()
    if not doc:
        return {"ok": False, "error": "そのIDが見つかりません"}
    vecs = [v for v in (doc.get("vecs") or []) if isinstance(v, dict) and v.get("v")]
    if not vecs:
        return {"ok": False, "error": "覚えが空です"}
    n = len(list(db.collection("notfaces").stream()))
    db.collection("notfaces").document("o%02d" % (n + 1)).set(
        {"vecs": vecs, "born": time.time(), "why": "元 " + who})
    db.collection("faces").document(who).delete()
    _junk_cache[0] = 0.0
    _log_event("face_to_object", {"person": who, "shots": len(vecs)})
    return {"ok": True, "moved": who, "shots": len(vecs), "count": n + 1}


@router.get("/notfaces")
async def notfaces_list():
    """覚えている「人ではないもの」の数。中身（特徴量）は出さない。"""
    try:
        out = [{"id": d.id, "shots": len((d.to_dict() or {}).get("vecs") or []),
                "why": (d.to_dict() or {}).get("why", "")}
               for d in get_db().collection("notfaces").stream()]
    except Exception as e:
        return {"objects": [], "error": str(e)}
    return {"objects": sorted(out, key=lambda x: x["id"]), "threshold": JUNK_SIM}


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
    # 顔を消しても「いま居る人」の覚えに古いIDが残り、次の見回りの滞在に
    # 消したはずの p02 が付いていた（2026-09-10 13:44）。一緒に忘れる。
    st["cur_person"] = st["cur_state"] = None
    st["greeted_key"] = None
    for k in ("seen_people", "visit_people", "seen_by"):
        st[k] = []
    _save(st)
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
    from server import face
    try:
        known = _known_faces()
    except Exception as e:
        return {"pairs": [], "error": str(e)}
    ids = sorted(known)
    pairs = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            best, ok = 0.0, False
            for va in known[a]:
                for vb in known[b]:
                    xa = np.asarray(va, dtype=np.float32)
                    xb = np.asarray(vb, dtype=np.float32)
                    # 覚えの長さが違う組は比べられない。モデルを差し替えた前後の
                    # IDが並ぶ間だけ起きる。ここで落ちるとパネル全体が止まり、
                    # 「覚えた顔を忘れる」のボタンまで効かなくなった（2026-09-12）。
                    if xa.shape != xb.shape:
                        continue
                    best, ok = max(best, float(np.dot(xa, xb))), True
            pairs.append({"a": a, "b": b,
                          "similarity": round(best, 3) if ok else None,
                          "same_person": bool(ok and best >= face._SIM_THRESHOLD)})
    pairs.sort(key=lambda x: -(x["similarity"] if x["similarity"] is not None else -1))
    return {"pairs": pairs, "threshold": face._SIM_THRESHOLD}



# ---- 持ち歌（2026-09-06）----
# クラウドにGPUは無いので、その場では声を合成できない。
# 憲法どおりなら台詞は限られた数で足りる（数も評価も助言も言わないので）、
# あらかじめ作った音をGCSに置き、場面で選ぶ。
# 生き物の持ち歌が限られているのは、むしろ自然なこと。
#
# 第5条（間を置く）もここで守る。0.3秒以内に返すのは「軽率」なので、
# 鳴らしてよい時刻を決めておき、それより前に取りに来ても何も渡さない。
LINES_PREFIX = "spirit/lines/"
SPEAK_MIN = 0.8            # これより早くは返さない
SPEAK_MAX = 1.6            # ふつうの間
SPEAK_SLOW = 3.0           # ためらうときの間
SILENT_CHANCE = 0.1        # 10回に1回は黙る（ぎこちなさを残す・p.159）

_line_cache = {"at": 0.0, "names": []}


def _line_names() -> list:
    """置いてある持ち歌の名前。数分は覚えておく。"""
    now = time.time()
    if now - _line_cache["at"] > 300 or not _line_cache["names"]:
        try:
            _line_cache["names"] = [b["name"].split("/")[-1].replace(".pcm", "")
                                    for b in list_prefix(LINES_PREFIX)
                                    if b["name"].endswith(".pcm")]
            _line_cache["at"] = now
        except Exception as e:
            logger.warning("line list failed: %s", e)
    return _line_cache["names"]


def _pick_line(kind: str) -> str | None:
    """その場面の持ち歌から1つ選ぶ。毎回同じにはしない。"""
    import random
    cands = [n for n in _line_names() if n.rsplit("_", 1)[0] == kind]
    return random.choice(cands) if cands else None


def _plan_speech(st: dict, kind: str, slow: bool = False) -> None:
    """何を、いつ鳴らすかを決める。

    すぐ返すと、聞いていたのではなく反射したように見える。
    間があると、受け取って・考えて・返した、という順序が生まれる。"""
    import random
    if random.random() < SILENT_CHANCE:
        st["speak_line"] = None                    # たまに黙る
        return
    name = _pick_line(kind)
    if not name:
        return
    gap = random.uniform(SPEAK_MIN, SPEAK_SLOW if slow else SPEAK_MAX)
    st["speak_line"] = name
    st["speak_at"] = time.time() + gap
    _log_event("speak", {"line": name, "after": round(gap, 2)})


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


SAY_NAME = "say_0"          # その場で作った、いまの一言の声
VOICE_GAP = 60.0            # 声と声のあいだは1分あける（本人決定 2026-09-10）
VOICE_GAIN = 0.5            # 音量。1.0＝作り置きのまま。本人「下げてよい」→ まず半分（2026-09-10）
# 「何も鳴らさない」の返し方（2026-09-10）。
# C3のファーム（spirit_body.ino）は、クラウドから声が届かないと（204・1000バイト以下）
# 従来のあつ森語で鳴く作りになっている。声を1分に1回に絞ったとたん、残りの
# 問い合わせが全部あつ森語になった（本人「アツモリの声になってる」）。
# ファームを焼き直すまでは、黙るときも「無音の声」を返して、あつ森語に落ちないようにする。
# 16kHz・16bit・モノラルで 0.1秒 = 3200バイト（C3は1000バイト超で「喋った」とみなす）。
_SILENCE = bytes(3200)


def _quiet() -> Response:
    return Response(content=_SILENCE, media_type="application/octet-stream")


def _scale_pcm(pcm: bytes, gain: float) -> bytes:
    """16bit・モノラルの生PCMの音量を変える。C3を焼き直さずに済ませるため。"""
    if gain == 1.0 or not pcm:
        return pcm
    try:
        import array
        a = array.array("h")
        a.frombytes(pcm[:len(pcm) - len(pcm) % 2])
        for i in range(len(a)):
            a[i] = int(max(-32768, min(32767, a[i] * gain)))
        return a.tobytes()
    except Exception as e:
        logger.warning("pcm scale failed: %s", e)
        return pcm
VOICE_WINDOW = 300.0        # 場所の一言を鳴らすのは、滞在の最初の5分だけ（同）


@router.post("/say")
async def put_say(text: str = "", request: Request = None,
                  x_upload_key: str = Header(None)):
    """いまの一言を声にしたものを置く（2026-09-07）。

    クラウドには音声合成が無い（espeak-ngも入っていない）。VOICEVOXは
    宅内で動いていて、クラウドから宅内へは入れない。そこで宅内の側から
    「いまの一言を読み上げた音」を持ってきてもらう。

    text には、何を読み上げたのかを添える。地霊の一言は判断のたびに
    変わるので、これが今の一言と食い違っていたら、その音は古い。
    古い音を鳴らすくらいなら、場面に合った作り置きを鳴らすほうがよい。"""
    if UPLOAD_KEY and x_upload_key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    upload_to(LINES_PREFIX + SAY_NAME + ".pcm", data, "application/octet-stream")
    st = _load()
    st["say_text"] = text
    st["say_at"] = time.time()
    _save(st)
    _log_event("say_made", {"text": text[:40], "bytes": len(data)})
    return {"ok": True, "bytes": len(data), "text": text}


@router.get("/say", response_class=PlainTextResponse)
async def get_say():
    """いま声にしてほしい一言。宅内の合成係が数十秒おきに覗きにくる。

    もう声にしてあるなら空を返す。同じものを何度も作らせない。"""
    st = _load()
    text = st.get("comment") or ""
    if not text or st.get("say_text") == text:
        return "\n"
    return text + "\n"


@router.post("/lines/{name}")
async def put_line(name: str, request: Request, x_upload_key: str = Header(None)):
    """持ち歌を1本置く。手元のGPUで作ったものを送り込むための口。

    クラウドにGPUは無いので声は作れない。作るのは手元、置くのはここ。
    声を作り直したくなったら、同じ名前で上書きすればよい。"""
    if UPLOAD_KEY and x_upload_key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    if not re.fullmatch(r"[a-z0-9_]+_\d+", name):
        raise HTTPException(status_code=400, detail="bad name")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    url = upload_to(LINES_PREFIX + name + ".pcm", data, "application/octet-stream")
    _line_cache["at"] = 0.0                 # 覚え直させる
    return {"ok": True, "name": name, "bytes": len(data), "url": url}


# ---- 声を裏で事前に作る（本人決定 2026-09-09 深夜）----
# 人が去った直後は誰も居ないので、その時間に「知っている人ごとの次の一言」を文にして
# おき、宅内の声係（ラズパイの VOICEVOX・1本30秒）が音にして置いておく。
# 次に来た瞬間、その人の分（for_<ID>_0）を鳴らす。初めての人向けは for_new_0。
def _todo_name(pid: str) -> str:
    return "for_" + pid + "_0"


async def _prepare_greetings(st: dict, now: float) -> int:
    """知っている人ぜんぶと「初めての人」向けに、次の一言の文を作って覚えておく。"""
    persona = st.get("persona", "")
    n = 0
    try:
        db = get_db()
        news = _recent_care()                  # 場所の様子。誰がやったかは言わない
        for d in db.collection("faces").stream():
            doc = d.to_dict() or {}
            manner = _bond_stage(_bond_now(doc))[1]
            thanks = _own_care(d.id)
            text = await _greet_line(persona, manner, thanks, news and not thanks)
            if text and text != doc.get("next_text"):
                d.reference.update({"next_text": text, "next_at": now})
                n += 1
        text = await _greet_line(persona, BOND_STAGES[0][2], False, news)
        if text and text != st.get("next_new_text"):
            st["next_new_text"], st["next_new_at"] = text, now
            n += 1
    except Exception as e:
        logger.warning("prepare greetings failed: %s", e)
    if n:
        _log_event("prepared", {"lines": n})
    return n


@router.get("/todo")
async def todo():
    """声係が覗きにくる：まだ音になっていない一言の一覧。"""
    out = []
    try:
        for d in get_db().collection("faces").stream():
            doc = d.to_dict() or {}
            t = doc.get("next_text")
            if t and t != doc.get("made_text"):
                out.append({"name": _todo_name(d.id), "text": t})
    except Exception as e:
        logger.warning("todo list failed: %s", e)
    st = _load()
    t = st.get("next_new_text")
    if t and t != st.get("made_new_text"):
        out.append({"name": _todo_name("new"), "text": t})
    return {"todo": out}


@router.post("/todo/{name}")
async def todo_done(name: str, request: Request, text: str = "",
                    x_upload_key: str = Header(None)):
    """声係が作った音を置き、何を読んだかを覚える（同じ文を二度作らせない）。"""
    if UPLOAD_KEY and x_upload_key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    if not re.fullmatch(r"for_[a-z0-9]+_0", name):
        raise HTTPException(status_code=400, detail="bad name")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    upload_to(LINES_PREFIX + name + ".pcm", data, "application/octet-stream")
    _line_cache["at"] = 0.0
    pid = name[len("for_"):-2]
    if pid == "new":
        st = _load()
        st["made_new_text"] = text
        _save(st)
    else:
        try:
            get_db().collection("faces").document(pid).update({"made_text": text})
        except Exception as e:
            logger.warning("todo done update failed: %s", e)
    _log_event("voice_made", {"name": name, "bytes": len(data)})
    return {"ok": True, "name": name, "bytes": len(data)}


def _ready_line(pid: str, doc: dict, st: dict) -> str | None:
    """その人向けに作り置いた一言があれば、その持ち歌の場面名を返す。"""
    if pid == "new":
        ok = st.get("next_new_text") and st.get("next_new_text") == st.get("made_new_text")
    else:
        ok = doc.get("next_text") and doc.get("next_text") == doc.get("made_text")
    kind = "for_" + pid
    return kind if ok and _pick_line(kind) else None


@router.get("/lines")
async def list_lines():
    """置いてある持ち歌の一覧。"""
    return {"lines": sorted(_line_names())}


@router.get("/voice.pcm")
async def voice_pcm():
    """C3が取りに来る声。作り置きの日本語（VOICEVOX:ずんだもん）だけを返す。

    まず持ち歌を見る。決まっていて、鳴らしてよい時刻を過ぎていれば、それを返す。
    時刻より前なら何も返さない――その沈黙が「間」になる（憲法第5条）。

    2026-09-07: 合成（espeak-ng）の経路を外した。クラウドにespeak-ngは
    入っておらず（No such file or directory）、一度も成功していない。
    それでいて古い版が残した音声が配られ続け、どの声が鳴っているのか
    分からない状態になっていた。作り置きだけにすれば、鳴る声は必ず
    GCSに置いた19本のどれかになる。

    言うことが決まっていないときは、その場の様子から選ぶ。
    何も当てはまらなければ204で黙る。503はエラーであって沈黙ではない。"""
    st = _load()
    now = time.time()
    name = st.get("speak_line")
    if name:
        if now < st.get("speak_at", 0):
            return _quiet()                        # まだ。これが間になる
        st["speak_line"] = None                    # 一度鳴らしたら下ろす
        _save(st)
    else:
        # 場所の一言。C3は人が居るあいだ12秒おきに取りに来るので、毎回返すと
        # 1回の来訪で3回鳴ってしつこい（2026-09-10 実測）。1回だけだと聞き逃す。
        # 本人決定：1分に1回、滞在の最初の5分まで。
        # ※ファーム側の「1滞在3回まで・12秒おき」が残っているので、実機では
        #   今のところ1回しか鳴らない。9/12のファーム作業で 60秒おき・5回に直す。
        since = now - float(st.get("visit_start") or 0)
        if since >= VOICE_WINDOW or now - float(st.get("voiced_at") or 0) < VOICE_GAP:
            return _quiet()
        # いまの一言を声にしたものがあれば、それを鳴らす。
        # 一言は判断のたびに変わるので、食い違っていたら古い音。
        if st.get("say_text") and st.get("say_text") == (st.get("comment") or ""):
            name = SAY_NAME
        else:
            # 無ければ場面に合った作り置き。
            # 散らかっているならそわそわ、そうでなければひとりごと。
            name = _pick_line("worse" if st.get("score", 0) >= M_HI else "alone")
    if not name:
        return _quiet()
    try:
        pcm = read_object(LINES_PREFIX + name + ".pcm")
    except Exception as e:
        logger.warning("line read failed (%s): %s", name, e)
        pcm = None
    if not pcm:
        return _quiet()
    st["voiced_at"] = now                          # 次の声は VOICE_GAP 後
    _save(st)
    _log_event("voice", {"line": name, "bytes": len(pcm)})
    return Response(content=_scale_pcm(pcm, VOICE_GAIN), media_type="application/octet-stream")


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
  <h2 style="margin-top:0">カメラの向き</h2>
  <div class=st id=homest style="margin-bottom:8px">…</div>
  <button onclick="setHome()">いまの向きを待機位置にする</button>
  <button class=gray onclick="pause(1)">首振りを止める</button>
  <button class=gray onclick="pause(0)">再開する</button>
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
  fetch('/spirit/zones').then(function(r){return r.json()}).then(function(j){
    var h = '待機位置 '+(j.home||'既定のまま')+'<br>首振り '+(j.paused?'<span class=rec>止めている</span>':'動く');
    document.getElementById('homest').innerHTML = h;
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


# ---- 見方C（2026-09-09 本人決定）----
# 見るのはシンクだけ。前→今と今→前の両方向で聞き、答えが裏返しのときだけ変化と認める。
# 備え付けを文で説明する。シンク全体が写っていなければ何もしない。
# 根拠（9/9の実測・本番と同じモデル）：同じ景色20枚で誤報0／コップ1→5個は10/10「増えた」／
# 5個→コップ1は10/10「減った」／散らかった同士では片方向だと5組中4組が「減った」と誤報し、
# 両方向ルールで0になった／横0.05〜0.10のずれは平気、シンクが切れると誤報。
ZONE_NAMES = ("シンク",)          # 見張る区画。首振りで増やすときはここに足す
ZONE_FIXTURES = {
    "シンク": ("備え付けの物（ステンレスの水切りかご、壁の包丁立てと包丁、壁のフックに掛かっている道具、"
              "蛇口、排水口の網）は見ません。見るのは『シンク（流し台の金属のくぼみ）の底に置かれている物』だけです。"),
}
_ZONE_SCENE = "写真は共有キッチンのシンク周りを天井近くから見下ろしたものです。"


async def _ask_json(content: list, max_tokens: int = 150) -> dict:
    """写真つきの問いをJSONで返してもらう共通口。失敗は {"error": ...}。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"error": "no api key"}
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        msg = await client.messages.create(model=MODEL, max_tokens=max_tokens,
                                           messages=[{"role": "user", "content": content}])
        text = "".join(x.text for x in msg.content if x.type == "text")
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            return {"error": "not json: " + text[:160]}
        return json.loads(text[i:j + 1])
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, str(e)[:200])}


def _img_block(d: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(d).decode()}}


SHIFT_MAX_PX = 150     # 前後の写真がこれ以上ずれていたら比べない（1280幅の画素）


def _frame_shift(a: bytes, b: bytes) -> tuple:
    """2枚の写真の位置ずれ（画素）を測る。AIを使わない・その場で終わる。

    「シンク全体が写っているか」をAIに聞くと、定位置の写真でも「切れている」と
    答えてしまい門にならなかった（2026-09-09・3通りの聞き方で全部 false）。
    代わりに写真そのものの位置合わせで測る。実測：同じ向き 0px／中身が変わっただけ 20px／
    横0.05ずれ 124px（比べても平気だった）／横0.10ずれ 270px（シンクが切れて誤報した）。"""
    try:
        import cv2
        import numpy as np
        def g(d):
            im = cv2.imdecode(np.frombuffer(d, np.uint8), cv2.IMREAD_GRAYSCALE)
            return np.float32(cv2.resize(im, (640, 360))) / 255.0
        ga, gb = g(a), g(b)
        win = cv2.createHanningWindow((640, 360), cv2.CV_32F)
        (dx, dy), _resp = cv2.phaseCorrelate(ga, gb, win)
        return round(dx * 2), round(dy * 2)
    except Exception as e:
        logger.warning("frame shift failed: %s", e)
        return (0, 0)


async def _compare_zone(before: bytes, after: bytes, name: str, sink=None) -> dict:
    """前後2枚で、その区画の物が 増えた／減った／同じ かを決める（両方向ルール）。

    返す形は _compare_images と同じ（same / better / changes）ので、_zone_pass はそのまま。
    better＝減った（片づいた方向）。判断できないときは skip を付けて返す。

    sink＝(前が空か, 今が空か)。2026-09-10 夜：「空か」の答え（20/20で安定）を軸にする。
      空→空＝同じ（光が違っても比べない）／物あり→空＝片づいた／空→物あり＝散らかった。
      物あり→物あり（と、どちらか不明）のときだけ、くぼみ以外を塗りつぶしてAIに聞く。"""
    dx, dy = _frame_shift(before, after)
    if abs(dx) > SHIFT_MAX_PX or abs(dy) > SHIFT_MAX_PX:
        # ずれすぎて比べられない。黙って止まると気づけないので記録に残す
        # （2026-09-12 夜、カメラが落ちた。落ちたことは記録から分からなかった）。
        _log_event("aim_off", {"zone": name, "shift": [dx, dy], "stop_px": SHIFT_MAX_PX})
        return {"skip": "shifted", "same": True, "shift": [dx, dy]}
    if name == "シンク":
        eb, ea = (sink or (None, None))
        base = {"shift": [dx, dy], "forward": "rule", "backward": "rule"}
        if eb is True and ea is True:
            return dict(base, same=True, better=None, changes=[], rule="空→空")
        if eb is False and ea is True:
            return dict(base, same=False, better=True, rule="物あり→空",
                        changes=[{"what": "シンクが空になった", "how": "減った", "where": name}])
        if eb is True and ea is False:
            return dict(base, same=False, better=False, rule="空→物あり",
                        changes=[{"what": "シンクに物が置かれた", "how": "増えた", "where": name}])
        if eb is None and ea is True:
            # 前が分からなくても、今が空なら「散らかった」はあり得ない。片づいたかも
            # 分からないので「同じ」（迷ったら何もしない）。光の違いで「増えた」と
            # 言う誤りを、ここで止める（実測：前が不明の空どうしで 3/3 誤り）。
            return dict(base, same=True, better=None, changes=[], rule="不明→空")
        before, after = _sink_mask(before), _sink_mask(after)   # 物あり→物あり か不明
    fix = ZONE_FIXTURES.get(name, "")
    q = (_ZONE_SCENE + fix + "2枚の写真は同じ場所で、1枚目が前、2枚目が今です。"
         "『" + name + "』の中の物は前と比べてどうなりましたか？ 何が変わったかも短く。"
         "JSONだけで答えてください："
         "{\"change\": \"none\" | \"more\" | \"less\", \"what\": [\"変わった物を短く\"]}")
    fw = await _ask_json([_img_block(before), _img_block(after), {"type": "text", "text": q}], 200)
    bw = await _ask_json([_img_block(after), _img_block(before), {"type": "text", "text": q}], 200)
    if fw.get("error") or bw.get("error"):
        return {"error": fw.get("error") or bw.get("error")}
    f, b = fw.get("change"), bw.get("change")
    if f == "more" and b == "less":
        verdict = "more"
    elif f == "less" and b == "more":
        verdict = "less"
    else:
        verdict = "none"                       # 裏返しにならなければ「変化なし」（迷ったら何もしない）
    what = fw.get("what") if isinstance(fw.get("what"), list) else []
    changes = [{"what": str(w)[:20], "how": "減った" if verdict == "less" else "増えた",
                "where": name} for w in what[:5]] if verdict != "none" else []
    return {"same": verdict == "none",
            "better": True if verdict == "less" else (False if verdict == "more" else None),
            "changes": changes, "forward": f, "backward": b, "shift": [dx, dy]}


@router.post("/compare")
async def compare(before: UploadFile = File(...), after: UploadFile = File(...),
                  focus: str = "", x_upload_key: str = Header(None)):
    """前後2枚を見比べる（試験用の窓口）。記録も保存もしない。
    focusに場所の名前を渡すと、そこだけを見比べる。"""
    if UPLOAD_KEY and x_upload_key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    a, b = await before.read(), await after.read()
    return await _compare_zone(a, b, focus or "シンク")     # 本番と同じ見方C（2026-09-09）


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
AIM_REF_OBJ = "spirit/aim_ref.jpg"    # 画角の見本。カメラが落ちたり動いたときに、ここへ戻す
AIM_WARN_PX = 60                      # これを超えたら「ずれている」と言う（比較が止まるのは150px）
BASELINE_OBJ = "spirit/zonecheck/baseline.jpg"        # 旧・単一の基準（読み残し用）
BASELINE_PREFIX = "spirit/zonecheck/base_"           # 向きごとの基準写真

# ■ 二つの持ち場（2026-09-06）
#
# カメラは1台なので、同じ瞬間に「入り口」と「キッチン」の両方は見られない。
# だが、この二つは必要になる時刻が違う。
#
#   顔を撮る       … 秒単位。人は数秒で通り過ぎる。鳴ってから振ったのでは遅い
#   場所の変化を測る … 分単位。誰も居なければ、いつ撮っても同じ
#
# そこで、普段は入り口を向いたまま待つ（いつでも顔を撮れる状態）。
# 誰も居ないと分かってからキッチンへ見に行き、1枚撮って、また戻る。
# 前後比較は「誰も居ないキッチンの2枚」どうしなので成立する。
#
# 見張りはカメラではなく人感センサーがしている（24時間で110回）。
# カメラを見張りから降ろせるのは、そのおかげ。
CHECK_QUIET_SEC = 180.0     # 人が去ってこれだけ静かなら、見に行ってよい
CHECK_GAP = 300.0           # 見回り同士の最短間隔。2026-09-09: 1800→300。
CHECK_SELF_SEC = 90.0       # 見回りのあと、これだけの間の「動き」はカメラ自身の首振り（人ではない）
                            # 「人が来るたびに去ったあと1回」に変えたので、30分だと
                            # 続けて来た人の分を取りこぼす。誰も来なければ出さないので、
                            # 短くしても無駄打ちにはならない


def _baseline_key(pose: str) -> str:
    """その向き専用の基準写真の置き場。

    向きごとに分けないと、入り口の1枚とキッチンの1枚を見比べることになり、
    何も起きていなくても全部変わって見える。"""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (pose or "none"))
    return BASELINE_PREFIX + safe + ".jpg"
SWEEP_HINT_SEC = 120.0      # 人感が鳴ってから、目が探しに行く猶予
VISIT_END_GAP = 90.0        # 最後に人を見てからこれだけ経てば「去った」
VISIT_HOLD = 300.0          # 顔が取れた人を、この秒数は「まだ居る」として扱う
BASELINE_MAX_AGE = 7200.0   # 「前」の写真がこれより古ければ捨てて取り直す。
                            # 実際に5時間半をまたいだ比較が10件の誤記録を生んだ
ZONE_ALL_RATIO = 0.6        # この割合以上の区画が同時に変われば、場所の話ではない
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
    """見張る区画。2026-09-09からは ZONE_NAMES に決め打ち（シンクだけ）。

    AIに起こさせる方式（下）は残してあるが使わない。見る先を人が決めたので、
    区画の名前がぶれない。首振りで区画を増やすときは ZONE_NAMES に足す。"""
    # 記録の欄（試した数・当たり・誤報・状態）も揃えて返す。名前だけだと、
    # あとで誤報率を出すところで KeyError: 'false' になり、比較が途中で落ちた
    # （2026-09-10 15:32・15:43 の zone_error）。
    return [{"name": n, "trials": 0, "hits": 0, "false": 0, "state": "採用"} for n in ZONE_NAMES]
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
    if z.get("trials", 0) < ZONE_MIN_TRIALS:
        z["state"] = "試用中"
        return
    rate = z.get("false", 0) / max(1, z.get("trials", 0))
    z["state"] = "見送り" if rate > ZONE_MAX_FALSE else "採用"


async def _zone_pass(st: dict, before: bytes, after: bytes,
                     who: list, quiet: bool, seen_by=None, sink=None) -> None:
    """区画ごとに前後を見比べて、結果を記録する。

    quiet＝この間、誰も来ていない。そこで出た変化はすべて誤報とみなす。"""
    zones = _live_zones(st)
    cared = []                                 # 片づいた方向に変わった区画
    if quiet:                                  # 点検は1区画ずつ順ぐりに（費用のため）
        i = st.get("zone_rotate", 0) % max(1, len(zones))
        zones = zones[i:i + 1]
        st["zone_rotate"] = i + 1
    results = []
    for z in zones:
        r = await _compare_zone(before, after, z["name"],     # 見方C（両方向ルール）
                                sink if z["name"] == "シンク" else None)
        if r.get("error"):
            logger.warning("zone compare failed (%s): %s", z["name"], r["error"])
            continue
        if r.get("skip"):
            _log_event("zone_skip", {"zone": z["name"], "why": r["skip"],
                                     "shift": r.get("shift")})
            st["patrol_zone"] = z["name"] + " 比べず（ずれ）"
            continue                           # 全体が写っていない → 何もしない
        results.append((z, r))
        st["patrol_zone"] = z["name"] + (
            " 片づいた" if r.get("better") is True else
            " 散らかった" if r.get("better") is False else " 同じ")
        if r.get("changes"):
            st["patrol_zone"] += "（" + "・".join(str(c.get("what"))[:12] for c in r["changes"][:2]) + "）"
        if z["name"] == "シンク":
            # 表情は3段階（0きれい／1ふつう／2散らかっている）。向きだけで動かす。
            lvl = int(st.get("sink_level", 1))
            if r.get("better") is True:
                lvl = max(0, lvl - 1)
            elif r.get("better") is False:
                lvl = min(2, lvl + 1)
            st["sink_level"] = lvl
            st["score"] = st["raw_score"] = lvl / 2.0     # C3は score(0〜1) を読む
    # 片づけも散らかしも、場所ごとに起きる。区画がまとめて変わったなら、
    # それは誰かの手ではなく、光か露出かカメラの向きが変わったということ。
    n_changed = sum(1 for _z, r in results if not r.get("same"))
    if len(results) > 1 and n_changed >= len(results) * ZONE_ALL_RATIO:
        _log_event("zone_all", {"changed": n_changed, "of": len(results),
                                "quiet": quiet, "who": who})
        return
    for z, r in results:
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
                                "seen_by": seen_by, "rule": r.get("rule", "AI"),
                                "changes": r.get("changes") or []})
            if not quiet:
                _tally(z["name"], who, r.get("better"), r.get("changes") or [],
                       seen_by)
                if r.get("better"):
                    cared.append((z["name"], r.get("changes") or []))
    if cared:
        _keep_story(before, after, cared, who)
    _save(st)


# なつき度の動き（2026-09-03・台帳#12改訂に沿う）
# なつき度（設計の数字 2026-09-08 / 実装 2026-09-09）
# 0〜10の整数。初対面0。片づけ1回で+1（その時居た人ぜんぶ）。会わない7日ごとに−1。
# 使って放置しても下げない（台帳#12：「何もしなかったこと」では動かさない。
# 下がるのは会っていない時間だけ。ペットが久しぶりの人によそよそしいのと同じ）。
BOND_MAX = 10
BOND_CARE = 1          # 片づけてくれた → +1
BOND_USE = 0           # 使って、そのままにした → 動かさない
BOND_FADE_DAYS = 7.0   # 会わない日が7日たつごとに −1
# 滞在の扱い（本人決定 2026-09-09 夜）
STAY_MIN = 300.0        # 5分以下の滞在には何も付けない（本人決定：5分から）
VISIT_MERGE_GAP = 1800.0 # 出たり入ったりが30分以内なら、同じ滞在として続ける（本人：30分）
# 一度の滞在で +1 は最大1回（30分居ても+1）。滞在の始まりの時刻を「滞在の番号」として
# 人ごとに覚え、同じ番号では二度と上げない。
_cur_visit = [0.0]      # いま突き合わせている滞在の番号（_zone_cycle が入れる）
_patrol_ups = []        # この見回りでなつき度が上がった人（Notionの1行に書く）
BOND_DAILY_MAX = 3      # 1人1日に上がるのは最大3回（朝・昼・晩のだいたい3回。本人決定）
JST = 9 * 3600


def _jst_day(now: float) -> str:
    """日本時間の日付（1日の上限を数える単位）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now + JST))

# 段階（5つ）。なつき度 → (段階の名前, 地霊への「この相手への接し方」)
# 表情は場所の状態で決まり誰が来ても同じ。人によって変わるのは話し方だけ。
BOND_STAGES = (
    (0, "知らない",     "はじめて見る人。だれだろう、と少しとまどって、もじもじする。名前は聞かない。"),
    (1, "見たことある", "見たことはある人。まだ少し人見知り。小さな声で、短く。"),
    (3, "顔見知り",     "顔見知り。ふつうに、気がるに声をかける。"),
    (6, "なついている", "なついている人。来てくれてうれしい。声がはずむ。"),
    (9, "べったり",     "だいすきな人。まちきれなかった。甘えて、くっつきたい気分。"),
)


def _bond_up(pid: str, why: str, now: float) -> bool:
    """その人のなつき度を1上げる。一度の滞在で最大1回。0〜10で止める。"""
    try:
        ref = get_db().collection("faces").document(pid)
        doc = ref.get().to_dict() or {}
        if _cur_visit[0] and float(doc.get("bond_visit") or 0) == _cur_visit[0]:
            return False                       # この滞在ではもう上げた
        day = _jst_day(now)
        n_today = int(doc.get("bond_day_n") or 0) if doc.get("bond_day") == day else 0
        if n_today >= BOND_DAILY_MAX:
            _log_event("bond_cap", {"person": pid, "day": day})
            return False                       # 今日はもう3回上がった
        level = max(0, min(BOND_MAX, _bond_now(doc) + BOND_CARE))
        ref.update({"bond": level, "bond_at": now, "bond_visit": _cur_visit[0],
                    "bond_day": day, "bond_day_n": n_today + 1, "last_at": now})
        _log_event("bond_up", {"person": pid, "why": why, "bond": level})
        _patrol_ups.append("%s→%d" % (pid, level))
        return True
    except Exception as e:
        logger.warning("bond up failed: %s", e)
        return False


def _bond_stage(level: int) -> tuple:
    """なつき度（0〜10）→ (段階の名前, 接し方の指示文)。"""
    level = max(0, min(BOND_MAX, int(level)))
    name, manner = BOND_STAGES[0][1], BOND_STAGES[0][2]
    for lo, n, m in BOND_STAGES:
        if level >= lo:
            name, manner = n, m
    return name, manner
NEWS_WINDOW = 86400.0  # 「さっき誰かが」と伝えられる範囲


def _bond_now(doc: dict) -> float:
    """いまのなつき度。会っていない時間のぶんだけ薄れる。

    薄れるのは「掃除しなかったから」ではなく「会っていないから」。
    ペットが久しぶりの人によそよそしいのと同じで、罰ではない。"""
    try:
        b = int(round(float(doc.get("bond") or 0)))
    except (TypeError, ValueError):
        b = 0
    last = doc.get("last_at") or doc.get("born") or 0
    days = max(0.0, (time.time() - last) / 86400.0)
    return max(0, min(BOND_MAX, b - int(days // BOND_FADE_DAYS)))


def _manner(doc: dict, alone: bool) -> str:
    """その人への接し方を、地霊への指示文として返す。

    数は決して渡さない（規則4）。渡すのは態度だけ。
    ほかに人が居るときは、そっけなさを引っ込める（規則2）――
    冷たさを第三者が見た瞬間、それは共有され、陰口と同じ回路に乗る。"""
    if not doc:
        return ("初めて見る顔。誰だったか思い出せない。とぼけて、はぐらかす。"
                "名前を尋ねるようなことも言わない。")
    # 2026-09-09: 5段階（0／1-2／3-5／6-8／9-10）に統一。段階の名前と指示文は BOND_STAGES。
    # 「そっけない」段階は無くした。なつき度は0で止まり、下がるのは会わない時間だけなので、
    # 冷たさが罰として働く回路がそもそも生まれない（台帳#12）。alone は将来のために残す。
    return _bond_stage(_bond_now(doc))[1]


def _recent_care() -> bool:
    """最近（NEWS_WINDOW 内）、シンクが片づいた変化があったか。誰がやったかは見ない。"""
    try:
        docs = get_db().collection("spirit_log").order_by(
            "t", direction="DESCENDING").limit(60).stream()
        now = time.time()
        for d in docs:
            e = d.to_dict() or {}
            if now - (e.get("t") or 0) > NEWS_WINDOW:
                break
            if e.get("kind") == "care" or (e.get("kind") == "visit" and e.get("sink_empty")):
                return True
    except Exception as e:
        logger.warning("recent care lookup failed: %s", e)
    return False


def _own_care(pid: str) -> bool:
    """この人が最近、去ったあとにシンクをきれいにしていたか。

    2026-09-10 本人決定：「さっき別のだれかがきれいにしてくれた」は言わない。
    聞いた人が「自分はやっていない」と責められたように感じ、負債感になる。
    本人がやっていたときだけ、本人に「ありがとう」を伝える。
    見るのは、片づいた変化（care）と、去ったあとシンクが空だった滞在（visit）。"""
    try:
        docs = get_db().collection("spirit_log").order_by(
            "t", direction="DESCENDING").limit(60).stream()
        now = time.time()
        for d in docs:
            e = d.to_dict() or {}
            if now - (e.get("t") or 0) > NEWS_WINDOW:
                break
            if pid not in (e.get("who") or []):
                continue
            if e.get("kind") == "care" or (e.get("kind") == "visit" and e.get("sink_empty")):
                return True
    except Exception as e:
        logger.warning("own care lookup failed: %s", e)
    return False


def _tally(zone: str, who: list, better, changes: list, seen_by=None) -> None:
    """変化を「世話」か「利用」として数え、人ごとの覚えに足す。

    散らかったことは失敗ではない。その場所が使われた証拠である。
    世話だけ数えると「誰も使わない綺麗な場所」と「よく世話される場所」が
    区別できない。二つを対にして初めて場所の生き死にが見える。

    人ごとの数は、あとで地霊の態度に使う。ただし数そのものは誰にも見せない。
    順位や点数が見えた時点で、それは制度になる（台帳#12）。"""
    if better is None:
        return                                   # どちらとも言えない変化は数えない
    kind = "care" if better else "use"
    try:
        _plan_speech(_load(), "better" if better else "worse")
    except Exception as e:
        logger.warning("plan speech failed: %s", e)
    _log_event(kind, {"zone": zone, "who": who, "seen_by": seen_by or [],
                      "what": [c.get("what") for c in changes][:5]})
    if not who:
        return                                   # 誰が居たか分からない変化は人に付けない
    try:
        db = get_db()
        for pid in who:
            ref = db.collection("faces").document(pid)
            doc = ref.get().to_dict() or {}
            # 世話をすればなつく。使って放置しても動かさない（BOND_USE=0）。
            # 「何もしなかったこと」では動かない――通っただけの人に
            # 義務を作らないため（規則3）。+1 は _bond_up（一度の滞在で1回だけ）。
            ref.update({kind + "s": (doc.get(kind + "s") or 0) + 1,
                        "last_at": time.time()})
            if kind == "care":
                _bond_up(pid, "care:" + (zone or ""), time.time())
    except Exception as e:
        logger.warning("tally failed: %s", e)


_zone_busy = [False]       # 突き合わせが二重に走らないようにする札


async def _zone_cycle_bg(st: dict, data: bytes, now: float, pose: str = "") -> None:
    """裏で突き合わせを回し、終わったら札を下ろす。"""
    try:
        await _zone_cycle(st, data, now, pose)
    finally:
        _zone_busy[0] = False


# シンクのくぼみだけを切り出す枠（見る向き -0.70_-1.00 の写真での割合：左, 上, 右, 下）。
# 2026-09-10：写真全体で「空か」を聞くと、水切りかごの食器を数えて空の写真30/30で「空でない」と
# 答えた（9/9の20/20は「物あり」の写真だけで、空の写真は試していなかった）。
# くぼみだけに切って、排水口のふたを備え付けと明記すると、空2枚×10回＝20/20「空」、
# 物あり10/10「空でない」（当日実測）。
SINK_BOX = (0.47, 0.0, 0.86, 0.75)
_SINK_EMPTY_Q = (
    "この写真は、共有キッチンのシンク（流し台のステンレスのくぼみ）の底だけを、真上から写したものです。"
    "くぼみの中に見える『黒い丸いもの』は排水口のふたで、備え付けです。物ではありません。蛇口も備え付けです。"
    "排水口のふた以外に、くぼみの中に置かれている物（食器・コップ・鍋・ざる・スプーンなど）はありますか？ "
    "JSONだけで答えてください：{\"empty\": true または false, \"items\": \"あれば短く\"}"
)


def _sink_crop(data: bytes) -> bytes:
    """写真からシンクのくぼみだけを切り出す。切れなければ元のまま。"""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        c = im.crop((int(w * SINK_BOX[0]), int(h * SINK_BOX[1]), int(w * SINK_BOX[2]), int(h * SINK_BOX[3])))
        buf = io.BytesIO()
        c.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        logger.warning("sink crop failed: %s", e)
        return data


def _sink_mask(data: bytes) -> bytes:
    """シンクのくぼみ以外を灰色で塗りつぶす。塗れなければ元のまま。

    2026-09-10 夜の実測（5組×3回）：水切りかごの入れ替わりを「シンクの容器が減った」と
    読む誤りが 3/3 消え、物の増減は 6/6 正しい。ただし光の違う空どうしは 0/3
    （排水口の見え方の違いを「増えた」と言う）。空どうしは _sink_empty の答えで決め、
    ここへ来るのは「物あり→物あり」のときだけにする（_compare_zone）。"""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size
        box = (int(w * SINK_BOX[0]), int(h * SINK_BOX[1]), int(w * SINK_BOX[2]), int(h * SINK_BOX[3]))
        out = Image.new("RGB", (w, h), (128, 128, 128))
        out.paste(im.crop(box), box[:2])
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        logger.warning("sink mask failed: %s", e)
        return data


async def _sink_empty(data: bytes):
    """シンクのくぼみの中が空か。分からなければ None（動かさない）。

    2026-09-10：くぼみだけに切り出してから聞く（上の SINK_BOX の説明）。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        msg = await client.messages.create(
            model=MODEL, max_tokens=80,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(_sink_crop(data)).decode()}},
                {"type": "text", "text": _SINK_EMPTY_Q}]}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        m = re.search(r"\{.*\}", text, re.S)
        v = json.loads(m.group(0)).get("empty") if m else None
        return v if isinstance(v, bool) else None
    except Exception as e:
        logger.warning("sink empty check failed: %s", e)
        return None


async def _zone_cycle(st: dict, data: bytes, now: float, pose: str = "") -> None:
    """人が去った直後、または長く静かなときに、前後を突き合わせる。

    「前」は最後に無人と確かめた1枚。「後」はいま届いた1枚。
    突き合わせが済んだら、いまの1枚が次の「前」になる。"""
    try:
        # 区画は ZONE_NAMES に決め打ち。古い状態（AIが起こした調理台・棚など）が残っていたら立て直す
        if [z.get("name") for z in (st.get("zones") or [])] != list(ZONE_NAMES):
            st["zones_prev"] = st.get("zones") or []     # なぜ立て直したかを記録に残す
            st["zones"] = await _derive_zones(data)
            if st["zones"]:
                _log_event("zones_set", {"zones": [z["name"] for z in st["zones"]],
                                         "was": [z.get("name") for z in (st.get("zones_prev") or [])]})
        key = _baseline_key(pose)
        base = read_object(key)
        # 時刻も向きごとに持つ。1つしか持っていなかった頃は、2箇所を
        # 往復するたびに「向きが変わった」と判定され、基準を取り直しては
        # 捨てるのを繰り返して、いつまでも比較にたどり着けなかった。
        ats = st.get("baseline_ats") or {}
        stale = now - float(ats.get(pose) or 0) > BASELINE_MAX_AGE
        if base is None or stale:
            # 「前」が無いか、カメラの向きが変わったか、古すぎる。
            # 向きの違う2枚を比べると、何も起きていなくても全部変わって見える
            # （実際にそれで「世話7回」という嘘の記録が付いた）。
            # 時間が空きすぎた2枚も同じで、朝と昼では光がまるで違う。基準を取り直す。
            if base is not None:
                logger.info("baseline reset (%s -> %s, age %ds)",
                            st.get("baseline_pose"), pose,
                            now - st.get("baseline_at", 0))
            upload_to(key, data, "image/jpeg")
            ats[pose] = now
            st["baseline_ats"] = ats
            st["baseline_at"], st["baseline_pose"] = now, pose   # 表示用
            st["sink_empty_prev"] = await _sink_empty(data)      # 次の比較の「前」の答え
            st["visit_people"] = []            # 比べられなかった来訪は数えない
            st["visit_seen"], st["seen_by"] = False, []
            _save(st)
            return
        who = st.get("visit_people") or []
        _patrol_ups.clear()
        st["patrol_zone"] = ""
        # 「静か」＝この間に誰も来ていない。顔が取れたかどうかではない。
        # ここを who で見ていたため、顔が取れない日は片づけまで誤報として
        # 数えられ、区画の評判が下がりつづけていた。
        quiet = not st.get("visit_seen")
        if quiet and now - float(ats.get(pose) or 0) < IDLE_CHECK_GAP:
            return                             # 静かな時は、そう何度も点検しない
        # 滞在の長さ。5分以下しか居なかった人には何も付けない（本人決定 2026-09-09）。
        stay = _stay_seconds(st)
        _cur_visit[0] = float(st.get("visit_start") or now)   # この滞在の番号
        if who and stay <= STAY_MIN:
            _log_event("visit_short", {"who": who, "stay": round(stay)})
            who = []
        # 「今、シンクは空か」は毎回1回聞く（比較の軸にも、+1の判断にも使う）
        empty = await _sink_empty(data)
        await _zone_pass(st, base, data, who, quiet, st.get("seen_by") or [],
                         sink=(st.get("sink_empty_prev"), empty))
        # 真ん中の案（本人決定 2026-09-09）：去った後にシンクが空なら、来る前がどうであれ
        # 居た人ぜんぶに +1。自分の分を片づけて帰った人も、他人の分を片づけた人もなつく。
        # 使って散らかしたままは 0（下げない）。一度の滞在で +1 は1回だけ（_bond_up）。
        if who:
            _log_event("visit", {"who": who, "stay": round(stay), "sink_empty": empty})
            if empty:
                st["sink_level"] = 0                      # 空＝一番きれい（Aは戻す合図）
                st["score"] = st["raw_score"] = 0.0
                for pid in who:
                    _bond_up(pid, "sink_empty", now)
        # 誰も居ない今のうちに、次に来る人向けの一言を文にしておく（声係が音にする）
        await _prepare_greetings(st, now)
        # Notion「地霊の記録」に1行（本人決定 2026-09-10：別アカウントの専用DB）
        try:
            pz = st.get("patrol_zone") or ""
            result = ("片づいた" if "片づいた" in pz else "散らかった" if "散らかった" in pz
                      else "比べず" if ("比べず" in pz or not pz) else "同じ")
            what = pz[pz.find("（") + 1:pz.rfind("）")] if "（" in pz else ""
            sink = "空" if empty is True else ("物あり" if empty is False else "未確認")
            title = "%s 見回り｜%s｜シンク%s" % (time.strftime("%m-%d %H:%M", time.gmtime(now + JST)), result, sink)
            await asyncio.to_thread(_notion_patrol, now, title,
                                    st.get("patrol_url") or st.get("photo_url") or "",
                                    int(st.get("patrol_bytes") or 0),
                                    result, sink, list(who), list(_patrol_ups), what)
        except Exception as e:
            logger.warning("notion patrol failed: %s", e)
            _log_event("notion_error", {"text": ("%s: %s" % (type(e).__name__, e))[:120]})
        upload_to(key, data, "image/jpeg")
        ats[pose] = now
        st["baseline_ats"] = ats
        st["baseline_at"], st["baseline_pose"] = now, pose       # 表示用
        st["sink_empty_prev"] = empty                            # 次の比較の「前」の答え
        st["visit_people"] = []
        st["visit_seen"], st["seen_by"] = False, []
        _save(st)
    except Exception as e:
        logger.warning("zone cycle failed: %s", e)
        # 失敗が表から見えないと、基準の写真が入れ替わらない理由を追えない
        # （2026-09-10 朝、09:19の基準がそのまま残っていた）。記録に残す。
        _log_event("zone_error", {"err": ("%s: %s" % (type(e).__name__, e))[:160]})


@router.get("/aim")
async def aim():
    """いまの画角が、見本からどれだけずれているか（2026-09-12 夜）。

    カメラが落ちた。戻したつもりでも、同じ向きの数値を送っても台座が動いて
    いれば別の場所を写す。そして今までは、ずれても表からは何も見えなかった
    ——ラズパイが「撮れませんでした」と自分の画面に出して5分後にやり直す
    だけで、記録にも画面にも残らない。比較が黙って止まる。

    ここを開いたまま、カメラを手で動かすと、数字が動く。0に近づけば元の画角。
    シンクの切り出し（SINK_BOX）は画面の割合で決め打ちなので、
    ずれたままだとシンクでない場所を見て「空です」と答えてしまう。"""
    ref = read_object(AIM_REF_OBJ)
    now = read_object("spirit/latest.jpg")
    if ref is None:
        return {"ok": False, "error": "見本がありません。いまの画角でよければ /spirit/aim/ref に POST してください"}
    if now is None:
        return {"ok": False, "error": "いまの写真がありません"}
    dx, dy = _frame_shift(ref, now)
    off = max(abs(dx), abs(dy))
    return {"ok": True, "dx": dx, "dy": dy,
            "state": "合っている" if off <= AIM_WARN_PX else
                     ("ずれている" if off <= SHIFT_MAX_PX else "ずれすぎ（比較が止まります）"),
            "warn_px": AIM_WARN_PX, "stop_px": SHIFT_MAX_PX}


@router.post("/aim/ref")
async def aim_ref(key: str = ""):
    """いまの画角を「見本」として覚える。据え付けが決まったときに一度押す。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    data = read_object("spirit/latest.jpg")
    if data is None:
        return {"ok": False, "error": "いまの写真がありません"}
    upload_to(AIM_REF_OBJ, data, "image/jpeg")
    _log_event("aim_ref", {"bytes": len(data)})
    return {"ok": True, "bytes": len(data)}


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
    aim_now = None
    try:
        ref, now = read_object(AIM_REF_OBJ), read_object("spirit/latest.jpg")
        if ref is not None and now is not None:
            dx, dy = _frame_shift(ref, now)
            aim_now = {"dx": dx, "dy": dy, "off": max(abs(dx), abs(dy))}
    except Exception as e:
        logger.warning("aim check failed: %s", e)
    return {"zones": out, "home": st.get("home_pose") or "",
            "paused": bool(st.get("sweep_paused")),
            "aim": aim_now, "aim_warn_px": AIM_WARN_PX,
            "baseline_age": round(time.time() - st.get("baseline_at", 0))
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


STORY_PREFIX = "spirit/story/"


def _post_to_app(zones: list, what: list, before_url: str, after_url: str) -> str:
    """片づいたことを、ありがとうアプリの整備記録として投稿する（2026-09-06）。

    ここが「埋もれているありがとうを掘り起こして届ける装置」の、届ける側。
    今まで地霊とアプリは一本も繋がっておらず、気づいたことは記録に落ちて
    そのまま埋まっていた。整備した本人が投稿するのを待たずに済むようにする。

    名前は入れない。誰がやったか分かっていても入れない（台帳#12）。
    良い知らせは名前なしで第三者へ、という決まりに従う。数も順位も書かない。

    person_name は投稿者の欄なので「だれか」と置く。ありがとうは
    その場所へ向かって送られる。"""
    try:
        import uuid
        from datetime import datetime, timezone, timedelta
        jst = timezone(timedelta(hours=9))
        name = "・".join(zones[:3])
        line = "、".join(x for x in what[:3] if x)
        rid = str(uuid.uuid4())
        get_db().collection("maintenance").document(rid).set({
            "zone_id": "spirit",
            "zone_name": name,
            "person_name": "だれか",
            "content": line,
            "before_photo": before_url,
            "after_photo": after_url,
            "before_suggestion": None,
            "place_line": _sanitize(_load().get("comment") or ""),
            "created_at": datetime.now(jst).isoformat(),
            "thanks_count": 0,
            "status": "completed",
            "by_spirit": True,               # 人の投稿と見分けるための印
        })
        return rid
    except Exception as e:
        logger.warning("post to app failed: %s", e)
        return ""


def _notion_patrol(when: float, title: str, image_url: str, size_bytes: int,
                   result: str = "", sink: str = "", who=None, ups=None, what: str = "") -> None:
    """見回りの結果を Notion の「地霊の記録」に1行足す（2026-09-10・本人決定）。

    タイムラプスとは別のアカウント・別のデータベース。鍵とIDは環境変数
    SPIRIT_NOTION_TOKEN / SPIRIT_NOTION_DB（GitHub Secrets → Cloud Run）。
    1行＝見回り1回。列：時刻・種別・結果・シンク・居た人・なつき度・変化・写真。
    失敗しても本体は止めない。"""
    token = os.environ.get("SPIRIT_NOTION_TOKEN", "")
    dbid = os.environ.get("SPIRIT_NOTION_DB", "")
    if not (token and dbid):
        # 鍵かIDが本番に渡っていない。黙って戻ると原因が追えない（2026-09-10 夜：
        # 22:34の見回りで行が出ず、記録にも何も残らなかった）。
        _log_event("notion_skip", {"token": bool(token), "db": bool(dbid)})
        return
    try:
        import httpx
        from datetime import datetime, timezone, timedelta
        at = datetime.fromtimestamp(when, timezone(timedelta(hours=9)))
        props = {
            "名前": {"title": [{"text": {"content": title[:180]}}]},
            "時刻": {"date": {"start": at.isoformat()}},
            "種別": {"select": {"name": "見回り"}},
        }
        if result:
            props["結果"] = {"select": {"name": result}}
        if sink:
            props["シンク"] = {"select": {"name": sink}}
        if who:
            props["居た人"] = {"multi_select": [{"name": str(w)} for w in who]}
        if ups:
            props["なつき度"] = {"rich_text": [{"text": {"content": "・".join(ups)[:200]}}]}
        if what:
            props["変化"] = {"rich_text": [{"text": {"content": what[:200]}}]}
        payload = {"parent": {"database_id": dbid}, "properties": props}
        if image_url:
            payload["cover"] = {"type": "external", "external": {"url": image_url}}
            props["写真"] = {"files": [{"type": "external", "name": at.strftime("%Y-%m-%d %H:%M") + ".jpg",
                                       "external": {"url": image_url}}]}
        r = httpx.post("https://api.notion.com/v1/pages", json=payload, timeout=15,
                       headers={"Authorization": "Bearer " + token,
                                "Notion-Version": "2022-06-28",
                                "Content-Type": "application/json"})
        if r.status_code != 200:
            logger.warning("notion patrol row failed %s: %s", r.status_code, r.text[:200])
            _log_event("notion_error", {"status": r.status_code, "text": r.text[:120]})
    except Exception as e:
        logger.warning("notion patrol error: %s", e)
        _log_event("notion_error", {"text": str(e)[:120]})


@router.get("/notion_test")
async def notion_test(key: str = ""):
    """Notion「地霊の記録」に試しの1行を書けるかを、その場で確かめる（週1点検にも使う）。

    2026-09-10 夜：見回りは走ったのに行が出ず、原因が推測しかできなかったので足した。"""
    if UPLOAD_KEY and key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")
    token = os.environ.get("SPIRIT_NOTION_TOKEN", "")
    dbid = os.environ.get("SPIRIT_NOTION_DB", "")
    out = {"token": bool(token), "db": bool(dbid), "python": sys.version.split()[0]}
    if not (token and dbid):
        return out
    try:
        import httpx
        from datetime import datetime, timezone, timedelta
        at = datetime.now(timezone(timedelta(hours=9)))
        r = httpx.post("https://api.notion.com/v1/pages", timeout=15,
                       json={"parent": {"database_id": dbid}, "properties": {
                           "名前": {"title": [{"text": {"content": at.strftime("%m-%d %H:%M") + " 試し（点検）"}}]},
                           "時刻": {"date": {"start": at.isoformat()}},
                           "種別": {"select": {"name": "見回り"}}}},
                       headers={"Authorization": "Bearer " + token, "Notion-Version": "2022-06-28",
                                "Content-Type": "application/json"})
        out["status"] = r.status_code
        out["url"] = r.json().get("url") if r.status_code == 200 else r.text[:200]
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


def _keep_story(before: bytes, after: bytes, cared: list, who: list) -> None:
    """片づいた前後の2枚を、その場所の積み重ねとして残す。

    どちらも「人が居ない」と確かめた1枚なので、写真を残さない決まりに触れない。
    数を数えないので、物の個数がぶれても記録は揺らがない。
    ここが「積み重ねが見える→自分もやりたくなる」ための材料になる。"""
    try:
        t = int(time.time())
        a = upload_to(STORY_PREFIX + "%d_a.jpg" % t, before, "image/jpeg")
        b = upload_to(STORY_PREFIX + "%d_b.jpg" % t, after, "image/jpeg")
        zones = [c[0] for c in cared]
        what = [w.get("what") for c in cared for w in c[1]][:5]
        rid = _post_to_app(zones, what, a, b)      # アプリの整備記録にも載せる
        _log_event("story", {"before": a, "after": b, "who": who,
                             "zones": zones, "what": what, "record": rid})
    except Exception as e:
        logger.warning("keep story failed: %s", e)


_STORY_PAGE = """<!doctype html><html lang=ja><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>このばしょの つみかさね</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;padding:18px;background:#faf8f5;color:#3a3630}
 h1{font-size:19px;margin:0 0 4px}
 .lead{font-size:13px;color:#7a7268;margin:0 0 20px;line-height:1.7}
 .item{background:#fff;border-radius:14px;padding:13px;margin-bottom:16px;
       box-shadow:0 1px 4px rgba(0,0,0,.07)}
 .pair{display:grid;grid-template-columns:1fr 1fr;gap:8px;align-items:start}
 .pair img{width:100%;border-radius:9px;display:block}
 .cap{font-size:11px;color:#9a9288;margin-top:3px}
 .when{font-size:12px;color:#7a7268;margin-bottom:8px}
 .zones{margin-top:9px}
 .tag{display:inline-block;background:#eef3ec;color:#4a7c59;border-radius:20px;
      padding:3px 11px;font-size:12px;margin:2px 3px 0 0}
 .none{color:#9a9288;font-size:14px;text-align:center;padding:40px 0;line-height:1.9}
</style>
<h1>このばしょの つみかさね</h1>
<p class=lead>だれかが てを かけてくれた ときの、まえと あとです。<br>
なまえは のこりません。かずも かぞえません。</p>
<div id=list><p class=none>よみこみちゅう…</p></div>
<script>
function show(html){document.getElementById('list').innerHTML=html}
fetch('/spirit/log?limit=200').then(function(r){
  if(!r.ok) throw new Error(r.status);       // 起動直後は503が返ることがある
  return r.json();
}).catch(function(){
  show('<p class=none>いま よみこめませんでした。<br>すこし たってから ひらいてください。</p>');
  return null;
}).then(function(j){
  if(!j) return;
  var ev=(j.events||[]).filter(function(e){return e.kind==='story'});
  if(!ev.length){
    show('<p class=none>まだ なにも ありません。<br>だれかが かたづけてくれたら、ここに ならびます。</p>');
    return;
  }
  var h='';
  ev.forEach(function(e){
    var d=new Date(e.t*1000);
    h+='<div class=item><div class=when>'
      +(d.getMonth()+1)+'がつ'+d.getDate()+'にち '
      +('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2)+'</div>'
      +'<div class=pair><div><img src="'+e.before+'" loading=lazy><div class=cap>まえ</div></div>'
      +'<div><img src="'+e.after+'" loading=lazy><div class=cap>あと</div></div></div>';
    if(e.zones&&e.zones.length){
      h+='<div class=zones>';
      e.zones.forEach(function(z){h+='<span class=tag>'+z+'</span>'});
      h+='</div>';
    }
    h+='</div>';
  });
  show(h);
});
</script></html>"""


@router.get("/story", response_class=HTMLResponse)
async def story_page():
    """積み重ねのページ。だれの名前も、いくつという数も出さない。

    研究の狙いは「積み重ねが見える→自分もやりたくなる」という
    もうひとつの流れを起こすこと。ここは順位表ではないので、
    誰が何回やったかは決して出さない。"""
    return _STORY_PAGE


@router.get("/log")
async def get_log(limit: int = 200):
    """研究データの取り出し口（judge/care/presenceの時系列）。"""
    try:
        docs = get_db().collection("spirit_log").order_by(
            "t", direction="DESCENDING").limit(min(limit, 1000)).stream()
        return {"events": [d.to_dict() for d in docs]}
    except Exception as e:
        return {"events": [], "error": str(e)}
