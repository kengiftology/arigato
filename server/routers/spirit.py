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
import hmac
import json
import os
import sys
import re
import time
import asyncio
import base64
import random
import logging

from fastapi import APIRouter, Request, Header, HTTPException, UploadFile, File
from fastapi.responses import PlainTextResponse, HTMLResponse, Response, JSONResponse

from server.database import get_db
from server.storage import upload_to, list_prefix, delete_prefix, read_object

router = APIRouter(prefix="/spirit", tags=["spirit"])
logger = logging.getLogger("spirit")

from server.keys import key_ok   # 目の認証はタイムラプスと同じ鍵（入れ替え中は新旧どちらも通す）
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
    # 2026-09-22：ここに【見る範囲】（点数と一言はシンクの物だけで）の1行を足したが、外した。
    # AI がシンクの外の物（道具立ての箸・コンロの鍋）を「シンク」と呼び変えて数えただけで、
    # 物の一覧の場所まで不正確になった。点数と一言は、いまはシンクを切り出した写真で作る（receive_frame）。
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

# 止まったら気づけるように、機械ごとに「最後にクラウドへ来た時刻」を持つ（2026-09-17）。
# 9/13夜は本番の入れ替えで12時間、9/16朝はラズパイの電圧不足で14時間止まり、
# どちらも誰も気づけなかった。3つとも決まった間隔で必ず来るので、来なくなったら止まっている。
#   カメラ＝人がいなくても5分おきに1枚／C3＝10秒おきに /m／声の係＝30秒おきに /todo
# メモリにだけ持つ（C3は10秒おきに来るので、毎回保存すると書き込みが増えすぎる）。
# 起動し直すと0に戻るが、そのぶん「起動から◯秒はまだ分からない」として扱う。
_BOOT_AT = time.time()
_ALIVE = {"camera": 0.0, "c3": 0.0, "voice": 0.0}
ALIVE_LIMIT = {"camera": 900, "c3": 120, "voice": 600}   # 来る間隔の3〜12倍。これを超えたら止まっている
# 声の係の 180 は、9/23 19:45 に空振りを出した。作り置きの音を11本まとめて作っていた
# 5分間、覗きに来なかっただけだった（19:41:37 prepared → 19:46:39 まで）。
# 直しは2つ。**音を1本置くたびに生きていると記す**（作業中と停止を取り違えない）のと、
# 上限そのものを 600 に。9/23 の1日を測ると、作り置き1本は中央値18秒・最長234秒
# （505本中3本が180秒超）、いまの一言は判断から声になるまで最長530秒。
# 180 のままでは1日3回の空振りが出る。600 はその両方を覆う。
# （9/24 追記：初め「最長422秒」と書いたが、まとまりの切れ目を1本の時間として
#  数えた誤りだった。正しく測り直した上の数字に差し替えた。結論は変わらない）
# 空振りのメールが続くと本人が見張りを読まなくなり、本当の停止に気づけなくなる。
# カメラの 900（15分）と並べても、声の 600（10分）は釣り合う。
BUSY_CAMERA_LIMIT = 60   # 在室中はこの秒数来なければ異常（人が居る間は1.5秒おきに来る）
ASKING_QUIET = 180       # 呼び名を聞き始めてからこの秒数は、上の見張りを休む
ALIVE_NAME = {"camera": "カメラ（ラズパイの橋渡し役）", "c3": "キャラ（C3）",
              "voice": "声の係（ラズパイ）"}
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
    now = time.time()
    try:
        get_db().collection("spirit_log").add({"t": now, "kind": kind, **data})
    except Exception as e:
        logger.warning("spirit log failed: %s", e)
    _note_change(kind, data, now)


def _note_change(kind: str, data: dict, t: float):
    """片づいた方向の変化を、探さずに取り出せる所へ控える（2026-09-25）。

    思い出をさがす範囲を7日に広げたが、記録は1日約1,800件（大半は写真と人の出入り）で、
    7日ぶんを毎回めくるのは高くつく。起きた瞬間に1つだけ控えておけば、あとは読まずに済む。
    控えが無い古い出来事のためだけに、さかのぼる道（MEMORY_SCAN）を残してある。"""
    good = (kind == "care" or (kind == "zone" and data.get("better"))
            or (kind == "visit" and data.get("sink_empty")))
    # 状態がまだ読まれていないときは触らない（ここから _load を呼ぶと記録が入れ子になる）
    if not good or _state_cache is None:
        return
    if kind == "zone":
        what = "、".join((c.get("what") or "") for c in (data.get("changes") or []) if c.get("what"))
    else:
        what = "シンクが きれいに なっていた"
    if not what:
        return
    try:
        _state_cache["last_change"] = {
            "what": what[:60], "t": t,
            "who": [w for w in (data.get("who") or []) if w]}
        _save(_state_cache)
    except Exception as e:
        logger.warning("last change note failed: %s", e)


def _load() -> dict:
    global _state_cache
    if _state_cache is not None:
        return _state_cache
    # ここに来る＝このサーバーが起動して初めて読む。記憶は保存済みの時点まで戻るので、
    # 「状態が巻き戻った」ように見えたとき、再起動だったのかを記録から確かめられるようにする。
    # pid と起動からの秒数も残す（9/17：6秒おきの boot が本当の起動し直しかを見分けるため）
    _log_event("boot", {"pid": os.getpid(), "up": round(time.time() - _BOOT_AT)})
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
    # 「最後に来た時刻」も一緒に残す（2026-09-24）。クラウドは1日に何度も起動し直すので、
    # 覚えを頭の中だけに置くと、そのたびに「まだ一度も来ていない」に戻る。
    # そのあいだ _health() は「起動したばかりだから」で無事と答え、**止まっている装置が隠れる**。
    # 9/24、橋渡しが1時間51分止まったのに、途中の見張りが「無事」と答えた時間があった。
    # クラウドは同時に何台も動く。台ごとに知っていることが違うので、
    # 自分の知っている分だけで書くと**他の台が知っている分を消してしまう**
    # （9/24 実測：C3 しか受けていない台が保存し、カメラと声の覚えが消えた）。
    # 保存されている分と突き合わせて、新しいほうを残す。
    prev = _alive_saved()
    st["alive"] = {k: max(float(prev.get(k) or 0), float(_ALIVE.get(k) or 0))
                   for k in set(prev) | set(_ALIVE)
                   if max(float(prev.get(k) or 0), float(_ALIVE.get(k) or 0))}
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


def _sanitize(c, limit: int = MAX_COMMENT) -> str:
    if not isinstance(c, str):
        return ""
    # 改行を空白に直したあと、空白が2つ並ぶと読み上げの間が不自然になる（9/25 の見本）
    c = re.sub(r"\s+", " ", re.sub(r"[{}\"\\\n]", " ", c)).strip()
    if any(b in c for b in _BAD):
        return "きょうもおつかれさま"
    if len(c) <= limit:
        return c
    # 上限で機械的に切ると、文の途中で終わる（9/25：「……ゆいちゃんのこと、おもって」）。
    # そのまま声になるので、言いかけのまま鳴る。切るなら文の終わりで切る。
    cut = max(c.rfind(ch, 0, limit + 1) for ch in "。！？…")
    return c[:cut + 1] if cut > 0 else c[:limit]


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


# 切り出した写真（_sink_crop）を判定に渡すときの断り書き。
CROP_NOTE = ("この写真は、シンク（流し台の金属のくぼみ）の底だけを切り出して、上下を直したものです。"
             "右の灰色の帯は隠してある所で、物ではありません。\n")


async def _judge_image(image_bytes: bytes, persona: str = "", sink_empty=None,
                       cropped: bool = False) -> dict:
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
                {"type": "text", "text": (CROP_NOTE if cropped else "") +
                                         "いまのあなたの見た景色です。判断をJSONで。" + (
                    # シンクのくぼみの中は、別の確かめ（切り出して聞く）で答えが出ている。
                    # 2026-09-13：シンクが空なのに一言が「あちこちに物があって、そわそわする」
                    # と言い、聞いた人はシンクのことだと受け取った。写真全体を見るこちらは
                    # 水切りかごの食器を「シンクにある」と数えてしまう（9/9に不採用にした
                    # 数え方が、一言の側に残っていた）。答えを渡して食い違いを止める。
                    "" if sink_empty is None else
                    ("\n※シンクのくぼみの中は空です。シンクに物があるとは言わないでください。"
                     if sink_empty else
                     "\n※シンクのくぼみの中には物があります。"))},
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
_face_span = [0.0]        # いま束ねた顔を、何秒のあいだ見かけていたか
MOVE_MIN = 0.5           # 顔の幅に対して、これだけ離れたら「動いた」
# 2026-09-13：25秒→120秒。使える顔は、思っていたよりずっとまばらにしか来ない。
# 今日の 11:48〜11:56 の滞在（8分）で、照合に使える顔は4枚だけで、間隔は25〜108秒。
# 25秒の窓では2枚が同時に入ることがなく、「3コマ束ねる」が永久に成立しなかった。
# 仕分け済み710枚で、窓の長さと「別人が混ざる危険」を測った（線0.35）：
#    25秒  同じ人の組 1216（0.35以上）／ 別人  0組・最大0.136
#   120秒  同じ人の組 4868                ／ 別人  0組・最大0.136   ← 4倍になって危険は増えない
#   300秒  同じ人の組 9726                ／ 別人  1組・最大0.608   ← ここから混ざる
# 束ねる相手は「似ている顔」で選ぶので、時間を延ばしても他人は入ってこない。
FACE_BUF_SEC = 120.0      # これより古い顔は忘れる
FACE_BUF_MAX = 8          # 同じ顔として束ねる枚数の上限
FACE_BUF_KEEP = 6         # Firestore に持ち回るコマ数（1本512個の数値・6本で約25KB）
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
# 小さい顔は、コマを1枚多く集めてから決める（2026-09-13）。
# 9/13 に届いた顔270件のうち 159件（59%）が 70〜100px で、照合はできるが
# 登録はできない帯に入っていた。「もったいない」という本人の指摘で測り直した。
# 覚えは100px以上の8本・4人・境目0.30。
#    70〜100px  1コマ 本人63.2%/別人8.8%  2コマ 90.8%/5.7%  3コマ **97.6%/0.0%**
#   100〜150px  1コマ 68.8%/4.1%         2コマ 90.3%/0.9%  3コマ 94.7%/0.4%
#   150px以上   1コマ 69.9%/**13.2%**    2コマ 58.1%/21.9% 3コマ 60.1%/20.5%
# 大きい顔ほど良い、は間違いだった。大きく写る＝カメラのすぐ前を通っている
# ということで、画面からはみ出し、動きブレも出る。9/12 22:38 に 336px の顔が
# 別人と判定されたのはこれ。ただし150px以上は52枚しかなく、線を引くには足りない。
#
# なお顔が小さいのは見下ろす角度のせいではない（本人の指摘 2026-09-13）。
# 待つ向きは机のある部屋をほぼ真横に見ていて、顔は正面で写る。小さいのは
# 単純に距離。だから「近づける／寄せる」か「コマを束ねる」かの二択になる。
# 2026-09-25：大きく写った顔は、2コマ目を待たずに1枚で決める。
# 人は数秒で通り過ぎるので、2コマ目が来ないまま終わる回がある（9/25 13:10、本人が
# 149px・0.553 で写ったのに保留のまま通り過ぎた）。本人の分類2,461枚で測ると、
# 140px以上・0.46以上の1枚判定は **95回中95回当たり・外れ0**。
# 0.46 は確定線 0.40 より厳しいので、2コマで決めるより安全側になる。
FACE_ONE_PX = 140         # この幅以上なら1コマで決めてよい
FACE_ONE_SIM = 0.46       # ただし、その1枚の近さがこれ以上のときだけ
FACE_SMALL_PX = 100       # これ未満は「小さい顔」
FACE_MIN_FRAMES_SMALL = 3 # 小さい顔は3コマ揃うまで決めない
# 覚えの8本は「違う見え方」で埋める（2026-09-12 夜）。
# 22:01、本人が自分の p01 と 0.131 しか合わず、新しいIDになった。p01 の8本は
# 全部 14:53〜15:31 の38分間で埋まっていて、昼の光の顔しか持っていなかった。
# 時間帯を1つ伏せて測ると（仕分け済み・6時間帯ある2人）：
#   来た順・満杯で打ち止め（いまの作り） 中央0.354 ／ 結べない 43%
#   似すぎなら入れず、違えば一番かぶった1本と交換  中央0.439 ／ 結べない 13%
# 「時間帯をばらす」だけでは効かない（43%のまま）。効くのは見え方の違い。
FACE_SAME_LOOK = 0.70     # これ以上似た覚えを既に持っていたら、入れない
# 判定を3つに分ける（2026-09-17・研究トークA）。
# 9/16 21:41:59、本人の傾いた顔（起き具合1.42）が p01 0.24 / p02 0.302 で、
# 境目0.30をわずかに超えて p02 に決まり、そのまま p02 の覚えに入った。翌日には
# 本人が p02 に 0.806 で付いた（p02 の27枚中9枚が別人）。
# 根は「境目が低い」「決まるたび無条件に覚え直す」「外れた顔ほど覚えに残る入れ替え」。
#   確定：重心との点数が 正面 0.35 以上／傾き 0.40 以上 → IDを付ける
#   保留：0.25 以上で確定に届かない → IDを付けない・新しい人も作らない・覚えも変えない
#   新規：全員と 0.25 未満 → これまでどおり新しい人の条件を見る
# 本人の分類で再生（9/16 18:06〜9/17 の325枚・何も覚えていない状態から）：
#   別人に付いた顔 今の規則 4枚 → この規則 0枚（実際の本番は9枚）
# 同じく 9/12〜9/15 の2,306枚：別人 3枚 → 0枚（IDが付く顔は 1,008 → 885 に減る）
# 2026-09-23：正面の線を 0.35 → 0.40 に上げた。メガネの女性2人（p13 とその隣の人）が
# 0.357〜0.384 で同じIDに入り、別人に同じIDが付いた（混ざり）。混ざった覚えは本人が
# 見分けるまで直らないので、割れ（同じ人が2つのID）より重い。割れは夜のまとめ直しで
# 自動で戻せる。9/12〜9/15 の本人分類2,306枚で測った損は、名前が付く顔 893→860枚（−3.7%）、
# 別人は 0枚のまま、ID の数も 7個のまま（0.45 まで上げると 732枚＝−18% で上げすぎ）。
FACE_CONFIRM_FRONT = 0.40
FACE_CONFIRM_TILT = 0.40
FACE_HOLD = 0.25
# 覚えに足すのは、正面で・点数 0.45 以上で・最初に覚えた顔（核）と 0.40 以上似ているときだけ。
# 満杯なら、核は残し、重心から一番遠い1本を捨てる（以前は「一番ありふれた1本」を捨てて
# 外れ値＝他人の顔を残していた）。
FACE_LEARN_SIM = 0.45
FACE_CORE_SIM = 0.40
# 至近距離の顔からは新しい人を作らない（2026-09-22・本人決定）。照合には使う。
# 9/20 00:46、本人がカメラのすぐ前で下から見上げた顔（328px・画面幅の14%）は、
# 本人の他の顔と 0.06 しか合わず、別の人（p04）として登録された。9/21 23:28 の
# p12（D の人の割れ・309px）も同じ。9/19〜9/22 に生まれた12個のIDで試算すると、
# 画面幅の12.5%（2304幅で約290px）を超える顔を登録に使わなければ割れ2件を防げて、
# 本当に新しい人は1人も止めない（本当に新しい人の登録時の幅は 76〜280px）。
FACE_ENROLL_MAX_PX = 290
# 「誰とも言い切れない」帯（0.25〜確定の線）に居つづける人を、新しい人として出す条件（2026-09-23）
# 2026-09-23 夜に 5枚10秒 → 3枚5秒 に緩めた。9/23 に6分いた未登録の人が、
# 一度に最大3枚しか続かず登録に届かなかった。登録されない人はその日ぶん取り返せないが、
# 緩めて増える割れは夜のまとめ直しで自動で戻せる。9/12〜9/15 の2,306枚では結果が1枚も
# 変わらない（当時の人は全員ふつうの経路で登録できていたため＝悪化なし）。
FACE_NEW_MIN_N = 3            # そろっていてほしい枚数（正面・100px以上）
FACE_NEW_SIM = 0.50           # 互いの似ている度合い（中央）。同じ人どうしでも 0.48〜0.54 しか出ない
FACE_NEW_SPAN = 5.0           # 何秒にわたって取れているか
FACE_NEW_WINDOW = 120.0       # この秒数より古い顔は忘れる
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


def _remember_face(st: dict, vec: list, px: int, pos=None) -> tuple:
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
    # 2026-09-12 深夜：ためる場所をサーバーの中の変数からここへ移した。
    # Cloud Run は混むとサーバーを複数立ち上げるので、1コマ目と2コマ目が
    # 別のサーバーに届くと、ためた分が見えない。実測：23:30以降の278コマの
    # うち259コマ（93%）が「まだ1コマしかない」で捨てられ、誰も認識できなかった。
    buf = [x for x in (st.get("fbuf") or [])
           if isinstance(x, dict) and x.get("v") and now - float(x.get("t") or 0) <= FACE_BUF_SEC]
    buf.append({"v": vec, "px": px, "t": now, "pos": list(pos) if pos else None})
    st["fbuf"] = buf[-FACE_BUF_KEEP:]
    _face_buf[:] = [(x["v"], x["px"], x["t"], x.get("pos")) for x in st["fbuf"]]
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
    span = (max(x[2] for x in mine) - min(x[2] for x in mine)) if mine else 0.0
    _face_span[0] = span
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


def _learn_memory(vecs: list, vec: list, sim: float, front: bool):
    """確定した顔を覚えに足すか決める（2026-09-17）。足すなら新しい並びを、足さないなら None。

    足すのは、正面で・点数が FACE_LEARN_SIM 以上で・最初に覚えた1本（核）と
    FACE_CORE_SIM 以上似ているときだけ。境目ぎりぎりで決まった顔は、判定には使うが覚えない。
    満杯なら、核（先頭）は残し、残りのうち重心から一番遠い1本を捨てる。"""
    import numpy as np
    try:
        if not front or sim < FACE_LEARN_SIM or not vecs:
            return None
        M = np.asarray([v["v"] for v in vecs], dtype=np.float32)
        v = np.asarray(vec, dtype=np.float32)
        if M.shape[1] != v.shape[0] or float(M[0] @ v) < FACE_CORE_SIM:
            return None
        if len(vecs) < FACE_MEMORY:
            return list(vecs) + [{"v": vec}]
        c = M.mean(axis=0)
        c = c / (np.linalg.norm(c) + 1e-9)
        far = 1 + int(np.argmin(M[1:] @ c))
        return [x for i, x in enumerate(vecs) if i != far] + [{"v": vec}]
    except Exception as e:
        logger.warning("memory learn failed: %s", e)
        return None


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


def _add_candidate(vec: list, px: int, now: float) -> None:
    """IDには早いが質は足りている顔を、心当たりとして取っておく。

    状態の文書に入れるとコマごとに書き直すことになるので、別の置き場にする。"""
    try:
        get_db().collection("pending").document("c%d" % int(now * 1000)).set(
            {"v": vec, "at": now, "px": px})
    except Exception as e:
        logger.warning("candidate add failed: %s", e)


def _take_candidates(vec: list, now: float) -> list:
    """この顔と同じ心当たりを集めて返し、置き場からは消す（使い切り）。

    古いもの・上限を超えたものも、ここで片づける。"""
    import numpy as np
    out = []
    try:
        v = np.asarray(vec, dtype=np.float32)
        docs = list(get_db().collection("pending").stream())
        docs.sort(key=lambda d: (d.to_dict() or {}).get("at") or 0)
        for d in docs[-CAND_MAX:] if len(docs) > CAND_MAX else docs:
            x = d.to_dict() or {}
            w = np.asarray(x.get("v") or [], dtype=np.float32)
            old = now - float(x.get("at") or 0) > CAND_KEEP_SEC
            if not old and w.shape == v.shape and float(np.dot(v, w)) >= FACE_SAME_TRACK:
                out.append(x["v"])
                d.reference.delete()
            elif old:
                d.reference.delete()
        for d in docs[:-CAND_MAX] if len(docs) > CAND_MAX else []:
            d.reference.delete()
    except Exception as e:
        logger.warning("candidate take failed: %s", e)
    return out[-(FACE_MEMORY - 1):]


def _identify(st: dict, data: bytes):
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
    taken = set()        # 同じ写真に写っている2人は別人（2026-09-17）。先に決まったIDは他の顔に付けない
    usable = named = 0   # 照合に使えた顔と、名前が付いた顔（2026-09-24）
    for f in found:
        was = (st.get("last_face") or {}).get("t")
        r = _identify_one(st, f["crop"], f["px"], f.get("edge"), f.get("pos"),
                          f.get("up"), f.get("ratio"), f.get("pts"), f.get("front"),
                          taken=taken)
        # last_face は「照合に使える顔（物ではない）」のときだけ書き換わる。
        # 書き換わったのに名前が付かなかった顔＝未登録の人か、見分けられなかった人。
        if (st.get("last_face") or {}).get("t") != was:
            usable += 1
        _collect_face(f, (r or {}).get("person", ""))
        if r:
            # 確定した顔の枠（2026-09-22）。複数人のとき、服装で相手を指して
            # 話しかけるために使う（研究トークC）。座標は回した後の写真の画素。
            cx, cy = f.get("pos") or (None, None)
            r = dict(r, box_cx=cx, box_cy=cy, box_w=f["px"])
            people.append(r)
            taken.add(r["person"])
    named = len(people)
    if usable > named:
        # 名前のつかない顔が写った時刻をためる（2026-09-24）。世話の記録に unknown_recent を添える。
        now_ = time.time()
        buf = [t for t in (st.get("unknown_at") or []) if now_ - float(t) <= 900]
        st["unknown_at"] = (buf + [now_] * (usable - named))[-40:]
    if not people:
        return {"person": None, "px": px}
    # 先頭＝一番大きく写っている人。いま目の前に居る相手として扱う。
    head = people[0]
    return {"person": head["person"], "state": head["state"], "px": px,
            "all": [x["person"] for x in people],
            "box_cx": head["box_cx"], "box_cy": head["box_cy"], "box_w": head["box_w"],
            "boxes": [{"person": x["person"], "box_cx": x["box_cx"], "box_cy": x["box_cy"],
                       "box_w": x["box_w"]} for x in people]}


def _distinct_enough_to_enroll(st: dict, vec: list, px: int, front: bool) -> bool:
    """「誰とも言い切れない（保留）」人でも、新しい人として登録してよいか（2026-09-23）。

    9/23 17:43〜17:49、未登録の人がキッチンに6分いたのに、登録済みの誰かと 0.25〜0.33 で
    似ていたため、ずっと保留のまま（22回）どのIDにもならなかった。「登録済みの誰とも
    0.25未満」を求めると、少し似ている人は永久に登録されない。
    そこで、**同じ顔が長くはっきり取れている**ときは、保留の帯でも新しい人として出す：
      FACE_NEW_MIN_N 枚以上・時間の広がり FACE_NEW_SPAN 秒以上・互いの中央 FACE_NEW_SIM 以上。
    取り違えではなく「二重のID」が増える向きの緩めなので、間違えても夜のまとめ直し
    （重いモデル・r50）で1つに戻せる。"""
    import numpy as np
    try:
        if not front or px < FACE_SMALL_PX:
            return False
        now = time.time()
        buf = [x for x in (st.get("new_buf") or [])
               if isinstance(x, dict) and x.get("v") and now - float(x.get("t") or 0) <= FACE_NEW_WINDOW]
        buf.append({"v": vec, "t": now, "px": int(px)})
        st["new_buf"] = buf[-8:]
        if len(buf) < FACE_NEW_MIN_N:
            return False
        span = max(x["t"] for x in buf) - min(x["t"] for x in buf)
        if span < FACE_NEW_SPAN:
            return False
        M = np.asarray([x["v"] for x in buf], dtype=np.float32)
        iu = np.triu_indices(len(M), 1)
        med = float(np.median((M @ M.T)[iu]))
        ok = med >= FACE_NEW_SIM
        _log_event("new_from_hold", {"n": len(buf), "span": round(span), "med": round(med, 3),
                                     "px": int(px), "ok": ok})
        return ok
    except Exception as e:
        logger.warning("distinct check failed: %s", e)
        return False


def _identify_one(st: dict, crop, px: int, edge: bool = False, pos=None,
                  up: bool = True, ratio=None, pts=None, front: bool = True, taken=None):
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
    dn = not up
    if dn:
        # 大きく傾いた顔（起き具合1.70超）＝洗い物や食事でうつむいた顔。
        # 2026-09-24 まではここで捨てていた。いちばん見たい場面がまるごと落ちるので、
        # **照合にだけ使う**ことにした。ただし次の3つを守る（2026-09-05 に「誰でも吸い込む網」に
        # なったのと同じ轍を踏まないため）：
        #   ・この1枚だけで決める（数コマの平均に混ぜない。混ぜると正面のコマの点数まで下がり、
        #     決まらなかった顔が後で新しい人になって割れが増える。実測：ID 7個→10個）
        #   ・ここから新しい人は作らない（うつむきは体系的に「知らない人」の側へ寄る）
        #   ・覚えない（_learn_memory は正面だけなので、そのままで満たされる）
        # 9/12〜15 の2,306枚：名前が付いた顔 860→1078枚・別人 0枚のまま・ID 7個のまま。
        front = False
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
    # 照合に使える顔（物ではない）が写った、という事実を、確定かどうかに関係なく残す（2026-09-22）。
    # 段階を渡している人とは明らかに違う顔が写ったら渡すのをやめる、の材料（研究トークD）。
    # 1枚ぶんの、各IDの覚えの重心との近さ。壊れても顔の判定は止めない。
    try:
        # 2026-09-26：この滞在で顔を見はじめた時刻と、最後に見た時刻を持つ（記録だけ・判定には使わない）。
        # 「通り過ぎただけの人」と「立ち寄った人」を後から分けるため（主張#25）。
        # 誰か分からなかった人にも付くので、`visit` の `who` が空でも長さが残る。
        if not st.get("visit_face_first"):
            st["visit_face_first"] = time.time()
        st["visit_face_last"] = time.time()
        st["last_face"] = {"t": time.time(), "px": int(px),
                           "scores": {k: round(v, 3) for k, v in face.score_frames([one], known).items()}}
    except Exception as e:
        logger.warning("last_face failed: %s", e)
    if dn:
        # うつむきは束ねないので「何コマ揃ったか」は数えない。下の枚数の条件は通す
        # （小さい顔の3コマ条件も含めて。9/12〜15 の2,306枚では、通しても別人は0枚のまま
        #  名前が付く顔が 977→1078枚に増えた）。
        frames, n, best_px, spread = [one], FACE_MIN_FRAMES_SMALL, px, 0.0
    else:
        frames, n, best_px, spread = _remember_face(st, one, px, pos)
    need = FACE_MIN_FRAMES_SMALL if best_px < FACE_SMALL_PX else FACE_MIN_FRAMES
    one_ok = False
    if not dn and n < need and known and px >= FACE_ONE_PX:
        # 大きく写った1枚。ここで決めないと、通り過ぎる人は永久に名前が付かない。
        # 近さが FACE_ONE_SIM 以上のときだけ、この1枚で決める。
        try:
            sc = face.score_frames([one], known)
            sc = {k: v for k, v in sc.items() if k not in (taken or ())}
            if sc and max(sc.values()) >= FACE_ONE_SIM:
                frames, n, one_ok = [one], need, True
        except Exception as e:
            logger.warning("one frame check failed: %s", e)
    if n < need and known:
        # まだ1コマしか無い。人が居ることは確かなので、そう伝えるだけにして、
        # 誰かは決めない（次のコマが届けば2枚揃って決まる・数秒後）。
        _log_small("one_frame", px, sim=round(sim1, 3), n=n, need=need)
        return None
    scores = {k: v for k, v in face.score_frames(frames, known).items() if k not in (taken or ())}
    pid = max(scores, key=scores.get) if scores else None
    sim = scores[pid] if pid else 0.0
    line = FACE_ONE_SIM if one_ok else (FACE_CONFIRM_FRONT if front else FACE_CONFIRM_TILT)
    if pid is not None and sim < line:
        if sim >= FACE_HOLD and not _distinct_enough_to_enroll(st, one, px, front):
            # 保留：誰かに似ているが、言い切れない。名前を付けず、新しい人も作らず、覚えも変えない。
            _log_small("hold", best_px, sim=round(sim, 3), who=pid, n=n,
                       ratio=round(ratio, 2) if ratio else None)
            return None
        pid = None                                   # 全員と遠い → 新しい人の候補
    vec = one                                        # 覚えに足すのは、いまの1枚
    note = {"sim": round(sim, 3), "sim1": round(sim1, 3), "n": n, "px": best_px}
    db = get_db()
    if pid is None:                                  # 初めて見る顔
        if dn:
            # うつむきの顔からは新しいIDを出さない（2026-09-24）。うつむきは体系的に
            # 「知らない人」の側へ寄るので、ここから登録すると同じ人が何度も生まれる。
            # 下の not_front でも同じく弾かれるが、理由が「傾きすぎ」だと数えるときに
            # 紛れるので、先にうつむきとして記録する。
            _log_small("dn_no_new", best_px, sim=round(sim, 3), n=n)
            return None
        if not front:
            # 照合には使えるが、新しいIDを出すには傾きすぎ（1.30〜1.70）。
            # 傾いた顔から卵を作ると、そのIDが誰でも吸い込む網になる。
            _log_small("not_front", px, ratio=round(ratio, 2) if ratio else None)
            return None
        if best_px > FACE_ENROLL_MAX_PX:
            # 至近距離。顔の見え方が普段とまるで違い、同じ人でも別人として登録される。
            _log_small("too_close", best_px, sim=round(sim, 3), n=n)
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
        if _face_span[0] < MIN_PRESENCE:
            # 通りすがり。IDは出さないが、顔は心当たりとして取っておく。
            # 同じ人が次に居座ったとき、この分もまとめて覚えの中身になる。
            _add_candidate(vec, best_px, time.time())
            _log_small("passing_by", best_px, n=n, span=round(_face_span[0]))
            return None
        pid = _new_person_id()
        now2 = time.time()
        past = _take_candidates(vec, now2)        # 前に通りかかったときの顔
        db.collection("faces").document(pid).create(     # 既にあれば失敗する（上書きしない）
            {"vecs": [{"v": v} for v in past] + [{"v": vec}],
             "born": now2, "persona": "", "state": "egg"})
        _log_event("arrive", dict(note, person=pid, state="new_egg", from_past=len(past)))
        return {"person": pid, "state": "egg"}
    doc = db.collection("faces").document(pid).get().to_dict() or {}
    vecs = doc.get("vecs", [])
    # 2026-09-12：5枚→8枚。同じ4人・同じ境目での実測で、新しいIDが
    # 生まれる率が 8.2% → 3.8% と半分以下になった。計算は増えない。
    kept = _learn_memory(vecs, vec, sim, front)
    if kept is not None:
        db.collection("faces").document(pid).update({"vecs": kept})
        if len(kept) == len(vecs):
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


def _touch_person(st: dict, pid: str, now: float) -> float:
    """その人の滞在の始まりを覚えて、始まりの時刻を返す（2026-09-13）。

    以前は「誰かの気配が30分とぎれたら滞在おわり」という、家に1つの滞在だった。
    共用キッチンでは誰かしらが通るので30分の無人が来ず、朝8:14に始まった滞在が
    7時間つづいたまま終わらなかった。挨拶もなつき度も、その1滞在に縛られていた。
    本人の案：「**その人が**30分来なければ、その人の滞在は終わり」。
    人ごとに区切れば、共用でも成り立つ。"""
    seen = st.get("seen_at") or {}
    vis = st.get("visit_of") or {}
    if now - float(seen.get(pid) or 0) > VISIT_MERGE_GAP or not vis.get(pid):
        vis[pid] = now
    seen[pid] = now
    st["seen_at"] = {k: v for k, v in sorted(seen.items(), key=lambda x: -x[1])[:8]}
    st["visit_of"] = {k: v for k, v in vis.items() if k in st["seen_at"]}
    return float(vis[pid])


def _person_stay(st: dict, pid: str, now: float) -> float:
    """その人が、いまの滞在にどれだけ居るか（秒）。"""
    v = (st.get("visit_of") or {}).get(pid)
    return (now - float(v)) if v else 0.0


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
    if not key_ok(key):
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
    if not key_ok(key):
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
    if not key_ok(x_upload_key):
        raise HTTPException(status_code=401, detail="bad key")
    _ALIVE["camera"] = time.time()
    _t_body = time.perf_counter()              # 写真を受け取り終えるまでも測る（2026-09-15）
    data = await request.body()
    _body_ms = round(1000 * (time.perf_counter() - _t_body))
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
    _ms = {"body": _body_ms}
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
            res = _identify(st, data)
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
                _touch_person(st, res["person"], now)   # その人だけの滞在を進める
                # 挨拶は「その人を最後に迎えてから GREET_GAP たったら、また」。
                # 2026-09-13：それまでは「同じ滞在で1回だけ」だったが、この家では
                # 滞在が切れない。誰かしらが通るので30分の無人が訪れず、朝8:14に
                # 立った札が7時間そのままで、p01 と10回分かっても一度も鳴らなかった。
                # 滞在という単位はこの共用キッチンでは成立しない。
                # 本人：「聞き逃すとむずむずするので、複数回挨拶してほしい」。
                gm = st.get("greeted_at") or {}
                since = now - float(gm.get(res["person"]) or 0)
                here = _person_stay(st, res["person"], now)
                if here < MIN_PRESENCE:
                    # 通りすがりには声をかけない。居ることは記録に残す
                    _log_small("passing_by", px=res.get("px") or 0,
                               person=res["person"], here=round(here))
                elif since < GREET_GAP:
                    _log_small("greeted_recently", px=res.get("px") or 0,
                               person=res["person"], since=round(since))
                if here >= MIN_PRESENCE and since >= GREET_GAP:
                    gm[res["person"]] = now
                    st["greeted_at"] = {k: v for k, v in sorted(
                        gm.items(), key=lambda x: -x[1])[:8]}      # 直近8人ぶんだけ持つ
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
                    # 呼び名がまだ無い人には、迎えの代わりに呼び名を聞く（2026-09-17）
                    # ほかに人が居るかは、同じ写真に写っている人と、この滞在で見かけた人の両方で見る
                    # （visit_people はこの後で足されるので、ここで合わせる）
                    from server.routers import spirit_name
                    others = (set(st.get("visit_people") or []) | set(res.get("all") or [])) - {res["person"]}
                    # 何人か居るときは、服で「あかい ふくの ひと、…」と呼びかけて聞く（9/22）
                    if not await spirit_name.maybe_ask_async(st, res["person"], doc, now, not others,
                                                             res.get("boxes") or [], data):
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
                # 人ごとの「最後に見た時刻」（2026-09-24）。世話の記録に seen_ago を添えるのに使う。
                sa = dict(st.get("seen_at") or {})
                for pid in res.get("all") or [res["person"]]:
                    sa[pid] = now
                st["seen_at"] = {k: v for k, v in sa.items() if now - float(v) <= 86400}
                _keep_shot(st, now, data, res["person"])
                _lap("shot")
                from server.routers import spirit_name
                # 来訪ごとに1回、こちらから話しかける（2026-09-23）。
                # 迎えの一言が鳴り終わってから（speak_line が空になってから）始める。
                # 呼び名を聞いている最中も、まだ鳴らしていない一言があるときも、始めない。
                # 通りすがりには話しかけない。迎えと同じ線（30秒以上その場に居る人）を使う。
                # 9/23 23:03、7秒しか居なかった人（passing_by）に話しかけてしまった。
                if (not st.get("speak_line") and not spirit_name.asking(st, now)
                        and _person_stay(st, res["person"], now) >= MIN_PRESENCE):
                    await spirit_name.maybe_talk(st, res["person"], None, now)
                _save(st)
                _lap("save")
                _log_ms("face", _ms, len(data))
                return {"ok": True, "person": res["person"], "state": res["state"],
                        "people": res.get("all") or [res["person"]],
                        "judged": False, "why": "person_seen", "ms": _ms,
                        # 呼び名を聞いている相手。ラズパイはこれを見て C3 に鳴らさせ、答えを取りに行く
                        "ask_name": spirit_name.asking(st, now),
                        "ask_sec": spirit_name.asking_sec(st)}
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
        _lap("save")
        return {"ok": True, "judged": False, "why": "wait_for_check",
                "hires": now < st.get("want_hires", 0), "ms": _ms}
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

    # シンクの答えを先に出して、一言のAIにも渡す（2026-09-13）。
    # _zone_cycle でも使うので、ここで1回だけ聞いて持ち回る。
    # 見本（`AIM_REF_OBJ`）と景色が違うなら、シンクの判定をしない（2026-09-22）。
    # 16:25〜22:09、カメラが壁やシンクの外を向いたまま、決め打ちの枠で切り出して
    # 「シンク、きれいだなあ」「ピンクのもの」と言い続け、16:45 には嘘の片づけまで記録した。
    # 違う向きの切り出しに「空か」を聞くと嘘の「空」が出て、滞在の sink_empty から
    # なつき度+1まで付きうるので、そのときは「空か」も聞かずに「分からない」（None）にする。
    view_ok, view_resp, view_shift = _view_ok(data)
    if view_ok:
        sink_now = await _sink_empty(data)
    else:
        sink_now = None
        _log_event("judge_skip", {"scope": "aim_off", "pose": pose, "shift": view_shift, "resp": view_resp})
    st["sink_now"], st["sink_now_at"] = sink_now, now
    # 写真全体（r）からは物の一覧と人数だけを使い、点数と一言はシンクのくぼみだけを
    # 切り出した写真（rs）で作る（2026-09-22 本人「シンクだけ」）。写真全体に「シンクの物だけで
    # 決めて」と文で頼んだら、AI が道具立ての箸・おたまやコンロの鍋を「シンク」と呼び変えて
    # 数えた（16:20）。9/13 の _sink_crop と同じで、文で断るより見せないのが効く。
    # 2つは互いに関係しないので同時に聞く（待ち時間を1回ぶんに保つ。橋渡しは40秒まで待つ）。
    persona = st.get("persona", "")
    crop = _sink_crop(data)
    if not view_ok:                   # 景色が違う：切り出しはシンクでない所を見るので、点数と一言は作らない
        r = await _judge_image(data, persona, sink_now)
        rs, scope = {}, "aim_off"
    elif crop is data:                # 切り出しに失敗すると元の写真が返る。黙って写真全体で作らない
        r = await _judge_image(data, persona, sink_now)
        rs, scope = {}, "crop_failed"
        _log_event("sink_crop_failed", {"pose": pose})
    else:
        r, rs = await asyncio.gather(_judge_image(data, persona, sink_now),
                                     _judge_image(crop, persona, sink_now, cropped=True))
        scope = "sink_crop"
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
    sc = rs.get("score")                  # 点数はシンクの切り出しから（人数と物の一覧は写真全体の r から）
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
        c = _sanitize(rs.get("comment", ""))    # 一言もシンクの切り出しから
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
        aim_shift, aim_conf = _aim_now(data)
        _log_event("judge", {"raw": sc, "score": round(st["score"], 3), "pose": pose, "scope": scope,
                             "N": round(_calc_n(st, now), 3), "comment": st.get("comment", ""),
                             "objects": st.get("objects", []), "people": npeople,
                             "who": st.get("seen_people") or [],
                             "aim_shift": aim_shift, "aim_conf": aim_conf})
    st["seen_people"] = []                # ここまでを1区間として締める
    _save(st)
    # 人が去って落ち着いてから突き合わせる。居る間の1枚を「後」にすると
    # 本人が写り込んでしまい、物の変化と見分けがつかない。
    check = st.get("check_pose") or ""
    right_place = (not check) or (pose == check)   # 見に行く先で撮った1枚か
    if right_place and npeople == 0 and now - st.get("last_seen", 0) > VISIT_END_GAP:
        # 2026-09-14：突き合わせと「前」の差し替えは、返事を返す前に済ませる。
        # それまでは返事を先に返して裏で走らせていたが、Cloud Run は返事のあとの
        # 処理にCPUをほとんど回さず、途中で捨てることもある。9/14 は 10:05・10:12・
        # 10:28 の見回りがどれも一言の準備の手前で止まり、「前」が朝8:28のまま残った。
        # そのため空のシンクを毎回「物あり→空」と読み、同じ片づけを3回数え、
        # 誰も居ないのに「よかった」の一言を積んで、あとで人感が鳴ったときに鳴らした。
        # 区画はシンク1つで、判定はほぼ規則で決まるので、待たせても橋渡し（40秒）に収まる。
        # 遅い仕事（一言の準備・Notion）だけを裏に回す。止まっても比較は狂わない。
        if not _zone_busy[0]:
            _zone_busy[0] = True
            try:
                tail = await _zone_cycle(st, data, now, pose)
            finally:
                _zone_busy[0] = False
            if tail:
                asyncio.create_task(_zone_tail(st, now, **tail))
    logger.info("spirit judge: raw=%s smoothed=%.2f comment=%s", sc, st["score"], st.get("comment"))
    return {"ok": True, "judged": sc is not None, "score": st["score"],
            "hires": now < st.get("want_hires", 0)}


@router.get("/m", response_class=PlainTextResponse)
async def get_m(boot: str | None = None, joy: int | None = None):
    """C3互換: 'score N flag stage'（flag 1=無人）。

    boot（2026-09-19）＝C3が起動して最初の1回だけ添える。on＝電源が入った／
    wd＝クラウドへ5分通らず、C3が自分で起動し直した。いつ・なぜ起動したかを記録に残す。

    joy（2026-09-21）＝C3が「なついている人だ」と喜んだあと、次の1回だけ添える（段階3〜4）。
    9/21朝、p02 が本物の片づけで段階3に届いて戻ってきたのに、喜んだかどうかが
    どこにも残っていなかった。台帳#5・#6 の証拠（いつ・誰に特別に喜んだか）を取るための記録。
    誰に喜んだかはC3は知らないので、クラウドが「その段階を渡した相手」を覚えておいて添える。

    stage（2026-09-19）＝いま居る人のなつき度の段階 0〜4（BOND_STAGES の並び順）。
    誰も居ない・誰か分からないときは 0。末尾に足しただけなので、
    先頭3つしか読まない古いファームはそのまま動く。"""
    if boot in ("on", "wd"):
        _log_event("c3_boot", {"why": boot})
    st = _load()
    now = time.time()
    if joy is not None and 3 <= joy <= 4:
        # 喜んだのは「前の問い合わせで段階 joy を渡した相手」で、知らせが届いた今の人ではない。
        # 2026-09-22 13:15：p02 に段階3を渡して C3 が喜んだあと、知らせが届くまでの10秒で
        # p01 の顔が確定し、喜びが p01 に付いた（p01 は段階2なのに stage 3 と記録された）。
        hit = next((s for s in reversed(_served) if s[2] == joy and now - s[0] <= 30), None)
        if hit:
            who, face_ago, served_ago, src = hit[1], hit[3], round(now - hit[0]), "served"
        else:      # 覚えが無い（別の起動体に当たった・起動し直した）。今の人を使い、そう印を付ける
            who, face_ago = st.get("cur_person"), round(now - st.get("face_at", 0))
            served_ago, src = None, "now"
        _log_event("c3_joy", {"stage": joy, "person": who, "face_ago": face_ago,
                              "served_ago": served_ago, "who_from": src,
                              "person_now": st.get("cur_person")})
    stage = _cur_stage_index(st)
    _served.append((now, st.get("cur_person") if stage else None, stage,
                    round(now - st.get("face_at", 0))))
    del _served[:-12]
    n = _calc_n(st, now)
    # 5つめ＝いま名前を聞いているか（2026-09-23）。C3 は考えている顔と「？」を出す。
    # 速さは橋渡しからの無線の合図に任せる（0.2〜1秒）。ここは合図が届かなかったときの直し
    # なので、10秒遅れても構わない。名前を聞く側（研究トークC）が st["listen_until"] に
    # 「いつまで聞いているか」を入れる。入っていなければ 0 を返す。
    # ここで落ちると /m ごと落ち、C3 が値を受け取れず見張りの再起動を繰り返す（9/22 と同じ轍）。
    # 形が思っていたのと違っても、必ず 0（＝聞いていない）に倒す。
    try:
        listen = 1 if float(st.get("listen_until") or 0) > now else 0
    except Exception:
        listen = 0
    # 6つめ＝撤去期に何を消すか（2026-09-24 本人の決定）。12/7〜12/20 は主張#5 の唯一の対照。
    #   0＝ふつう　1＝画面だけ消す　2＝画面と声を消す
    # 「声も消すかどうか」は12月に決めることになったので、両方作って**選ぶだけ**にしてある。
    # C3 を抜くわけにはいかない（人感も世話の判定も C3 の中にある）ので、
    # 置いたまま見え方・聞こえ方だけを止める。記録する側は何も変えない。
    # 手元の命令ではなくここに置くのは、C3 が起動し直しても10秒で戻すため
    # （C3 は通信が5分絶えると自分で起動し直す。2週間のあいだに必ず何度か起きる）。
    try:
        hide = int(st.get("hide") or 0)
        hide = hide if hide in (0, 1, 2) else 0
    except Exception:
        hide = 0
    return "%.3f %.3f %d %d %d %d\n" % (
        st["score"], n, 1 if st["empty"] else 0, stage, listen, hide)


# 直近の問い合わせで C3 に渡したもの（時刻, 人, 段階, 顔で確かめてからの秒）。
# 喜んだ知らせが来たら、ここから「その段階を渡した相手」を引く。起動体ごと・メモリだけ。
_served: list = []


_stage_memo = [None, 0.0, 0]   # (人, 読んだ時刻, 段階)。C3は10秒おきに来るので、読むのは1分に1回


# 段階を渡し続ける時間（2026-09-22）。VISIT_HOLD（300秒）から分けた。
# 9/22 13:21、p02 が最後に写ってから251秒後に、本人の前で p02 向けの「なついている」の
# 喜びが出た（本人は確定しきれず「いま居る人」が p02 のまま残った）。これまでの喜び11件は、
# 顔の確認から1〜39秒の9件がすべて正しく、251秒の1件が間違い。VISIT_HOLD は片づけの +1 を
# 誰に付けるかにも使うので、そちらは300秒のまま、段階だけを短くする。
STAGE_HOLD = 90.0

# 別の顔が写ったら段階を渡すのをやめる、は使わない（2026-09-23 本人の決定）。
# 「喜んでいるのを他の人に見せるのは問題ない」。9/22 の決定④（温かさは第三者に見せる）と同じ向き。
# 段階が別の人の前で出ても、それは見られて困るものではない、という判断。
# 仕組みは下に残してあるので、必要になったらここを True にすれば戻る。
# STAGE_HOLD（顔で確かめてから90秒）はそのまま。誰も居ないのに段階が残り続けるのは別の問題。
OTHER_FACE_GUARD = False

# 写っている顔が、段階を渡している人とは明らかに違うなら、その時点で渡すのをやめる（2026-09-22）。
# 顔の側（研究トークA）が照合に使える顔を数値にするたびに st["last_face"] を上書きする。
# 本人と p02 の近さは −0.02〜0.10、p02 どうしは 0.45〜0.71（9/21〜22 の実例）で、大きく離れている。
# last_face が無いとき（顔の側の変更がまだ入っていない・うつむき等で数値にならない）は何もしない。
OTHER_FACE_SIM = 0.25     # これ未満なら「渡している人とは違う顔」
LAST_FACE_FRESH = 10.0    # この秒数より古い顔は使わない

# 止める根拠を、ちゃんと見えている顔だけに絞る（2026-09-23）。
# 9/23 05:54:20、同じ人がぶれて流れた143pxの1枚が数値のうえだけ別人寄り（p07 0.334）に出て、
# 段階を渡すのをやめた。害は「喜ばなかった」だけだが、ぶれ・横顔・小さい顔の1枚で止めるのは
# 行きすぎ。顔の側（研究トークA）が last_face に大きさ・正面らしさ・くっきり具合を入れるので、
# それがそろった顔が「続けて2枚とも違う人」のときだけ止める。
# 値が足りない顔は数に入れない（＝ふだんどおり渡す）。止めそこねるより、間違えて止めるほうが困る。
#
# 顔の側（研究トークA・9/23）と合わせる名前
#   front … 正面かどうかの真偽値。帯（ratio 0.80〜1.30）は顔の側が持つので、そのまま使う
#   ratio … 両目の幅に対する「目の中点から口の中点までの長さ」。記録用に添えるだけ
#   blur  … ラプラシアン分散。大きいほどくっきり。112×112 に揃えてから測っている
LF_RATIO, LF_FRONT, LF_BLUR = "ratio", "front", "blur"
OTHER_FACE_MIN_PX = 100          # これより小さい顔は数に入れない
OTHER_FACE_MIN_BLUR = 150.0      # これ未満はぶれている（05:54 の143pxは133で弾かれる）
OTHER_FACE_NEED = 2              # 何枚続けて違う人なら止めるか
OTHER_FACE_RUN_GAP = 30.0        # これより間が空いたら「続けて」ではない

_other_face_run = [0.0, 0]       # (数に入れた最後の顔の時刻, 続けて違う人だった枚数)


def _face_usable(lf: dict) -> bool:
    """止める根拠に使える顔か（大きい・正面・くっきり）。値が足りなければ使わない。"""
    try:
        if float(lf.get("px") or 0) < OTHER_FACE_MIN_PX:
            return False
        front = lf.get(LF_FRONT)
        if front is None or not front:
            return False
        blur = lf.get(LF_BLUR)           # cv2 が失敗すると None
        return blur is not None and float(blur) >= OTHER_FACE_MIN_BLUR
    except Exception:
        return False


def _other_face_now(st: dict, pid: str, now: float) -> bool:
    """いま写っている顔が、pid とは明らかに違う人か。判断できなければ False。"""
    lf = st.get("last_face")
    if not isinstance(lf, dict):
        return False
    # ここで落ちると /spirit/m ごと落ち、C3 が値を受け取れず見張りの再起動を繰り返す。
    # 形が思っていたのと違っても、必ず False（＝ふだんどおり渡す）に倒す。
    try:
        t = float(lf.get("t") or 0)
        if now - t > LAST_FACE_FRESH:
            return False
        scores = lf.get("scores")
        if not isinstance(scores, dict):
            return False
        sc = scores.get(pid)
        if sc is None or not _face_usable(lf):
            return False          # 数に入れない。いまの続き数もそのままにする
        if t != _other_face_run[0]:          # C3 は10秒おきに来る。同じ1枚を二重に数えない
            if t - _other_face_run[0] > OTHER_FACE_RUN_GAP:
                _other_face_run[1] = 0
            _other_face_run[0] = t
            _other_face_run[1] = _other_face_run[1] + 1 if float(sc) < OTHER_FACE_SIM else 0
        return _other_face_run[1] >= OTHER_FACE_NEED
    except Exception:
        return False


# 別の顔で段階を渡すのをやめたときの記録（2026-09-22）。C3 は10秒おきに来るので、
# 毎回書くと同じ行が並ぶ。止まり始めと、止めた相手・写っている別の顔が変わったときだけ書く。
_stage_stop_memo = [None, None]   # (止めた相手, 写っている顔にいちばん近いID)


def _note_stage_stop(st: dict, pid: str, now: float) -> None:
    """stage_stop を1行残す。ここで落ちると /spirit/m ごと落ちるので、何があっても黙って戻る。"""
    try:
        lf = st.get("last_face") or {}
        scores = lf.get("scores") if isinstance(lf.get("scores"), dict) else {}
        best = max(scores, key=lambda k: float(scores[k])) if scores else None
        if _stage_stop_memo == [pid, best]:
            return
        _stage_stop_memo[:] = [pid, best]
        _log_event("stage_stop", {
            "person": pid,                                  # 段階を渡すのをやめた相手
            "person_score": scores.get(pid),                # 写っている顔と、その人との近さ（0.25未満）
            "other_best": best,                             # 写っている顔にいちばん近いID
            "other_score": scores.get(best) if best else None,
            "face_ago": round(now - float(st.get("face_at") or 0)),        # その人を顔で確かめてから
            "last_face_ago": round(now - float(lf.get("t") or 0), 1),      # 別の顔が写ってから
            "px": lf.get("px"),
            "ratio": lf.get(LF_RATIO),                      # 目から口までの長さ（正面らしさ）
            "blur": lf.get(LF_BLUR),                        # くっきり具合（大きいほどくっきり）
            "run": _other_face_run[1]})                     # 続けて違う人だった枚数
    except Exception:
        pass


def _cur_stage_index(st: dict) -> int:
    """いま居る人の段階を 0〜4 で返す。居なければ 0。

    2026-09-19：`cur_person` は人が去っても消えない（消えるのは顔を消したときだけ）。
    そのまま渡すと、何時間も前に帰った人の段階が残り、次に来た別の人に
    その人向けの態度が出てしまう。顔で確かめてから STAGE_HOLD の間だけ渡す。"""
    now = time.time()
    pid = st.get("cur_person")
    if not pid or now - st.get("face_at", 0) > STAGE_HOLD:
        _stage_stop_memo[:] = [None, None]  # 別の顔で止めているのではない。次に止まったらまた記録する
        return 0
    if OTHER_FACE_GUARD and _other_face_now(st, pid, now):   # 既定では切（9/23 本人の決定）
        _note_stage_stop(st, pid, now)
        return 0
    _stage_stop_memo[:] = [None, None]  # 渡している＝止まっていない。次に止まったらまた記録する
    if _stage_memo[0] == pid and now - _stage_memo[1] < 60.0:
        return _stage_memo[2]
    try:
        doc = get_db().collection("faces").document(pid).get().to_dict() or {}
    except Exception:
        return _stage_memo[2] if _stage_memo[0] == pid else 0
    level = _bond_now(doc)
    idx = sum(1 for lo, _n, _m in BOND_STAGES if level >= lo) - 1
    _stage_memo[:] = [pid, now, max(0, idx)]
    return _stage_memo[2]


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
        "stage": _cur_stage_index(st),           # いま居る人の段階0〜4（C3に渡しているのと同じ値）
        "stage_name": BOND_STAGES[_cur_stage_index(st)][1],
        "last_judge_ago": round(now - st["last_judge"]) if st["last_judge"] else None,
        "judge_error": _judge_err[0],
    }


@router.get("/presence", response_class=PlainTextResponse)
async def presence(state: str | None = None):
    """C3が ?state=empty|occupied で報告。引数なしは現在状態を返す（目が撮る前の確認用）。"""
    st = _load()
    if state in ("empty", "occupied"):
        # C3 が生きている印は、この形の呼び出しだけで数える（2026-09-19）。
        # もとは /spirit/m で数えていたが、あれは誰が叩いても更新されるので、
        # 入れ替え後の自動確認や手元の道具が叩くたびに「C3は生きている」と見えた。
        # ?state= を付けて呼ぶのは C3 だけ（spirit_body.ino の8秒ごとの報告）。
        _ALIVE["c3"] = time.time()
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
    if not key_ok(key):
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


def _mark_period(st: dict, name: str, by: str = "") -> None:
    """期間の境目を1行残す（2026-09-24）。

    あとで「いつからいつまでを数えるか」で迷わないための印。
    試運転期→観察期間の切り替わり、撤去期の入り／戻しを、**同じ並びに**置く。
    記録（spirit_log）と状態の両方に残すのは、記録は古いものから流れていくが、
    状態なら `/spirit/full` でいつでも一目で読めるため。決まりどおり、後から動かさない。"""
    try:
        row = {"t": time.time(), "name": str(name)[:60], "by": str(by or "unknown")[:30]}
        ps = st.get("periods")
        st["periods"] = (ps if isinstance(ps, list) else []) + [row]
        del st["periods"][:-50]
        _log_event("period", {k: v for k, v in row.items() if k != "t"})
    except Exception:
        pass


@router.post("/period")
async def period_mark(key: str = "", name: str = "", by: str = ""):
    """期間の境目を記録に残す（2026-09-24）。name を付けなければ、並びを見るだけ。

    例：本番の開始日（10/4 に確定して、後から動かさない）／撤去期の入りと戻し。
    撤去期の切り替えは `/spirit/hide` が自分でここに書くので、手で呼ぶ必要はない。"""
    if not key_ok(key):
        raise HTTPException(status_code=401, detail="bad key")
    st = _load()
    if not name:
        return {"ok": True, "periods": st.get("periods") or [],
                "note": "name を付けると境目を1行残します"}
    _mark_period(st, name, by)
    _save(st)
    return {"ok": True, "periods": st.get("periods") or []}


@router.post("/hide")
async def hide_set(key: str = "", level: int = -1, by: str = ""):
    """撤去期（12/7〜12/20）に、顔と声を消す／戻す（2026-09-24）。

    level 0＝ふつう　1＝画面だけ消す　2＝画面と声を消す。
    level を付けずに呼べば、いまの値を見るだけ。

    **C3 は抜かない。**人感も世話の判定も C3 の中にあるので、抜くと
    「人が来た」「片づけられた」が両方とも記録されなくなり、比べる相手そのものが消える。
    ここで印を立て、`/spirit/m` の6つめで C3 に渡す。手元の命令ではなくここに置くのは、
    C3 が通信の絶えで起き直しても10秒で戻すため（2週間のあいだに必ず何度か起き直す）。

    切り替えた時刻は face_state として残す。あとで期間を分けて数えるときの境目になる。"""
    if not key_ok(key):
        raise HTTPException(status_code=401, detail="bad key")
    st = _load()
    now = int(st.get("hide") or 0)
    if level == -1:                      # 付けなかった（既定値）＝いまの値を見るだけ
        return {"ok": True, "hide": now, "note": "level を付けると変えます（0/1/2）"}
    if level not in (0, 1, 2):           # -2 のような打ち間違いを黙って見過ごさない

        raise HTTPException(status_code=400, detail="level は 0・1・2 のどれか")
    st["hide"] = level
    if level != now:
        _log_event("face_state", {"hide": level, "was": now, "by": by or "unknown"})
        _mark_period(st, ["撤去期：もどす", "撤去期：画面だけ消す", "撤去期：画面と声を消す"][level], by)
    _save(st)
    return {"ok": True, "hide": level, "was": now,
            "note": ["ふつう", "画面だけ消す", "画面と声を消す"][level]}


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
    if not key_ok(key):
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
    """C3が世話イベント検出時に報告してくる。研究の主要指標なので必ず時刻つきで残す。

    2026-09-24：そのとき誰が居たかも一緒に残す。それまでは件数と時刻だけだったので、
    9/16〜9/23 の世話76件のうち51件が誰にも結びつかなかった。
    **1人に決め打ちしない。**居た人を複数のまま、何秒前に見たかと、
    直近10分の「名前のつかない顔」の数を並べて残す。後から数え方を変えられるように。"""
    st = _load()
    now = time.time()
    seen = list(st.get("visit_people") or [])
    sa = st.get("seen_at") or {}
    zl = st.get("zone_last") or {}
    # 10分より古い区画は添えない。分からないものは空のままにする（推測で埋めると後から区別できない）。
    zone = zl.get("name", "") if (now - float(zl.get("t") or 0)) <= 600 else ""
    _log_event("care", {"count": n,
                        "zone": zone,
                        "seen": seen,
                        "seen_ago": {p: round(now - float(sa[p])) for p in seen if p in sa},
                        "unknown_recent": len([t for t in (st.get("unknown_at") or [])
                                               if now - float(t) <= 600])})
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
<ul><li>この場所にある物と、その変化（「皿が3つ増えた」「箱が片づいた」など、言葉での記録）</li>
<li>「片づけられた」というできごとの回数と時刻</li>
<li>顔の特徴を数値にしたもの（同じ人が来たと分かるため。数値から顔画像には戻せません）</li>
<li>人がいた時間帯</li>
<li>答えてくれた方の呼び名（本名でなくてかまいません。答えないこともできます）</li>
<li>話しかけた直後の十数秒に聞き取った言葉（文字にしたもの）</li></ul>
<h2>記録しないもの</h2>
<ul><li>氏名など個人を特定する情報（呼び名を教えてくれた方以外は、「1番の人」「2番の人」としか区別しません）</li>
<li>それ以外の会話（下の「話しかけるとき」の十数秒をのぞき、音は聞いていません）</li>
<li>音そのもの（文字にしたあと、その場で捨てます）</li></ul>
<h2>写真について</h2>
<p>写真は、その場の判断に使ったあと捨てるのが基本です。
ただし<b>いまは装置の見分けが正しいかを確かめている期間</b>のため、人が写った写真と顔の切り抜きを
<b>期限つきで保存しています</b>（研究者本人だけが、見分けの答え合わせのために見ます）。
確かめ期間が終わったら消します。</p>
<h2>話しかけるとき</h2>
<p>キャラクターは、呼び名をたずねるほか、その場に来た方へ<b>ときどき短く話しかけます</b>
（「きょうは なに するの？」など。こちらからは<b>1回の滞在につき1度だけ</b>です）。
話しかけたあと、<b>十数秒のあいだマイクの音を聞き取ります</b>。
お返事いただけた場合は、もう少しだけ続けます（最大6往復・3分まで）。
<b>黙っていれば、それで終わります。こちらから追いかけて話しかけることはありません。</b></p>
<p>聞き取った言葉は<b>文字にして研究の記録として残します</b>（音は残しません）。
そのあいだ、近くで話している方の言葉も文字として残ることがあります。
何人かいるときは、どなたに話しているかを服の色などで伝えるようにしています。
聞き取りには外部のサービス（Googleの音声認識）を使います。音はその処理のためだけに送られます。</p>
<h2>人がいる間も見ています</h2>
<p>誰が何を動かし、誰が戻したかを知るために、人がいる間も観察します。</p>
<p>画像の判断と呼び名の取り出しにはAI（Anthropic社のClaude）を使用しています。データは研究終了時に破棄します。</p>
<p>覚えられたくない方、呼び名を消してほしい方、装置を止めてほしい方はお申し出ください。
以後その方の顔は照合せず、共通のキャラクターで応対します。</p>
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


# 観察メモ・人格の書き込みの合言葉（いたずら防止程度・研究者本人用）。
# 2026-09-23 まで、ここに直に書いてあった文字列が **Wi-Fi と C3 の書き込みの合言葉と同じ**で、
# 公開リポジトリから読めた。1つ見えたら3つとも開く状態だったので、環境変数に移した。
# 決めていないと（空なら）この2つの口は誰にも開かない。
NOTE_KEY = os.environ.get("NOTE_KEY", "")


def _note_key_ok(k) -> bool:
    if not NOTE_KEY:
        raise HTTPException(status_code=503,
                            detail="NOTE_KEY が決まっていません（Cloud Run の環境変数）")
    return hmac.compare_digest(str(k or ""), NOTE_KEY)

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
 else{
   // 合言葉を変えたとき、ブラウザが古いものを覚えたままだと入力欄が出てこなくなる。
   // 断られたら覚えを捨てて、もう一度入れられるようにする（2026-09-23）
   localStorage.removeItem(K);
   const f=document.getElementById('key'); f.style.display=''; f.value='';
   document.getElementById('msg').textContent =
     (r.status===503) ? 'サーバー側で合言葉が決まっていません' : 'あいことばが違うかも。入れ直してください';}}
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
    if not _note_key_ok(body.get("key")):
        raise HTTPException(status_code=401, detail="bad key")
    st = _load()
    st["persona"] = str(body.get("persona", ""))[:2000]
    _save(st)
    _log_event("persona", {"len": len(st["persona"])})
    return {"ok": True, "len": len(st["persona"])}


@router.post("/note")
async def add_note(request: Request):
    body = await request.json()
    if not _note_key_ok(body.get("key")):
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


def _read_first(paths) -> str:
    for p in paths:
        try:
            with open(p) as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def _memory() -> dict:
    """このサーバーのメモリ（MB）と、プロセスの素性（2026-09-17）。

    9/17 に「boot」の記録が6秒おきに570回出た。メモリ不足で落ちているのか、
    落ちていないのに記録だけ出ているのかを、pid と起動からの秒数で切り分ける。"""
    out = {"pid": os.getpid(), "up": round(time.time() - _BOOT_AT)}
    status = _read_first(["/proc/self/status"])
    for line in status.splitlines():
        k, _, v = line.partition(":")
        if k in ("VmRSS", "VmHWM"):                      # いま／起動してからの最大
            out[{"VmRSS": "rss_mb", "VmHWM": "peak_mb"}[k]] = round(int(v.split()[0]) / 1024)
    lim = _read_first(["/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"])
    if lim.isdigit() and int(lim) < 1 << 50:
        out["limit_mb"] = round(int(lim) / 1048576)
    cur = _read_first(["/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"])
    if cur.isdigit():
        out["cgroup_mb"] = round(int(cur) / 1048576)
    ev = _read_first(["/sys/fs/cgroup/memory.events"])
    for line in ev.splitlines():
        k, _, v = line.partition(" ")
        if k in ("oom", "oom_kill"):
            out[k] = int(v)
    return out


_alive_read = [0.0, {}]     # 保存された「最後に来た時刻」の読み置き（時刻, 中身）
ALIVE_READ_GAP = 30.0       # これより新しければ 読み直さない


def _alive_saved() -> dict:
    """保存された「最後に来た時刻」を、そのつど読み直す（2026-09-24）。

    `_load()` は起動時に一度読んだきりを返し続けるので、ここには使えない。
    クラウドは同時に何台も動くことがあり、**写真を受け取っていない台**は
    自分の頭の中に camera の覚えを持たない。その台が起動時の値を握り続けると、
    時間だけが過ぎて、やがて嘘の「止まっている」を出す。
    30秒に一度だけ読み直して、どの台から見ても同じ答えになるようにする。"""
    now = time.time()
    if now - _alive_read[0] < ALIVE_READ_GAP:
        return _alive_read[1]
    try:
        snap = _doc().get()
        _alive_read[1] = ((snap.to_dict() or {}).get("alive") or {}) if snap.exists else {}
    except Exception as e:
        logger.warning("alive read failed: %s", e)
    _alive_read[0] = now
    return _alive_read[1]


def _health() -> dict:
    """機械ごとに、最後に来てから何秒たったか・止まっていそうか。"""
    now = time.time()
    up = now - _BOOT_AT
    items, bad = [], []
    saved = _alive_saved()                     # 起動し直す前・別のインスタンスの「最後に来た時刻」
    for k, limit in ALIVE_LIMIT.items():
        t = max(_ALIVE[k], float(saved.get(k) or 0))
        ago = round(now - t) if t else None
        if t:
            ok = ago <= limit
        else:
            ok = up <= limit          # 一度も来ておらず、保存された覚えも無い（本当の初回）
        items.append({"id": k, "name": ALIVE_NAME[k], "ago": ago, "limit": limit,
                      "ok": ok, "unknown": not t})
        if not ok:
            bad.append(ALIVE_NAME[k])
    # 人が居るのに写真が来ない（2026-09-21）。
    # カメラの目安は15分だが、人が居る間は1.5秒おきに来るはずで、
    # 9/21 は「人感は人を捉えているのに写真0枚」が何度も起きていた。
    # 15分の目安ではそれに気づけないので、在室中だけ別の目安で見る。
    st = _load()
    cam = _ALIVE["camera"]
    # 呼び名を聞いている間は、橋渡しが録音で写真を止める（1往復約25秒×最大5回）。
    # その間に「写真が来ない」と鳴らすと誤報になるので、聞き始めて3分は見ない（2026-09-21）。
    na = st.get("name_ask") or {}
    asking_now = now - float(na.get("at") or 0) < ASKING_QUIET
    if (not st.get("empty", True) and cam and now - cam > BUSY_CAMERA_LIMIT
            and not asking_now):
        items.append({"id": "camera_busy", "name": "人が居るのに写真が来ない",
                      "ago": round(now - cam), "limit": BUSY_CAMERA_LIMIT,
                      "ok": False, "unknown": False})
        bad = bad + ["人が居るのに写真が来ない"]
    return {"ok": not bad, "stopped": bad, "items": items,
            # いま本番で動いているのはどの版か（2026-09-25）。
            # 9/25、入れ替えが5時間止まっていたのに誰も気づかなかった。
            # 橋渡しが 9/23 の写しのまま2日ぶら下がっていたのも同じ形。
            # **外から見えないものは、次も同じだけ気づけない。**
            "version": (os.environ.get("GIT_SHA") or "?")[:7],
            "deployed": os.environ.get("DEPLOYED_AT") or "?",
            "boot_ago": round(up), "now": now, "mem": _memory()}


@router.get("/health")
async def health(strict: int = 0):
    """止まっていないかを外から確かめる口（2026-09-17）。合言葉なしで読める（時刻だけ）。

    strict=1 のときは、止まっている機械があれば 503 を返す。
    GitHub Actions の見張り（.github/workflows/watch.yml）がこれを叩き、
    失敗すると GitHub から本人にメールが届く。"""
    h = _health()
    if strict and not h["ok"]:
        return JSONResponse(h, status_code=503)
    return h


@router.get("/state")
async def state_dump(key: str = ""):
    """いまの状態を、そのまま読む（2026-09-13）。

    「なぜ黙っているのか」「いつの滞在として数えているのか」を外から見る術が
    無く、丸一日 speak が0件の理由が推測しかできなかった。重い中身（特徴量・
    写真）は外して返す。"""
    if not key_ok(key):
        raise HTTPException(status_code=401, detail="bad key")
    st = _load()
    heavy = ("fbuf", "zones", "zones_prev")
    out = {k: v for k, v in st.items() if k not in heavy}
    out["_fbuf"] = len(st.get("fbuf") or [])
    now = time.time()
    for k in ("visit_start", "last_seen", "last_motion", "speak_at", "face_at",
              "baseline_at", "checked_at", "photo_at"):
        if st.get(k):
            out[k + "_ago"] = round(now - float(st[k]))
    return out


@router.get("/export")
async def export_all(since: float = 0.0, limit: int = 20000):
    """論文分析用：機械ログと観察メモをまとめてJSONで返す。

    since（UNIX秒）から後だけを返す（2026-09-24）。古い順に2万件で打ち止めだったため、
    8/30〜9/15 しか取り出せず、9/16以降が読めなかった。#6 の時間差の分析はここを読む。"""
    out = {"spirit_log": [], "fieldnotes": [], "since": since}
    try:
        q = get_db().collection("spirit_log")
        if since:
            q = q.where("t", ">=", float(since))
        out["spirit_log"] = [d.to_dict() for d in q.order_by("t").limit(min(limit, 20000)).stream()]
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
    """匿名IDを発行（連番のみ・誰なのかは記録しない）。

    2026-09-16：番号を使い回さない。以前は「今いる人数＋1」で、p02 を消して
    p01・p03 の2人になると、次の新しい人も p03 になり、元の p03 を
    .set() で上書きしていた（9/15 01:00〜01:06、C と D が交互に p03 を上書き）。
    発行した一番大きい番号を覚えておき、その次を出す。消した番号は二度と使わない。"""
    db = get_db()
    ref = db.collection("spirit_meta").document("ids")
    try:
        last = int((ref.get().to_dict() or {}).get("last_person") or 0)
    except Exception:
        last = 0
    try:
        for d in db.collection("faces").stream():
            m = re.match(r"p(\d+)$", d.id)
            if m:
                last = max(last, int(m.group(1)))
    except Exception:
        pass
    n = last + 1
    try:
        ref.set({"last_person": n}, merge=True)
    except Exception as e:
        logger.warning("person id counter save failed: %s", e)
    return "p%02d" % n


def _new_person_id_floor() -> None:
    """いまある顔の一番大きい番号を、発行済みとして残す（消す前に呼ぶ）。"""
    db = get_db()
    ref = db.collection("spirit_meta").document("ids")
    last = int((ref.get().to_dict() or {}).get("last_person") or 0)
    for d in db.collection("faces").stream():
        m = re.match(r"p(\d+)$", d.id)
        if m:
            last = max(last, int(m.group(1)))
    ref.set({"last_person": last}, merge=True)


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
    if not key_ok(x_upload_key):
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
            db.collection("faces").document(pid).create(   # 既にあれば失敗する（上書きしない）
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
    if not _note_key_ok(body.get("key")):
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
# 入り方の型（2026-09-25・本人「単調さを直す」）。
# 「入り方は毎回変える」と頼む形では変わらなかった。9/22 に頼んだのに、9/25 の見本でも
# 4本中3本が「あ、◯◯ちゃんだ」で始まった。頼むのをやめ、作るたびに型を1つ渡して、
# 構えのほうから変える。1本ずつ別々に作るかぎり、毎回いちばんありそうな入りに寄る。
# 迎えの一言に混ざってはいけないあいさつ言葉（2026-09-25）。
# 店員の口調になり、子どものひとりごとでなくなる。
_GREET_BAD = ("こんにちは", "こんばんは", "おはよう", "いらっしゃい", "ようこそ", "おじゃま")
# お願い・提案の形（2026-09-25 本人「お願いは捨てましょう」）。
# 掟は「命令しない・お願いしない・提案しない」。9/25 の見本に
# 「くっついちゃってもいい？」が出た。**問いかけそのものは捨てない**
# （「なに つくるの？」は掟に触れないし、話が続くもとになる）。
# 捨てるのは、相手に許しを求める形・何かをしてもらう形だけ。
_GREET_BEG = ("おねがい", "てもいい", "ていい？", "てくれる", "てくれない",
              "てほしい", "しようよ", "しない？", "しよう？")

GREET_OPENINGS = [
    "呼び名から始める（『◯◯ちゃん、……』）。",
    "呼び名を最後に置く。文の頭に呼び名を出さない。",
    "自分の気持ちから始める（『うれしいなあ。……』）。",
    "いまの場所の様子から始める（『ここ、しずかだったんだよ。……』）。",
    "ふいに気づいたように、短い声から始める（『あ、』『きゃあ、』）。",
]
# 思い出を話す本の入り方。思い出があるときだけ使う。
# 2026-09-25：ここに『このまえ、……』と例を書いたら、【思い出】が『さっき』と指示して
# いるのに例のほうに引かれ、**正しい一言まで捨てられた**（14本全部）。
# いつのことかは【思い出】だけが決める。ここでは言葉を指定しない。
MEMORY_OPENING = "思い出から始める（いつのことかを言うことばから入る）。"


def _pick_openings(name: str, k: int = 1) -> list:
    """入り方の型をk個えらぶ。重ならないように選ぶ。

    呼び名を知らない相手には、呼び名を使う型を渡さない（渡すと書けない）。"""
    pool = GREET_OPENINGS if name else [o for o in GREET_OPENINGS if "呼び名" not in o]
    return random.sample(pool, min(max(1, k), len(pool)))

_GREET_SYSTEM = (
    "あなたは『きっちんちゃん』。共有キッチンに棲みついている、小さな子どものような地霊です。"
    "いま目の前に人が来ました。"
    "【話し方】ちいさな子どもが、ひとりごとのように。ひらがな多め。"
    "『あのね』『えーとね』『〜なあ』『〜かなあ』『〜だね』のような言い方。"
    "ていねい語（です・ます・いらっしゃいませ・こんにちは）は使わない。店員のようには絶対に言わない。"
    "点々（……）でためらってよい。15字以内。"
    # 2026-09-25：手本が4つのうち2つ「あ、」で始まっていて、作るものもそこへ寄っていた
    "【手本】『あのね、まってたんだよ。』『あれ。えーと……どなたかなあ。』"
    "『きてくれたね。うれしいなあ。』『ここ、しずかだったんだよ。』"
    "【いちばん大事な掟】命令しない・お願いしない・提案しない・責めない。"
    "『片付けて』『〜してね』の類は絶対に言わない。数や回数も口にしない。"
    # 2026-09-10 本人決定。思い出のときだけ禁じていたが、9/25 に思い出の無い一言へ
    # 「また来てくれたんだ」が出た。来かたに触れるのは、どの一言でも負い目になる。
    "『また来た』『ひさしぶり』『しばらく』『なん日ぶり』のように、"
    "その人が来なかった時間に触れることばも言わない。"
    "相手を評価する言葉（えらい・すごい・だめ）も言わない。"
    # 2026-09-25 本人「意味がわかることを発してほしい」。前に来たときの話をなぞった
    # 「きょうもなんか、まぜちゃったんだなあ」が、聞いた人には何のことか分からなかった。
    "【いちばん大事な掟その二】**その一言だけを聞いて、意味が分かるように言う。**"
    "前に何があったかを知らないと分からない言い方はしない。"
    "『あれ』『それ』『いつもの』のように、何を指すか言わずに済ませない。"
    "相手と分かち合えているのは、いまの場所の様子（シンク・ここ）と、"
    "いま目の前にいること、この2つだけだと思って書く。"
    "【この相手への接し方】ここに書かれた気分のとおりに振る舞う。"
    "ただし、その理由（相手が何をした・しなかった）には決して触れない。"
    "返すのは声に出す一言だけ。かぎかっこも説明も、ト書きもいらない。"
)


# 覚えた呼び名で呼ぶか（2026-09-22・本人「その後、名前で呼んでほしい」「ちゃん付け」）。
# 本人が文面の例（/spirit/greet_preview）を見て「よい」と言ったので 9/23 07:1x に入にした。
CALL_NAME = True


MEMORY_ON = True         # 思い出を一言に混ぜるか（2026-09-23。本人が見本を見て「よい」→ 入）
SAID_MIN_LINES = 2       # その人の言葉が何件たまったら、しゃべり方を写すか（2026-09-23）
SAID_MIN_CHARS = 10      # 合計でこれだけの字数がたまってから


async def _greet_line(persona: str, manner: str, thanks: bool = False,
                      news: bool = False, name: str = "", avoid: list | None = None,
                      said: list | None = None, memory: dict | None = None,
                      opening: str = "") -> str:
    """その人へ向けた一言をつくる。

    opening＝入り方の型（2026-09-25）。GREET_OPENINGS から1つ渡す。

    thanks＝この人が前に片づけていた（ありがとうを言う）。
    news＝最近シンクがきれいになっていた（場所の様子として伝える。誰がやったかは言わない）。
    2026-09-10 本人決定：「だれかがきれいにしてくれた」は負債感になるので言わない。
    「シンクがきれいになってた」と場所の様子を言うのはよい。
    name＝その人が教えてくれた呼び名（2026-09-22）。あれば文の中で1回だけ『◯◯ちゃん』と呼ぶ。
    ひらがなで書かせるのは、VOICEVOX が漢字の名前を読み違えないため（「桑原」→「くわはら」）。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ""
    ask = "【この相手への接し方】" + manner
    if opening:
        ask += ("\n【入り方】" + opening +
                "\n**この入り方で書く。ほかの入り方にしない。**"
                "とくに、指示されていないのに『あ、』『あっ』で始めない。")
    if name:
        ask += ("\n【呼び名】この人の呼び名は『%s』。文の中で1回だけ、自然に『（呼び名の読みをひらがなで）ちゃん』と呼ぶ。"
                "呼び名は漢字やカタカナで書かず、読みをひらがなで書く。呼び名のぶんだけ字数を増やしてよい。"
                # 入り方は【入り方】で型を渡して決める（2026-09-25）。ここでは置き場所だけ言う。
                "名前は文の頭・途中・終わりのどこに置いてもよい。"
                "呼び名には必ず『ちゃん』を付ける（呼び捨てにしない）。" % name)
    if avoid:
        # 1本ずつ別々に作ると、毎回いちばんありそうな入りになる（9/23 の見本：4本中3本が同じ入り）。
        # すでに持っている文を見せて、入り方と言い回しを変えさせる。
        ask += ("\n【もう持っている一言】" + "／".join(avoid[:6]) +
                "\nこれらと、最初のことば（入り方）も言い回しも変える。同じ始まり方にしない。"
                # 9/23 の見本で「これは、もう持っている一言ですね。」と返事が混ざった
                "\n返すのは声に出す一言だけ。こちらへの説明・感想・ことわりは一切書かない。")
    # その人のしゃべり方を少しだけ写す（2026-09-23・本人「人ごとに個性を出したい」）。
    # 声も性格の型も変えず、実際に聞こえた言葉から言い方のくせだけを借りる。
    # このまえの思い出を1つ混ぜる（2026-09-23）。回数や「ひさしぶり」は言わない（負い目になる）。
    if memory and memory.get("what"):
        # いつのことかで言い分ける（9/23：0時間前なのに「このまえ」と言っていた）
        h = int(memory.get("hours") or 0)
        when = "さっき" if h < 6 else ("きのう" if h < 36 else "このまえ")
        ask += ("\n【思い出】" + str(memory["what"]) +
                # 2026-09-25：39時間前のことを「さっき」と言った見本が4本中2本あった。
                # ほんとうでないことを言っている。ほかの時のことばは使わせない。
                "（%d時間くらい前のこと。言うときは**『%s』とだけ言う**。"
                "『さっき』『きのう』『このまえ』のうち、**『%s』以外は使わない**）。" % (h, when, when) +
                ("この人が居たときの変化なので、『◯◯ちゃんが やってくれたやつ』のように"
                 "この人のしたこととして言ってよい。ありがとうの気持ちで。"
                 if memory.get("mine") else
                 "誰がやったかは言わない。場所の様子として『%s、〜なってたなあ』と言う。" % when) +
                "\n思い出は1つだけ、短く。**英語やかたい言い方は使わず、子どもの短いひらがなに言い直す**。"
                # 2026-09-25：さかのぼる範囲を7日にしたので、日数に触れる文が出やすくなる。
                "回数（◯回目）・日数（なん日ぶり・3日まえ・先週）・『ひさしぶり』・『しばらく』・"
                "『また来た』のような、**来かたや空いた時間に触れることばは一切言わない**。"
                "いつのことかを言うのは『さっき』『きのう』『このまえ』の3つだけ。")
    said = [s for s in (said or []) if isinstance(s, str) and s.strip()]
    if len(said) >= SAID_MIN_LINES and sum(len(s) for s in said) >= SAID_MIN_CHARS:
        ask += ("\n【この人が実際に言った言葉】" + "／".join("『%s』" % s for s in said[:6]) +
                "\n**写すのは「言い方」だけ。「話していた中身」は写さない。**"
                "この人のくせを**1つだけ選んで、読んで分かるくらいはっきり写す**。"
                "選ぶのは次のどれか：①一言の長さ（短く言い切る／だらだら続ける）"
                "②語尾（『〜した』『〜だね』『〜みたいな』『〜かな』）"
                "③意味を持たない口ぐせ（『なんか』『えーと』『だから』のように、"
                "取り去っても意味の変わらないことば）。"
                # 2026-09-25 本人「まぜちゃったんだなと言われても分からない」
                "\n**中身は持ち出さない。**その人が何を作ったか・何をしたか・何の話をしていたかには"
                "一切ふれない。前に来たときの話をなぞると、聞いた人には何のことか分からない。"
                "\n写すときの線："
                "相手の言葉をそのまま繰り返さない。ていねい語は使わない。"
                "キッチンちゃんは子どもなので、大人の言い回しは子どものことばに直してから写す。"
                "**からかっている・ふざけて真似しているように聞こえてはいけない。**"
                "その人の癖や訛りを笑いものにしない。言いよどみ・つっかえは写さない。")
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
        # 裏で走るので、待ちすぎないよう区切る（既定は10分×再試行2回）
        client = AsyncAnthropic(timeout=30.0, max_retries=1)
        msg = await client.messages.create(
            model=MODEL, max_tokens=120,
            system=(persona or _DEFAULT_PERSONA) + "\n" + _GREET_SYSTEM,
            messages=[{"role": "user", "content": ask}])
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        # 2026-09-25 の見本に「こんにちは」が混ざった。使わない約束は _GREET_SYSTEM に
        # 書いてあるのに、すり抜けた。出てしまったものは捨てる。捨てても作り直しは
        # もう一度まわるので、足りなくなるだけで、おかしな一言は残らない。
        bad = [w for w in _GREET_BAD + _GREET_BEG if w in text]
        # いつのことかを言い違えた一言も捨てる（2026-09-25）。39時間前を「さっき」と
        # 言った見本が4本中2本あった。頼むだけでは直らないので、出たものは捨てる。
        if memory and memory.get("what"):
            bad += [w for w in ("さっき", "きのう", "このまえ") if w != when and w in text]
        if bad:
            _log_event("greet_dropped", {"text": text[:40], "why": "／".join(bad)})
            return ""
        # 呼び名が入るぶん、上限も広げる（読みのひらがな＋「ちゃん」。途中で切れないように）
        return _sanitize(text, MAX_COMMENT + (len(name) * 3 + 3 if name else 0))
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
    if not key_ok(x_upload_key):
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


@router.get("/lines_stored")
async def lines_stored(x_upload_key: str = Header(None)):
    """いま置いてある迎えの一言を、人ごとに全部見せる（2026-09-26）。

    9/25 に「文の途中で切れた一言」が1本見つかった。1本だけ直しても、
    ほかに切れたものが残っていたら意味がないので、まとめて見られる口を作る。
    呼び名が入るので鍵つき。声そのものは返さない。"""
    if not key_ok(x_upload_key):
        raise HTTPException(status_code=401, detail="bad key")
    out = []
    try:
        for d in get_db().collection("faces").stream():
            v = d.to_dict() or {}
            ls = [x.get("t") or "" for x in _slots(v)]
            out.append({"id": d.id, "name": v.get("name") or "",
                        # 言いかけのまま終わっていないか（声になると、そのまま途切れて鳴る）
                        "unfinished": [t for t in ls if t and t[-1] not in "。！？…ー"],
                        "lines": ls})
    except Exception as e:
        return {"people": [], "error": str(e)}
    return {"people": out,
            "unfinished_total": sum(len(p["unfinished"]) for p in out)}


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
async def merge_people(keep: str, drop: str, key: str = "", vecs: str = "drop_first", why: str = ""):
    """割れてしまった2つのIDを1つにまとめる。

    世話・利用の数は足し合わせ、なつき度は大きい方を残し、drop を消す。
    /spirit/similar で「同じ人と判定」と出た組に対して使う。

    vecs（覚えの扱い）：
      drop_first（既定）… drop の見え方を keep の前に置いて8枚に切り詰める（2026-09-09 からの動き）
      keep       … keep の覚えに一切触らない（2026-09-22）。drop の顔が質の悪い1枚
                    （うつむき・至近距離）のとき用。9/17 からは覚えの先頭が「核」なので、
                    drop_first だと drop の顔が核になって keep の覚えが崩れる。
    呼び名：keep に無く drop にあれば、drop の呼び名を keep に移す（2026-09-22）。
    drop の人向けの一言（for_<drop>_*）は消し、まとめた対応は spirit_meta/aliases に残す。"""
    if not key_ok(key):
        raise HTTPException(status_code=401, detail="bad key")
    if vecs not in ("drop_first", "keep"):
        raise HTTPException(status_code=400, detail="vecs must be drop_first or keep")
    if keep == drop:
        raise HTTPException(status_code=400, detail="keep and drop are the same")
    db = get_db()
    a = db.collection("faces").document(keep)
    b = db.collection("faces").document(drop)
    da, dbb = a.get().to_dict(), b.get().to_dict()
    if not da or not dbb:
        return {"ok": False, "error": "そのIDが見つかりません"}
    upd = {"cares": (da.get("cares") or 0) + (dbb.get("cares") or 0),
           "uses": (da.get("uses") or 0) + (dbb.get("uses") or 0),
           "bond": max(float(da.get("bond") or 0), float(dbb.get("bond") or 0))}
    if vecs == "drop_first":
        # 2026-09-09: 消す側(drop)の見え方を前に置く。割れるのは、カメラの向きが
        # 変わって新しい角度の顔が古い顔と結べなかったときなので、新しい角度の
        # 見え方を残さないと、まとめた翌日にまた割れる（p01は古い向きの5枚で
        # 埋まっていて、今日の見下ろす角度の p02〜p04 が全部別人になった）。
        upd["vecs"] = ((dbb.get("vecs") or []) + (da.get("vecs") or []))[:FACE_MEMORY]
    moved_name = None
    if not da.get("name") and dbb.get("name"):
        moved_name = dbb["name"]
        upd["name"] = moved_name
        upd["name_at"] = dbb.get("name_at") or time.time()
    a.update(upd)
    b.delete()
    lines = 0
    try:
        lines = delete_prefix(LINES_PREFIX + "for_%s_" % drop)
        _line_cache["at"] = 0.0
    except Exception as e:
        logger.warning("merge lines cleanup failed: %s", e)
    try:
        # 2026-09-23：戻せるようにする。夜のまとめ直しを自動で走らせる前提なので、
        # 間違ってまとめたときに元へ返せないと、その人の積み重ねが失われる。
        # drop の覚え・世話・なつき度・呼び名を、まるごと控えておく（/spirit/unmerge で戻す）。
        db.collection("spirit_meta").document("aliases").set(
            {drop: {"to": keep, "at": time.time(), "why": why[:120], "name_moved": moved_name,
                    # まとめる前の keep の値も控える（戻すときに、なつき度まで元へ返せる）
                    "keep_before": {k: da.get(k) for k in ("cares", "uses", "bond", "bond_at")},
                    "saved": {k: dbb.get(k) for k in
                              ("vecs", "cares", "uses", "bond", "bond_at", "name", "name_at",
                               "born", "persona", "state", "lines", "last_at")}}}, merge=True)
    except Exception as e:
        logger.warning("merge alias save failed: %s", e)
    st = _load()
    changed = False
    if st.get("cur_person") == drop:
        st["cur_person"] = keep
        changed = True
    for k in ("seen_people", "visit_people", "seen_by"):
        v = st.get(k)
        if isinstance(v, list) and drop in v:
            st[k] = [keep if x == drop else x for x in v]
            changed = True
    if changed:
        _save(st)
    shots = len(upd.get("vecs", da.get("vecs") or []))
    _log_event("merge", {"keep": keep, "drop": drop, "vecs": vecs, "name": moved_name,
                         "lines_deleted": lines, "why": why[:120]})
    return {"ok": True, "keep": keep, "dropped": drop, "shots": shots, "vecs": vecs,
            "name_moved": moved_name, "lines_deleted": lines}


@router.post("/unmerge")
async def unmerge_people(drop: str, key: str = "", why: str = ""):
    """まとめたのを元に戻す（2026-09-23）。

    `spirit_meta/aliases` に控えてある drop の中身（覚え・世話・なつき度・呼び名）で
    drop を作り直し、まとめ先（keep）からは足し算した世話の数を引く。
    覚えは keep のものを触らない（`vecs=keep` でまとめた前提。`drop_first` で
    まとめていた場合は、keep の覚えに drop の顔が混ざったままになる）。"""
    if not key_ok(key):
        raise HTTPException(status_code=401, detail="bad key")
    db = get_db()
    ref = db.collection("spirit_meta").document("aliases")
    al = (ref.get().to_dict() or {}).get(drop)
    if not al or not al.get("saved"):
        return {"ok": False, "error": "控えがありません（まとめる前の中身が残っていない）"}
    keep = al.get("to")
    saved = {k: v for k, v in (al.get("saved") or {}).items() if v is not None}
    if db.collection("faces").document(drop).get().exists:
        return {"ok": False, "error": "%s はもう居ます" % drop}
    db.collection("faces").document(drop).create(saved)
    k = db.collection("faces").document(keep)
    dk = k.get().to_dict() or {}
    if dk:
        before = al.get("keep_before") or {}
        if before:                               # まとめる前の値が控えてあれば、そのまま戻す
            upd = {k: v for k, v in before.items() if v is not None}
        else:                                    # 古い控え（値が無い）なら、足した分を引くだけ
            upd = {"cares": max(0, (dk.get("cares") or 0) - (saved.get("cares") or 0)),
                   "uses": max(0, (dk.get("uses") or 0) - (saved.get("uses") or 0))}
        if al.get("name_moved") and dk.get("name") == al["name_moved"]:
            # まとめたとき drop から移した呼び名。戻すなら keep からは外す（両方に残さない）
            try:
                from google.cloud import firestore as _fs
                upd["name"] = _fs.DELETE_FIELD
                upd["name_at"] = _fs.DELETE_FIELD
            except Exception as e:
                logger.warning("name restore failed: %s", e)
        k.update(upd)
    try:
        from google.cloud import firestore as _fs
        ref.update({drop: _fs.DELETE_FIELD})
    except Exception as e:                       # 控えを消せなくても、戻す作業そのものは終わっている
        logger.warning("alias delete failed: %s", e)
    _log_event("unmerge", {"keep": keep, "drop": drop, "why": why[:120],
                           "shots": len(saved.get("vecs") or [])})
    return {"ok": True, "restored": drop, "from": keep, "shots": len(saved.get("vecs") or [])}


@router.post("/bond")
async def bond_set(who: str = "", value: int = 0, key: str = ""):
    """なつき度を手で書き換える（0〜10で止める）。試験と手直し用。

    2026-09-09: 段階を変えたときに地霊の言い方が変わるかを確かめるための口。
    数そのものは地霊が口に出さない（台帳#12）。研究者だけが触る。"""
    if not key_ok(key):
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
    _stage_memo[1] = 0.0                       # C3へ渡す段階をすぐ読み直させる（別の起動体では最大1分遅れる）
    return {"ok": True, "person": who, "bond": value, "stage": _bond_stage(value)[0]}


@router.post("/people/reset")
async def people_reset(key: str = "", who: str = ""):
    """世話・利用・なつき度をゼロに戻す（顔は覚えたまま）。

    比べてはいけない2枚から作られた記録が10件残った。中身は光と
    カメラの向きの変化で、誰の手でもない。論文の記録として置いておくと
    そのまま嘘になるので、消せる手を用意する。顔まで忘れる必要はない。"""
    if not key_ok(key):
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
    if not key_ok(key):
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
    if not key_ok(key):
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
    if not key_ok(key):
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
async def clear_faces(key: str = "", restart: int = 0):
    """覚えた顔をすべて忘れる。

    誤検出でできたIDが混ざると、以後の照合がその分だけ狂う。
    数が少ないうちは、選んで消すより一度まっさらにするほうが確実。

    restart=1 は「最初からやり直す」（2026-09-16・本人の希望：番号も p01 から）。
    番号を戻すなら、前の人に結びついていたものを全部消す。残すと、新しい p01 に
    前の p01 向けの一言（for_p01_*）が鳴る。restart なしのときは番号を戻さない。"""
    if not key_ok(key):
        raise HTTPException(status_code=401, detail="bad key")
    n = 0
    try:
        # 消す前に、発行済みの一番大きい番号を残す（2026-09-16）。残さないと次の人が
        # また p01 になり、前の p01 向けに作った一言（for_p01_*）がその人に鳴る。
        if not restart:
            _new_person_id_floor()
        for d in get_db().collection("faces").stream():
            d.reference.delete()
            n += 1
        if restart:
            get_db().collection("spirit_meta").document("ids").set({"last_person": 0}, merge=True)
            lines = delete_prefix(LINES_PREFIX + "for_p")    # 人ごとの声（for_new は残す）
            _line_cache["at"] = 0.0
    except Exception as e:
        return {"ok": False, "error": str(e)}
    st = _load()
    if restart:
        st["seen_at"], st["visit_of"] = {}, {}
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
    _log_event("faces_clear", {"deleted": n, "restart": bool(restart)})
    return {"ok": True, "deleted": n, "restart": bool(restart),
            "lines_deleted": lines if restart else 0}


@router.post("/faces/{pid}/vecs")
async def set_face_vecs(pid: str, request: Request, key: str = ""):
    """ある人の覚えを、手で選んだ顔に差し替える（2026-09-17・本人の希望）。

    覚えに別の人が混ざったとき、その人のなつき度・一言・生まれた時刻は残したまま、
    覚えだけを本人が分類した正しい顔に入れ替える。先頭の1本が核になる。
    本文は {"vecs": [[512個の数値], ...]}。人がいなければ作らない（404）。"""
    if not key_ok(key):
        raise HTTPException(status_code=401, detail="bad key")
    if not re.fullmatch(r"p\d+", pid):
        raise HTTPException(status_code=400, detail="bad id")
    body = await request.json()
    vecs = body.get("vecs") or []
    if not vecs or len(vecs) > FACE_MEMORY or any(len(v) != 512 for v in vecs):
        raise HTTPException(status_code=400, detail="vecs must be 1-%d lists of 512" % FACE_MEMORY)
    ref = get_db().collection("faces").document(pid)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="no such person")
    ref.update({"vecs": [{"v": [float(x) for x in v]} for v in vecs]})
    _log_event("face_vecs_set", {"person": pid, "shots": len(vecs), "why": body.get("why", "")[:120]})
    return {"ok": True, "person": pid, "shots": len(vecs)}


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
SPEAK_TTL = 120.0          # 予約した一言は、この秒数を過ぎたら鳴らさずに捨てる（2026-09-14）
                           # C3は居続ける人にだけ1分おきに取りに来るので、2分あれば届く
# 迎える言葉と場所の様子のあいだに挟む息つぎ（16kHz・16bit・モノラルの無音）。
# 0.6秒。続けて鳴らすと一息に聞こえてしまい、2つ言ったことが伝わらない。
BREATH = bytes(2 * int(16000 * 0.6))   # 0.6秒ぶんの無音
PAUSE = bytes(2 * int(16000 * 1.2))    # くり返しの前の、少し長めの間
GREET_GAP = 180.0          # 同じ人を迎え直すまでの間（2026-09-13・本人：3分）
# 通りすがりには声をかけない（2026-09-13・本人）。
# 「数秒しか部屋にいない人は、記録も要らないし、声かけも要らない」。
# 束ねる窓を120秒に広げたので、100秒あけて2回通っただけの人でも
# 2コマ揃ってしまう。その人の顔を見かけた幅が30秒に満たなければ、
# 迎えもしないし、新しいIDも出さない。
MIN_PRESENCE = 30.0
# 通りすがりでも、IDを出せる質の顔なら捨てない（2026-09-13・本人）。
# 「IDをつくるのに必要な情報なので、蓄積させといて良い」。
# 心当たりとして別に貯めておき、その人が本当に居座ったときに、
# まとめて覚えの中身にする。新しいIDが最初から何通りもの見え方を持って生まれる。
CAND_KEEP_SEC = 6 * 3600.0   # 心当たりを取っておく時間
CAND_MAX = 24                # 貯めておく上限

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
        _log_event("speak_skip", {"kind": kind, "why": "たまに黙る"})
        return
    name = _pick_line(kind)
    if not name:
        _log_event("speak_skip", {"kind": kind, "why": "その場面の持ち歌が無い"})
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
VOICE_GAIN = 1.3             # 音量。1.0＝作り置きのまま。本人「下げてよい」→ まず半分（2026-09-10）
# 2026-09-22：本人「もう1段」→ 1.125 → 1.3。D の実測で割れる声 0/40。
# 2026-09-21：本人「音量上げといて」→ 0.75 の1.5倍（約+3.5dB）。GCSの声40本の最大値21,720で、
# 1.125 なら割れる声は0本（割れない上限は約1.5）。まだ小さければ 1.3〜1.5 まで上げられる。
# 2026-09-13：0.5では小さいという本人の指摘。9/10に半分にする前（1.0）との中間にした。
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
    if not key_ok(x_upload_key):
        raise HTTPException(status_code=401, detail="bad key")
    _ALIVE["voice"] = time.time()          # 音を作っている間も生きている（2026-09-23）
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
    if not key_ok(x_upload_key):
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
# その人向けの一言を、何本まで持つか（2026-09-13・本人の希望）。
# 作り置き（「あ、きた。」など）をなるべく使わず、その人ごとの声を増やしたい。
# AIの呼び出しは増やさない：見回りのたびに1本だけ作り、空いている枠に入れる。
# 枠が埋まったら一番古い1本と入れ替える。数回の見回りで4種類たまる。
LINES_PER_PERSON = 4


def _todo_name(pid: str, i: int = 0) -> str:
    return "for_%s_%d" % (pid, i)


def _slots(doc: dict) -> list:
    """その人の一言の枠。[{"t": 文, "m": 声にした文, "at": 時刻}, ...]"""
    ls = doc.get("lines")
    if isinstance(ls, list):
        return [x for x in ls if isinstance(x, dict)][:LINES_PER_PERSON]
    # 前の作り（1本だけ）からの引き継ぎ
    if doc.get("next_text"):
        return [{"t": doc["next_text"], "m": doc.get("made_text", ""), "at": doc.get("next_at", 0)}]
    return []


def _put_line(ls: list, text: str, now: float) -> list | None:
    """新しい一言を枠に入れる。同じ文を既に持っていれば何もしない。"""
    if any((x.get("t") or "") == text for x in ls):
        return None
    ls = list(ls)
    if len(ls) < LINES_PER_PERSON:
        ls.append({"t": text, "m": "", "at": now})
    else:
        ls.sort(key=lambda x: x.get("at") or 0)
        ls[0] = {"t": text, "m": "", "at": now}        # 一番古いものと入れ替え
    return ls


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
            # この人にはどこまで話したか（2026-09-25）。同じ出来事を7日ぶんむし返さない。
            mem = (_recent_memory(d.id, after=float(doc.get("told_change") or 0))
                   if MEMORY_ON else None)
            nm = (doc.get("name") or "") if CALL_NAME else ""
            # 思い出を出すのは4本に1本くらい（2026-09-25）。毎回出すと、どの迎えも同じ話になる。
            if mem and random.random() >= 0.25:
                mem = None
            text = await _greet_line(persona, manner, thanks, news and not thanks, nm,
                                     avoid=[x.get("t") for x in _slots(doc) if x.get("t")],
                                     said=doc.get("said"), memory=mem,
                                     opening=MEMORY_OPENING if mem else _pick_openings(nm)[0])
            if text:
                ls = _put_line(_slots(doc), text, now)
                if ls is not None:
                    upd = {"lines": ls}
                    if mem:
                        upd["told_change"] = mem["t"]
                    d.reference.update(upd)
                    n += 1
        text = await _greet_line(persona, BOND_STAGES[0][2], False, news,
                                 opening=_pick_openings("")[0])
        if text and text != st.get("next_new_text"):
            st["next_new_text"], st["next_new_at"] = text, now
            n += 1
    except Exception as e:
        logger.warning("prepare greetings failed: %s", e)
    if n:
        _log_event("prepared", {"lines": n})
    return n


async def remake_lines(pid: str, n: int = LINES_PER_PERSON, save: bool = True,
                       memory_on: bool | None = None) -> list:
    """その人向けの一言を、呼び名入りで作り直す（2026-09-22）。

    呼び名を覚えた瞬間に呼ぶ。それまでの4本には名前が入っていないので、全部入れ替える。
    声は声係（ラズパイ）が順に作り直す。作り終わるまでは、ふつうの迎えの言葉が鳴る。
    save=False なら作った文を返すだけ（本人に見せる用）。"""
    st = _load()
    ref = get_db().collection("faces").document(pid)
    doc = ref.get().to_dict() or {}
    name = doc.get("name") or ""
    if not name:
        return []
    manner = _bond_stage(_bond_now(doc))[1]
    # 見本（save=False）は、もう話した思い出でも見せる。本人が言い方を見るためのもので、
    # 見せたことは「話した」に入らない。本番に入れるときだけ、話した印を見て・立てる。
    mem = (_recent_memory(pid, after=float(doc.get("told_change") or 0) if save else 0.0)
           if (MEMORY_ON if memory_on is None else memory_on) else None)
    # 入り方は本ごとに変える（2026-09-25）。作り直すのは4本なので、型は重ならない。
    kinds = _pick_openings(name, n)
    # 思い出は4本のうち1本だけに入れる（2026-09-25）。全部に渡すと、どの迎えも
    # 同じ話になる（見本で p01 は4本中3本、p02 は4本中3本がシンクの話になった）。
    mem_at = random.randrange(len(kinds)) if mem else -1
    if mem:
        kinds[mem_at] = MEMORY_OPENING
    texts, got_mem = [], False
    # 型は「何本できたか」ではなく「何回ためしたか」で進める（2026-09-25）。
    # できた数で進めていたら、捨てられた型を8回とも引き直し、**1本も作れなかった**。
    # 1つの型がうまくいかなくても、ほかの型はためされる形にする。
    for i in range(n * 2):                     # 同じ文が出たら数に入れない
        at = i % len(kinds)
        use_mem = bool(mem) and at == mem_at and not got_mem
        t = await _greet_line(st.get("persona", ""), manner, False, False, name,
                              avoid=texts, said=doc.get("said"),
                              memory=mem if use_mem else None,
                              opening=kinds[at])
        if t and t not in texts:
            texts.append(t)
            got_mem = got_mem or use_mem
        if len(texts) >= n:
            break
    if save and texts:
        now = time.time()
        upd = {"lines": [{"t": t, "m": "", "at": now} for t in texts]}
        if got_mem:
            upd["told_change"] = mem["t"]
        ref.update(upd)
        _log_event("lines_remade", {"person": pid, "lines": len(texts)})
    return texts


@router.get("/greet_preview")
async def greet_preview(person: str, n: int = 4, save: int = 0,
                        x_upload_key: str = Header(None)):
    """その人の呼び名を入れた迎えの一言を、試しに作って見せる（2026-09-22）。

    本人が文面を見て決めるための口。save=1 のときだけ、その人の一言を入れ替える。"""
    if not key_ok(x_upload_key):
        raise HTTPException(status_code=401, detail="bad key")
    texts = await remake_lines(person, max(1, min(n, 8)), save=bool(save), memory_on=True)
    doc = get_db().collection("faces").document(person).get().to_dict() or {}
    return {"person": person, "name": doc.get("name"), "said": doc.get("said") or [],
            # さかのぼる範囲は本番と同じ7日（2026-09-25 本人決定）
            "memory": _recent_memory(person),
            "lines": texts, "saved": bool(save and texts)}


@router.get("/todo")
async def todo():
    """声係が覗きにくる：まだ音になっていない一言の一覧。"""
    _ALIVE["voice"] = time.time()
    out = []
    try:
        for d in get_db().collection("faces").stream():
            doc = d.to_dict() or {}
            for i, x in enumerate(_slots(doc)):
                if x.get("t") and x.get("t") != x.get("m"):
                    out.append({"name": _todo_name(d.id, i), "text": x["t"]})
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
    if not key_ok(x_upload_key):
        raise HTTPException(status_code=401, detail="bad key")
    if not re.fullmatch(r"for_[a-z0-9]+_[0-9]", name):
        raise HTTPException(status_code=400, detail="bad name")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    _ALIVE["voice"] = time.time()          # 音を作っている間も生きている（2026-09-23）
    upload_to(LINES_PREFIX + name + ".pcm", data, "application/octet-stream")
    _line_cache["at"] = 0.0
    pid, i = name[len("for_"):-2], int(name[-1])
    if pid == "new":
        st = _load()
        st["made_new_text"] = text
        _save(st)
    else:
        try:
            ref = get_db().collection("faces").document(pid)
            ls = _slots(ref.get().to_dict() or {})
            if i < len(ls):
                ls[i]["m"] = text
                ref.update({"lines": ls})
        except Exception as e:
            logger.warning("todo done update failed: %s", e)
    _log_event("voice_made", {"name": name, "bytes": len(data)})
    return {"ok": True, "name": name, "bytes": len(data)}


def _ready_line(pid: str, doc: dict, st: dict) -> str | None:
    """その人向けに作り置いた一言があれば、その持ち歌の場面名を返す。"""
    if pid == "new":
        ok = st.get("next_new_text") and st.get("next_new_text") == st.get("made_new_text")
    else:
        # 1本でも声になっていれば使う。何本かあれば _pick_line が毎回選び直す。
        ok = any(x.get("m") and x.get("m") == x.get("t") for x in _slots(doc))
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
    # 迎える言葉（hello_*・その人向けの for_*）だけを「あいさつ」として扱う。
    # 2026-09-14：以前は予約された一言をすべてあいさつ扱いにしていたため、
    # 見回りの「よかった」まで、いまの一言を足して2回くり返していた。
    greeting = bool(name) and name.startswith(("hello_", "for_"))
    if name:
        if now < st.get("speak_at", 0):
            return _quiet()                        # まだ。これが間になる
        st["speak_line"] = None                    # 一度鳴らしたら下ろす
        _save(st)
        age = now - float(st.get("speak_at") or 0)
        if age > SPEAK_TTL:
            # 2026-09-14：予約に期限が無く、10:28 に誰も居ない見回りで積んだ一言が
            # 40分後の 11:07、人感が鳴った瞬間に鳴った。その場に向けた言葉ではない。
            _log_event("speak_expired", {"line": name, "age": round(age)})
            return _quiet()
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
    parts = [name]
    if greeting:
        # 迎える言葉のあとに、いまの場所の様子を続ける（2026-09-13・本人の希望）。
        # 「あ、きた。」……「あのね、ちょっとね……そわそわするなあ。」のように、
        # 一息おいて2つづけて鳴らす。C3は取りに来るたび1本しか鳴らせないので、
        # 2本を1本につないで渡す。
        if st.get("say_text") and st.get("say_text") == (st.get("comment") or ""):
            parts.append(SAY_NAME)
        else:
            m = _pick_line("worse" if st.get("score", 0) >= M_HI else "alone")
            if m:
                parts.append(m)
    pcms = []
    for nm in parts:
        try:
            b = read_object(LINES_PREFIX + nm + ".pcm")
        except Exception as e:
            logger.warning("line read failed (%s): %s", nm, e)
            b = None
        if b:
            pcms.append((nm, b))
    if not pcms:
        return _quiet()
    pcm = pcms[0][1]
    for nm, b in pcms[1:]:
        pcm = pcm + BREATH + b                     # 息つぎを挟んでつなぐ
    if greeting:
        # 同じことを2回言う（2026-09-13・本人の希望）。
        # 「一回聞き逃しても、3分待たなくていいように」。
        # 独り言をくり返すのは、子どもらしさとしても不自然ではない。
        pcm = pcm + PAUSE + pcm
    st["voiced_at"] = now                          # 次の声は VOICE_GAP 後
    _save(st)
    _log_event("voice", {"line": "＋".join(nm for nm, _ in pcms), "bytes": len(pcm)})
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

<div class=card id=alive>
  <h2 style="margin-top:0">止まっていないか</h2>
  <div class=st id=alivest>…</div>
</div>

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
function alive(){
  fetch('/spirit/health').then(function(r){return r.json()}).then(function(j){
    function ago(s){ if(s===null) return 'まだ来ていない';
      if(s<120) return s+'秒前'; if(s<7200) return Math.round(s/60)+'分前';
      return Math.round(s/3600)+'時間前'; }
    var h='';
    j.items.forEach(function(it){
      h+=(it.ok?'🟢 ':'🔴 ')+it.name+'：最後に来たのは '+ago(it.ago)
        +(it.ok?'':'　<span class=rec>止まっているかも</span>')+'<br>';
    });
    h+='<span style="color:#999;font-size:12px">クラウドが起動してから '+ago(j.boot_ago).replace('前','')+'</span>';
    document.getElementById('alivest').innerHTML=h;
    var c=document.getElementById('alive');
    c.style.background = j.ok ? '#fff' : '#fde2df';
    document.title = (j.ok?'':'🔴 ')+'確かめ用パネル';
  }).catch(function(){
    document.getElementById('alivest').innerHTML='<span class=rec>クラウドに届かない（本番が止まっているかも）</span>';
    document.getElementById('alive').style.background='#fde2df';
  });
}
load(); setInterval(load, 20000);
alive(); setInterval(alive, 20000);
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

# 区画ごとの設定（2026-09-25）。それまでコードに直に書いていたものを、ここに集めた。
# 区画を増やすと、向き・帯・線・問いが区画の数だけ要る。**測らずに使い回すと静かに全部飛ばされる**
# （9/24 実測：テーブル・コンロは全体で測ると違う向きとの差が7倍しか開かないが、上半分なら86倍）。
# `decided` は必須。手で決めた数字は古くなる（9/22 に手で決めた向きの補正が古くなり、
# 9/23〜24 に 20時間50分 の停止を招いた）。いつ・どうやって決めたかを数字と一緒に置く。
ZONE_CFG_DEFAULT = {
    "シンク": {
        "id": "sink", "active": True, "app_zone": "", "pose": "-0.70_-1.00",
        "aim": {"ref": None, "band": None, "min_conf": None},   # None＝いままでの共通の値
        "crop": {"rotate": 180, "hide_from": 0.70},
        "ask": {"scene": _ZONE_SCENE,
                "fixtures": ZONE_FIXTURES["シンク"],
                "empty_q": None},                               # None＝_SINK_EMPTY_Q
        "rule": {"kind": "change"},
        "decided": "2026-09-09 本人。線と帯は 9/22〜23 の実測",
    },
    "水切り": {
        "id": "rack", "active": False, "app_zone": "", "pose": "-1.00_-1.00",
        "aim": {"ref": "spirit/zoneref/rack.jpg", "band": [0.0, 1.0], "min_conf": 0.30},
        "crop": {"rotate": 180, "hide_from": None},
        # 本人の決め（2026-09-25）：「包丁は見えるのが正しい。それ以外の物は元の居場所があるので、
        # 元の場所に戻してほしい。だから物がある判定でよい」。
        # つまり除外するのは**包丁だけ**。菜箸・おたま・フライ返しも「置かれている物」に数える。
        # シンクで「排水口のふたと蛇口は備え付け」としたのと同じ手当てを、包丁にだけ当てる。
        "ask": {"scene": "写真は共有キッチンの食器の水切りかごを天井近くから写したものです。",
                "fixtures": ("かごそのもの（ステンレスの枠・網・受け皿）と、"
                             "壁に取り付けられた木の包丁立てとその包丁は、いつもそこにある物です。数えません。"),
                "empty_q": ("かごや水切りの上に置かれている物（食器・コップ・鍋・ざる・"
                            "菜箸・おたま・フライ返しなどの調理道具）はありますか？ "
                            "包丁立ての包丁は数えないでください。"
                            'JSONだけで答えてください：{"empty": true または false, "items": "あれば短く"}')},
        # シンクと同じ問い（物があるか）を使う。ただし**増えたことは悪くない**。
        # 水切りに食器が増えるのは「誰かが洗った」という良い行いで、
        # シンクと同じ符号で扱うと、洗った人が「散らかした」ことになってしまう。
        # 悪いのは置きっぱなしのほうなので、猶予が切れてから初めて「片づいていない」とする。
        # 猶予＝**日付が変わり、かつそのあと誰かがキッチンを使うまで**（2026-09-25 本人）。
        # 時計ではなく使われ方を基準にするのは、場所が人の生活のリズムで動くという考え方に合わせたもの。
        "rule": {"kind": "grace_next_day"},
        "decided": "2026-09-25 本人。向きは9/24の一覧から本人が指名。線0.30は9/24の実測（同じ向き1.005／違う向き最大0.050）",
    },
}


def _zone_cfg(name: str) -> dict:
    """区画の設定。状態に入っていればそれを使い、無ければ既定値。

    既定値は「コードに直に書いてあった値」そのものなので、
    状態に何も入れなければ**動きは1つも変わらない**。"""
    base = ZONE_CFG_DEFAULT.get(name) or {}
    try:
        saved = ((_load().get("zone_cfg") or {}).get(name)) or {}
    except Exception:
        saved = {}
    out = dict(base)
    for k, v in saved.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            d = dict(out[k]); d.update(v); out[k] = d
        else:
            out[k] = v
    return out


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
# 見本との「確かさ」（位置合わせの峰の高さ）がこれ未満なら、別の景色とみなして比べない（2026-09-22）。
# ずれの数字は、無関係な2枚でもたまたま小さく出る。9/22 16:45 は、壁を写した1枚と
# シンクを写した1枚を「ずれが小さい」と比べて、嘘の「片づいた」を出した。
# 実測（9/22・見本 spirit/aim_ref.jpg）：合っている 0.09〜0.22／ずれ・違う向き 0.00〜0.06。
AIM_MIN_CONF = 0.08
LAST_GOOD_TTL = 7200   # 直前に通った写真を、この秒数だけ「同じ向きの証人」として使う
# 向きを見分けるときは、画面の**下半分**（調理台・コンロ・床）だけで測る（2026-09-23）。
# シンクの中は水・光・映り込みで毎回変わるので、画面全体だと同じ向きでも確かさが暴れる。
# 実測（見本 spirit/aim_ref.jpg・全体→下半分）：
#   合っている 0.004→0.170／0.167→0.168／0.147→0.158／0.126→0.196
#   違う向き   0.070→0.042／0.005→0.042／0.018→0.043／0.012→0.003
# 全体だと合っている側が 0.004 まで落ちて分けられないが、下半分なら 0.158〜0.196 と 0.00〜0.06 で分かれる。
VIEW_BAND = (0.5, 1.0)   # 縦の何割目から何割目までを見るか


def _frame_match(a: bytes, b: bytes, band: tuple = None) -> tuple:
    """2枚の位置ずれ（画素）と、その確かさ（0〜1）。_frame_shift と同じ計算で、確かさも返す。
    band を渡すと、縦のその範囲だけで測る（向きの見分け用）。失敗したら (0, 0, None)。"""
    try:
        import cv2
        import numpy as np
        y0, y1 = (0, 360) if band is None else (round(360 * band[0]), round(360 * band[1]))
        def g(d):
            im = cv2.imdecode(np.frombuffer(d, np.uint8), cv2.IMREAD_GRAYSCALE)
            return np.float32(cv2.resize(im, (640, 360))[y0:y1]) / 255.0
        ga, gb = g(a), g(b)
        win = cv2.createHanningWindow((640, y1 - y0), cv2.CV_32F)
        (dx, dy), resp = cv2.phaseCorrelate(ga, gb, win)
        return round(dx * 2), round(dy * 2), float(resp)
    except Exception as e:
        logger.warning("frame match failed: %s", e)
        return 0, 0, None


_last_good = {"jpg": None, "ref_at": 0.0}   # 「見本そのものと合った」最後の1枚（錨）


_ref_cache = [0.0, None]     # 見本の読み置き（時刻, 中身）
REF_CACHE_GAP = 600.0        # 見本はめったに変わらないので10分に一度でよい


def _aim_now(jpg: bytes) -> tuple:
    """いまの1枚が見本からどれだけずれているか (shift, conf)。測れなければ (None, None)。

    2026-09-24：カメラの向きの補正が古くなって、写真が274pxずれたまま21時間
    「違う景色」で弾かれ続けた。**ずれを記録に残していなかったので、
    いつから何pxずつずれたのかを後から追えなかった。**毎回の判定に残す。
    この数字は D の毎日の見張りが読み、1日の中央値が 100px を超えたら知らせる
    （弾かれる線は150px。越える前に気づくための位置）。"""
    t = time.time()
    if t - _ref_cache[0] > REF_CACHE_GAP:
        _ref_cache[1] = read_object(AIM_REF_OBJ)
        _ref_cache[0] = t
    ref = _ref_cache[1]
    if ref is None:
        return None, None
    dx, dy, resp = _frame_match(ref, jpg, VIEW_BAND)
    return ([dx, dy], round(resp, 3)) if resp is not None else (None, None)


def _view_ok(now: bytes, zone: str = "") -> tuple:
    """今の1枚が見本と同じ景色か。(ok, resp, shift)。見本が無ければ ok（判断できないので止めない）。

    見分けは画面の下半分（VIEW_BAND）だけで測る。シンクの中は毎回変わるため。

    それでも、見本と比べるだけだと**向きは合っているのに中身が大きく変わった**ときに落ちる。
    9/23 11:06、シンクを白いまな板が覆った1枚が確かさ 0.004 で飛ばされた（見本は空のシンク）。
    同じ日の別の1枚とは 0.695 で一致していたので、向きは合っていた。
    そこで、見本に落ちても**「見本と合った最後の1枚」と合えば通す**。

    通した写真をその1枚に格上げはしない（2026-09-23・D の指摘）。
    格上げすると通った写真が次の基準になり、少しずつのずれが積み上がっても誰も気づけない
    （9/22、自動の向き直しがこの形で壁の方へ歩いていった）。錨は見本と合った時だけ打ち直す。"""
    aim = (_zone_cfg(zone).get("aim") or {}) if zone else {}
    ref_obj = aim.get("ref") or AIM_REF_OBJ           # 区画ごとの見本（無ければ共通）
    band = tuple(aim.get("band") or VIEW_BAND)        # 区画ごとの帯
    min_conf = aim.get("min_conf") or AIM_MIN_CONF    # 区画ごとの線
    ref = read_object(ref_obj)
    if ref is None:
        return True, None, None
    now_t = time.time()
    dx, dy, resp = _frame_match(ref, now, band)
    if resp is None:
        return True, None, [dx, dy]
    if resp >= min_conf:
        _last_good["jpg"], _last_good["ref_at"] = now, now_t
        return True, round(resp, 3), [dx, dy]
    prev = _last_good["jpg"]
    if prev is not None and now_t - _last_good["ref_at"] < LAST_GOOD_TTL:
        pdx, pdy, presp = _frame_match(prev, now, band)
        if (presp is not None and presp >= min_conf
                and abs(pdx) <= SHIFT_MAX_PX and abs(pdy) <= SHIFT_MAX_PX):
            return True, round(presp, 3), [pdx, pdy]   # 錨は打ち直さない
    return False, round(resp, 3), [dx, dy]


def _frame_shift(a: bytes, b: bytes) -> tuple:
    """2枚の写真の位置ずれ（画素）を測る。AIを使わない・その場で終わる。

    「シンク全体が写っているか」をAIに聞くと、定位置の写真でも「切れている」と
    答えてしまい門にならなかった（2026-09-09・3通りの聞き方で全部 false）。
    代わりに写真そのものの位置合わせで測る。実測：同じ向き 0px／中身が変わっただけ 20px／
    横0.05ずれ 124px（比べても平気だった）／横0.10ずれ 270px（シンクが切れて誤報した）。"""
    dx, dy, _conf = _frame_match(a, b)       # 確かさも欲しいときは _frame_match を使う
    return dx, dy


async def _compare_zone(before: bytes, after: bytes, name: str, sink=None) -> dict:
    """前後2枚で、その区画の物が 増えた／減った／同じ かを決める（両方向ルール）。

    返す形は _compare_images と同じ（same / better / changes）ので、_zone_pass はそのまま。
    better＝減った（片づいた方向）。判断できないときは skip を付けて返す。

    sink＝(前が空か, 今が空か)。2026-09-10 夜：「空か」の答え（20/20で安定）を軸にする。
      空→空＝同じ（光が違っても比べない）／物あり→空＝片づいた／空→物あり＝散らかった。
      物あり→物あり（と、どちらか不明）のときだけ、くぼみ以外を塗りつぶしてAIに聞く。"""
    # 今の1枚が、見本と同じ景色か（確かさで見る）。違えば比べない（2026-09-22）
    view_ok, resp, vshift = _view_ok(after, name)
    if not view_ok:
        _log_event("aim_mismatch", {"zone": name, "resp": resp, "shift": vshift,
                                    "min_resp": (_zone_cfg(name).get("aim") or {}).get("min_conf")
                                                or AIM_MIN_CONF})
        return {"skip": "view_mismatch", "same": True, "resp": resp, "shift": vshift}
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
    _ask = _zone_cfg(name).get("ask") or {}
    fix = _ask.get("fixtures", ZONE_FIXTURES.get(name, ""))
    q = (_ask.get("scene", _ZONE_SCENE) + fix + "2枚の写真は同じ場所で、1枚目が前、2枚目が今です。"
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
    if not key_ok(x_upload_key):
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
    if not key_ok(x_upload_key):
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
CHECK_GAP = 120.0           # 見回り同士の最短間隔。2026-09-09: 1800→300。2026-09-24: 300→120。
                            # 300 は実測でブレーキになっていた（9/16〜9/24 の見回り418回で、
                            # 間隔が5分未満のものが1回も無く、5〜6分に山がある）。
                            # 続けて人が来た日に、2人目の分を取りこぼす。
                            # 静けさの条件（CHECK_QUIET_SEC）は変えないので、**顔の撮り逃しは増えない**。
                            # 増える見回りは8日間で9回＝1日あたり約1.1回（いまは1日52回）。
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
    # 2026-09-13：成績で区画を止めるのをやめた。成績は記録として残す。
    #
    # 止めていた理由は「誰も来ていないのに変化したと言ったら誤報」だったが、
    # この数え方は人の検知が当てになる場合にしか成り立たない。実測では
    # 9/13 に人が居たかたまり19回のうち、誰か分かったのは2回。ほとんどの
    # 来訪を「誰も居ない」と思っているので、本物の変化まで誤報に数える。
    # 逆に人が居さえすれば、判定が間違っていても当たりに数えられた
    # （9/11 03:10：前後とも空なのに「片づいた」。p04が居たので当たり扱い）。
    # 成績は正しさではなく「人が居たかどうか」を測っていた。
    #
    # そしてシンクは区画が1つしかない。1つを止めると全部止まる。実際に
    # 9/11に見送りとなり、9/13は前後比較が1回も走らず、なつき度も上がらなかった。
    # 止める仕組みは、人の検知が当てになるようになってから戻す。
    return list(st.get("zones", []))


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


@router.post("/zones/reset_score")
async def zones_reset_score(key: str = ""):
    """区画の成績をまっさらにする（見方を作り直したときに使う）。

    9/12 に「空か」の判定を作り直した（くぼみだけ切り出し）。
    9/11 までの誤報は古い作りでのものなので、背負わせ続ける意味がない。"""
    if not key_ok(key):
        raise HTTPException(status_code=401, detail="bad key")
    st = _load()
    for z in st.get("zones", []):
        z["trials"] = z["hits"] = z["false"] = 0
        z["state"] = "試用中"
    _save(st)
    _log_event("zones_reset", {"zones": [z.get("name") for z in st.get("zones", [])]})
    return {"ok": True, "zones": st.get("zones", [])}


@router.post("/sink_check")
async def sink_check(request: Request, key: str = "", model: str = "",
                     rotate: int = 0, bright: float = 1.0):
    """送った写真1枚について「シンクは空か」だけ答える（2026-09-13）。

    記録には何も残さない。見方を直したあと、過去の写真で確かめるための口。"""
    if not key_ok(key):
        raise HTTPException(status_code=401, detail="bad key")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    # 何を「物」と見たのかまで返す。2026-09-13：夜の写真で15回中13回「物あり」と
    # 答えたが、切り出す範囲・向き・塗りつぶしのどれが効いているのか推測しかできず、
    # 3回続けて外した。見えているものを言わせる。
    out = {"empty": None, "answer": "分からない", "items": "", "raw": ""}
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        crop = _sink_crop(data)
        if rotate or bright != 1.0:
            # 向きと明るさを変えて試せるようにする（2026-09-13）。
            # 夜の写真で外すので、暗さが効いているのかどうかを分けたい。
            import io as _io
            from PIL import Image, ImageEnhance
            im = Image.open(_io.BytesIO(crop))
            if rotate:
                im = im.rotate(rotate, expand=True)
            if bright != 1.0:
                im = ImageEnhance.Brightness(im).enhance(bright)
            b = _io.BytesIO(); im.save(b, "JPEG", quality=90); crop = b.getvalue()
        out["px"] = len(crop)
        msg = await client.messages.create(
            model=model or MODEL, max_tokens=200,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(crop).decode()}},
                {"type": "text", "text": _SINK_EMPTY_Q}]}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        out["raw"] = text[:300]
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            j = json.loads(m.group(0))
            v = j.get("empty")
            out["empty"] = v if isinstance(v, bool) else None
            out["items"] = str(j.get("items") or "")[:120]
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    out["answer"] = "空" if out["empty"] is True else ("物あり" if out["empty"] is False else "分からない")
    return out


def _grace_over(st: dict, name: str, now: float, has_stuff: bool, used: bool) -> bool:
    """猶予が切れたか（`grace_next_day` の区画・2026-09-25 本人の指示）。

    水切りは、普段から食器が入っているのが正常な場所である。
    シンクと同じ「物があるか」を聞くが、**増えたことは悪くない**。
    水切りに食器が増えるのは「誰かが洗った」という良い行いで、
    シンクと同じ符号で扱うと、洗った人が「散らかした」ことになる。

    悪いのは置きっぱなしのほう。猶予は時計ではなく**使われ方**で計る：
    **日付が変わり、そのあと誰かがキッチンを使うまでは、そのままでよい。**
    夜に洗って寝て、翌朝までは何も言わない。翌日に誰かが来て、
    それでもまだ入っているなら、そこで初めて「片づいていない」になる。

    `used`＝この見回りの区間に人が居た。誰も来ない日は猶予が続く（本人と決めた形）。"""
    zs = st.setdefault("zone_state", {})
    cur = zs.get(name) or {}
    day = _jst_day(now)                    # 日本時間の日付（既存の数え方に合わせる）
    if not has_stuff:
        if cur:
            zs[name] = {}                      # 空になった＝猶予も消える
        return False
    if not cur.get("since"):
        zs[name] = {"since": now, "day": day}  # 入った日を覚える
        return False
    if day == cur.get("day"):
        return False                           # まだ同じ日
    if not cur.get("used_after"):
        if used:
            cur["used_after"] = now            # 日付が変わったあと、はじめて使われた
            zs[name] = cur
        return False
    return True                                # 日付が変わり、そのあと使われ、まだ入っている


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
            # 記録にも残す（2026-09-25）。サーバーのログにしか出していなかったので、
            # 「区画の記録が0件」のとき、止まっているのか何も変わらなかったのかを
            # 外から見分けられなかった。9/25、まさにそれで半日を費やした。
            logger.warning("zone compare failed (%s): %s", z["name"], r["error"])
            _log_event("zone_error", {"zone": z["name"], "err": str(r["error"])[:160]})
            st["patrol_zone"] = z["name"] + " 比べられず"
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
    if not results:
        # ここまで来て1件も残らなかった＝全部エラーか飛ばし。黙って終わらない
        _log_event("zone_none", {"zones": [z["name"] for z in zones], "quiet": quiet})
    for z, r in results:
        changed = not r.get("same")
        z["trials"] = z.get("trials", 0) + 1
        if quiet:
            if changed:
                z["false"] = z.get("false", 0) + 1
        elif changed:
            z["hits"] = z.get("hits", 0) + 1
        _score_zone(z)
        if not changed:
            # 変化なしも残す（2026-09-25）。これが無いと「区画の記録0件」が
            # 「何も変わらなかった」なのか「一度も見ていない」なのか分からない。
            _log_event("zone_same", {"zone": z["name"], "quiet": quiet,
                                     "rule": r.get("rule", "AI")})
        if changed:
            # 区画の id をそのまま持つ（2026-09-24）。`patrol_zone` は「シンク 比べず（ずれ）」
            # のような表示用の文なので、数える側からは使えない。
            st["zone_last"] = {"name": z["name"], "t": time.time()}
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
# 0〜10の整数。初対面0。片づけ1回で+1（その時居た人ぜんぶ）。
# 使って放置しても下げない（台帳#12：「何もしなかったこと」では動かさない）。
# 2026-09-19：会わない時間でも下げない（貯金）。下がる道は無い。
BOND_MAX = 10
BOND_CARE = 1          # 片づけてくれた → +1
BOND_USE = 0           # 使って、そのままにした → 動かさない
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


def _bond_up(pid: str, why: str, now: float, visit: float = 0.0) -> bool:
    """その人のなつき度を1上げる。その人の一度の滞在で最大1回。0〜10で止める。

    2026-09-13：滞在は人ごとに数える。家に1つの滞在で見ていた頃は、
    誰かしらが通りつづけると滞在が切れず、1日に数回しか上がらなかった。"""
    try:
        key = visit or _cur_visit[0]
        ref = get_db().collection("faces").document(pid)
        doc = ref.get().to_dict() or {}
        if key and float(doc.get("bond_visit") or 0) == key:
            return False                       # この人のこの滞在では、もう上げた
        day = _jst_day(now)
        n_today = int(doc.get("bond_day_n") or 0) if doc.get("bond_day") == day else 0
        if n_today >= BOND_DAILY_MAX:
            _log_event("bond_cap", {"person": pid, "day": day})
            return False                       # 今日はもう3回上がった
        level = max(0, min(BOND_MAX, _bond_now(doc) + BOND_CARE))
        ref.update({"bond": level, "bond_at": now, "bond_visit": key,
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
# 思い出をさがす範囲（2026-09-25・本人決定）。24時間では届かない。
# ここは人が毎日来る場所ではなく、2日空くのはふつう（この日も実際に空いた）。
# 24時間のままだと「このまえ やってくれたよね」が、人出の少なさだけで
# ほとんど起きなくなる。言い方は「このまえ」のまま、日数も回数も言わない。
MEMORY_WINDOW = 7 * 86400.0
# 思い出をさがすとき、いちどに見る記録の上限。記録は1日約1,800件なので、
# 7日ぶん全部は見ない。新しい方から見ていって、最初に見つかった1つを使う。
MEMORY_SCAN = 4000


def _bond_now(doc: dict) -> float:
    """いまのなつき度。会わなくても減らない（貯金）。

    2026-09-19（9/15 本人決定）：会わない7日ごとの −1 をやめた。
    「行かなきゃ」という負い目を作らないため。来ない間も、貯めたぶんはそのまま残る。"""
    try:
        b = int(round(float(doc.get("bond") or 0)))
    except (TypeError, ValueError):
        b = 0
    return max(0, min(BOND_MAX, b))


def _manner(doc: dict, alone: bool) -> str:
    """その人への接し方を、地霊への指示文として返す。

    数は決して渡さない（規則4）。渡すのは態度だけ。
    ほかに人が居るときは、そっけなさを引っ込める（規則2）――
    冷たさを第三者が見た瞬間、それは共有され、陰口と同じ回路に乗る。"""
    if not doc:
        return ("初めて見る顔。誰だったか思い出せない。とぼけて、はぐらかす。"
                "名前を尋ねるようなことも言わない。")
    # 2026-09-09: 5段階（0／1-2／3-5／6-8／9-10）に統一。段階の名前と指示文は BOND_STAGES。
    # 「そっけない」段階は無くした。なつき度は0で止まり、下がる道が無い（9/19〜）ので、
    # 冷たさが罰として働く回路がそもそも生まれない（台帳#12）。alone は将来のために残す。
    return _bond_stage(_bond_now(doc))[1]


_LAST_CHANGE: dict = {}          # 直近の変化の控え（窓ごと）。{window: (いつ調べたか, 結果)}
CHANGE_CACHE = 60.0              # 控えを使い回す長さ（秒）


def _recent_change(window: float) -> dict | None:
    """場所に起きたいちばん新しい変化を1つ。{"what","hours","who","t"}。

    2026-09-25：一言を作り置きするときは知っている人ぜんぶ（13人）を回すので、
    人ごとに探すと同じものを13回探すことになる。探すのは1回にして、
    誰の手柄として言えるかだけを人ごとに決める。"""
    got = _LAST_CHANGE.get(window)
    if got and time.time() - got[0] < CHANGE_CACHE:
        return got[1]
    now = time.time()
    # まず控え（起きた瞬間に書いたもの）。これがあれば、記録をめくらずに済む。
    note = (_load() or {}).get("last_change") or {}
    if note.get("what") and 0 < now - float(note.get("t") or 0) <= window:
        out = {"what": note["what"], "hours": int((now - float(note["t"])) // 3600),
               "who": note.get("who") or [], "t": float(note["t"])}
        _LAST_CHANGE[window] = (now, out)
        return out
    out = None
    try:
        # 60件だけ見ていた頃は、混んだ時間帯だと数分ぶんしか遡れず、
        # さっきの片づけを見落としていた（9/23 12:33 の見本が「なし」になった）。
        # 2026-09-25：300件でも足りていなかった。記録は1日約1,800件（大半は写真と人の出入り）で、
        # 300件では18時間しか遡れない。思い出になる記録は7日で93件しかないので、
        # いちばん新しいものが「新しい方から1,289件目」に沈み、**一度も見つかっていなかった**。
        # 件数で区切るのをやめ、時刻で区切る（同じ `t` の並べ替えなので、索引は足さずに済む）。
        # 控えができる前の出来事のための道。MEMORY_SCAN 件までしか遡らないので、
        # それより古いものは拾えない。控えができた後は、ここまで来ない。
        cutoff = now - window
        docs = get_db().collection("spirit_log").where(
            "t", ">=", cutoff).order_by(
            "t", direction="DESCENDING").limit(MEMORY_SCAN).stream()
        for d in docs:
            e = d.to_dict() or {}
            t = e.get("t") or 0
            if now - t > window:
                break
            what = ""
            if e.get("kind") == "zone" and e.get("better"):
                what = "、".join((c.get("what") or "") for c in (e.get("changes") or []) if c.get("what"))
            elif e.get("kind") == "care" or (e.get("kind") == "visit" and e.get("sink_empty")):
                what = "シンクが きれいに なっていた"
            if not what:
                continue
            out = {"what": what[:60], "hours": int((now - t) // 3600),
                   "who": [w for w in (e.get("who") or []) if w], "t": t}
            break
    except Exception as e:
        logger.warning("recent change lookup failed: %s", e)
        return None                              # 調べ損ねたときは控えを作らない
    _LAST_CHANGE[window] = (time.time(), out)
    return out


def _recent_memory(pid: str, window: float = 0.0, after: float = 0.0) -> dict | None:
    """その人に話せる「このまえの思い出」（2026-09-23・本人「思い出を混ぜたい」）。

    場所に起きた変化を1つ拾って返す。{"what": 変化の文, "hours": 何時間前か,
    "t": いつのことか, "mine": その人の手柄として言ってよいか}。
    mine は「その変化のときに居たのがその人ひとり」のときだけ True。
    2人以上居たときは、実際にやったのが別の人かもしれないので、場所の様子として言う。

    after＝この人にはここまで話した、という印（2026-09-25）。範囲を7日に広げたので、
    印が無いと、同じ出来事を一週間ぶん毎回むし返すことになる。"""
    got = _recent_change(window or MEMORY_WINDOW)
    if not got or got["t"] <= after:
        return None                              # もうこの人に話した思い出は、持ち出さない
    return {"what": got["what"], "hours": got["hours"], "t": got["t"],
            "mine": got["who"] == [pid]}


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


async def _zone_tail(st: dict, now: float, who: list, empty, pz: str, ups: list) -> None:
    """突き合わせのあとの遅い仕事（裏で走らせる）。

    「前」の差し替えは _zone_cycle の中で済んでいるので、ここが途中で止まっても
    次の比較は狂わない。止まったことが外から見えるよう、失敗は記録に残す。"""
    try:
        # 誰も居ない今のうちに、次に来る人向けの一言を文にしておく（声係が音にする）
        if await _prepare_greetings(st, now):
            _save(st)
    except Exception as e:
        _log_event("tail_error", {"step": "greetings", "err": ("%s: %s" % (type(e).__name__, e))[:160]})
    # Notion「地霊の記録」に1行（本人決定 2026-09-10：別アカウントの専用DB）
    try:
        result = ("片づいた" if "片づいた" in pz else "散らかった" if "散らかった" in pz
                  else "比べず" if ("比べず" in pz or not pz) else "同じ")
        what = pz[pz.find("（") + 1:pz.rfind("）")] if "（" in pz else ""
        sink = "空" if empty is True else ("物あり" if empty is False else "未確認")
        title = "%s 見回り｜%s｜シンク%s" % (time.strftime("%m-%d %H:%M", time.gmtime(now + JST)), result, sink)
        await asyncio.to_thread(_notion_patrol, now, title,
                                st.get("patrol_url") or st.get("photo_url") or "",
                                int(st.get("patrol_bytes") or 0),
                                result, sink, who, ups, what)
    except Exception as e:
        logger.warning("notion patrol failed: %s", e)
        _log_event("notion_error", {"text": ("%s: %s" % (type(e).__name__, e))[:120]})


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


SINK_ROTATE = 180        # 切り出したあと、正しい向きに直してから渡す
SINK_HIDE = 0.70         # 正しい向きで見た右側のこの割合から先を塗りつぶす（蛇口と水切りかご）


def _sink_crop(data: bytes) -> bytes:
    """写真からシンクのくぼみだけを切り出し、正しい向きに直し、蛇口を隠す。

    2026-09-13：夜の空のシンクを15回中13回「物あり」と答えていた原因が、
    ここにあった。AIに何が見えるか言わせたところ、名指しでこう返ってきた：
      「蛇口（左側に見える白い蛇口）」「蛇口のハンドル部分」
      「蛇口、ホース、テープ状の物体」「蛇口（備え付け）、排水口のふた」
    質問文には「蛇口も備え付けです」と書いてあるのに効かない。
    さらに写真を回さずに渡していたので、逆さまの蛇口が「スプーン」「スポンジ」
    にも見えていた。

    同じ写真での実測（9/11の空5枚・各3回＝15回）：
      いまのまま              2/15 正解
      回すだけ                7/15
      回す＋水切りかごを塗る  10/15
      **回す＋右の帯を塗る    15/15**
    物がある写真も混ぜた18回でも18/18（コップとスプーン／スプーン1本を言い当てた）。
    モデルを強くする必要はなかった（Haiku 4.5 のまま）。文で断るのではなく、
    見せないのが効く。"""
    try:
        import io
        from PIL import Image, ImageDraw
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        c = im.crop((int(w * SINK_BOX[0]), int(h * SINK_BOX[1]), int(w * SINK_BOX[2]), int(h * SINK_BOX[3])))
        if SINK_ROTATE:
            c = c.rotate(SINK_ROTATE)
        cw, ch = c.size
        ImageDraw.Draw(c).rectangle([int(cw * SINK_HIDE), 0, cw, ch], fill=(120, 120, 120))
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


async def _zone_cycle(st: dict, data: bytes, now: float, pose: str = "") -> dict | None:
    """人が去った直後、または長く静かなときに、前後を突き合わせる。

    「前」は最後に無人と確かめた1枚。「後」はいま届いた1枚。
    突き合わせが済んだら、いまの1枚が次の「前」になる。
    比べたときは、裏で続ける仕事（_zone_tail）に渡す材料を返す。"""
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
            # 次の比較の「前」の答え。この1枚でもう聞いてあればそれを使う（同じ写真に二度聞かない）
            prev = st.get("sink_now") if now - float(st.get("sink_now_at") or 0) <= 60 else None
            st["sink_empty_prev"] = prev if prev is not None else await _sink_empty(data)
            st["visit_people"] = []            # 比べられなかった来訪は数えない
            st["visit_seen"], st["seen_by"] = False, []
            st["visit_face_first"] = st["visit_face_last"] = 0.0
            _save(st)
            _log_event("baseline", {"pose": pose, "sink_empty": st["sink_empty_prev"]})
            return None
        who = st.get("visit_people") or []
        _patrol_ups.clear()
        st["patrol_zone"] = ""
        # 「静か」＝この間に誰も来ていない。顔が取れたかどうかではない。
        # ここを who で見ていたため、顔が取れない日は片づけまで誤報として
        # 数えられ、区画の評判が下がりつづけていた。
        quiet = not st.get("visit_seen")
        if quiet and now - float(ats.get(pose) or 0) < IDLE_CHECK_GAP:
            # 黙って返さない（2026-09-25）。ここは「静かなので今回は点検しない」という
            # 正常な抜け道だが、何も残らないので、外からは「止まっている」と区別できない。
            # 9/25、区画の記録が2日間0件だったのを、半日かけて切り分ける羽目になった。
            _log_event("zone_wait", {"pose": pose, "since": round(now - float(ats.get(pose) or 0)),
                                     "gap": IDLE_CHECK_GAP})
            return None                        # 静かな時は、そう何度も点検しない
        # 滞在の長さ。5分以下しか居なかった人には何も付けない（本人決定 2026-09-09）。
        # 滞在の長さは人ごとに見る（2026-09-13）。家に1つの滞在で見ていた頃は、
        # 誰かが通りつづけると誰の滞在も切れず、7時間が1滞在になっていた。
        stay = _stay_seconds(st)
        _cur_visit[0] = float(st.get("visit_start") or now)
        stays = {pid: _person_stay(st, pid, now) or stay for pid in who}
        short = [pid for pid in who if stays[pid] <= STAY_MIN]
        if short:
            _log_event("visit_short", {"who": short,
                                       "stay": {k: round(stays[k]) for k in short}})
        who = [pid for pid in who if stays[pid] > STAY_MIN]
        # 「今、シンクは空か」。見回りの一言を作るときに聞いた答えを使い回す
        # （同じ写真に二度聞かない）。古ければ聞き直す。
        empty = st.get("sink_now")
        if empty is None or now - float(st.get("sink_now_at") or 0) > 60:
            empty = await _sink_empty(data)
        await _zone_pass(st, base, data, who, quiet, st.get("seen_by") or [],
                         sink=(st.get("sink_empty_prev"), empty))
        # 真ん中の案（本人決定 2026-09-09）：去った後にシンクが空なら、来る前がどうであれ
        # 居た人ぜんぶに +1。自分の分を片づけて帰った人も、他人の分を片づけた人もなつく。
        # 使って散らかしたままは 0（下げない）。一度の滞在で +1 は1回だけ（_bond_up）。
        # 2026-09-24：誰も見分けられなかった滞在も残す。以前は `if who:` の中だけで
        # 書いていたので、分からなかった滞在が1件も残らず、「世話が起きた滞在のうち
        # 誰がやったか分かった割合」の分母が作れなかった（9/10〜15 は滞在26件すべてが
        # 「分かった」に見えるが、分からなかった滞在が記録されていないだけ）。
        # 顔が写っていた長さ（秒）。MIN_PRESENCE(30秒)未満なら「通り過ぎた」と読める。
        fspan = round(float(st.get("visit_face_last") or 0) - float(st.get("visit_face_first") or 0))
        _log_event("visit", {"who": who, "sink_empty": empty,
                             "stay": {k: round(stays[k]) for k in who},
                             "seen": bool(st.get("visit_seen")),
                             "face_span": max(0, fspan),
                             "passed_by": bool(st.get("visit_face_first")) and fspan < MIN_PRESENCE})
        if who:
            if empty:
                st["sink_level"] = 0                      # 空＝一番きれい（Aは戻す合図）
                st["score"] = st["raw_score"] = 0.0
                for pid in who:
                    _bond_up(pid, "sink_empty", now,
                             (st.get("visit_of") or {}).get(pid) or _cur_visit[0])
        # 比べ終えたら、遅い仕事より先に「前」を差し替えて保存する（2026-09-14）。
        # 以前はこれが一言の準備と Notion のあとにあり、そこで止まると
        # 「前」が古いまま残って、同じ変化を何度も数えていた。
        tail = {"who": list(who), "empty": empty,
                "pz": st.get("patrol_zone") or "", "ups": list(_patrol_ups)}
        upload_to(key, data, "image/jpeg")
        ats[pose] = now
        st["baseline_ats"] = ats
        st["baseline_at"], st["baseline_pose"] = now, pose       # 表示用
        # 答えが出なかった（None）ときは、前の答えを持ち越す。Noneで上書きすると
        # 次の比較が「どちらか不明」になり、塗りつぶし比較（AI）に回ってしまう。
        if empty is not None:
            st["sink_empty_prev"] = empty                        # 次の比較の「前」の答え
        st["visit_people"] = []
        st["visit_seen"], st["seen_by"] = False, []
        st["visit_face_first"] = st["visit_face_last"] = 0.0
        _save(st)
        return tail
    except Exception as e:
        logger.warning("zone cycle failed: %s", e)
        # 失敗が表から見えないと、基準の写真が入れ替わらない理由を追えない
        # （2026-09-10 朝、09:19の基準がそのまま残っていた）。記録に残す。
        _log_event("zone_error", {"err": ("%s: %s" % (type(e).__name__, e))[:160]})
        return None


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
    dx, dy, resp = _frame_match(ref, now)
    off = max(abs(dx), abs(dy))
    import hashlib
    other = resp is not None and resp < AIM_MIN_CONF
    return {"ok": True, "dx": dx, "dy": dy,
            "resp": round(resp, 3) if resp is not None else None, "min_resp": AIM_MIN_CONF,
            # 何を読んだのかを添える。2026-09-13：見本といまの写真が
            # 同じ中身（md5一致）なのに54pxと答え、どちらを読み違えているのか
            # 外から分からなかった。
            "ref": {"bytes": len(ref), "md5": hashlib.md5(ref).hexdigest()[:10]},
            "now": {"bytes": len(now), "md5": hashlib.md5(now).hexdigest()[:10]},
            # 確かさが低いと、ずれの数字そのものが当てにならない（9/22）。先にそちらを見る
            "state": "違う景色（比較が止まります）" if other else
                     ("合っている" if off <= AIM_WARN_PX else
                      ("ずれている" if off <= SHIFT_MAX_PX else "ずれすぎ（比較が止まります）")),
            "warn_px": AIM_WARN_PX, "stop_px": SHIFT_MAX_PX}


@router.post("/aim/ref")
async def aim_ref(key: str = ""):
    """いまの画角を「見本」として覚える。据え付けが決まったときに一度押す。"""
    if not key_ok(key):
        raise HTTPException(status_code=401, detail="bad key")
    data = read_object("spirit/latest.jpg")
    if data is None:
        return {"ok": False, "error": "いまの写真がありません"}
    upload_to(AIM_REF_OBJ, data, "image/jpeg")
    _log_event("aim_ref", {"bytes": len(data)})
    return {"ok": True, "bytes": len(data)}


@router.get("/history")
async def history(days: float = 1.0):
    """誰が・どの区画を・何回世話したか（2026-09-24・9/28の関門③の材料）。

    関門の夕方にその場で数えて返す。手作業を挟むと当日に間に合わない。
    `pairs`（人×区画の組の数）だけで「2人以上・2区画以上」が判定できる。
    **確定した相手だけを数える**（保留・不明は people に入れず no_person に出す)。"""
    import collections
    since = time.time() - max(0.04, days) * 86400
    tbl = collections.defaultdict(lambda: collections.Counter())
    cares = no_zone = no_person = 0
    last = 0.0
    try:
        q = get_db().collection("spirit_log").where("t", ">=", since)
        for d in q.order_by("t").limit(20000).stream():
            x = d.to_dict() or {}
            if x.get("kind") != "care":
                continue
            cares += 1
            last = max(last, float(x.get("t") or 0))
            who = x.get("who") or x.get("seen") or []
            zone = (x.get("zone") or "").strip()
            if not zone:
                no_zone += 1
            if not who:
                no_person += 1
                continue
            for pid in who:                      # 1人に決め打ちしない。居た人ぜんぶに数える
                tbl[pid][zone or "(区画なし)"] += 1
    except Exception as e:
        return {"error": str(e)}
    names = {}
    try:
        for pid in tbl:
            v = get_db().collection("faces").document(pid).get().to_dict() or {}
            if v.get("name"):
                names[pid] = v["name"]
    except Exception as e:
        logger.warning("history names failed: %s", e)
    zones = {z for c in tbl.values() for z in c if z != "(区画なし)"}
    pairs = sum(1 for pid in tbl for z in tbl[pid] if z != "(区画なし)")
    return {"people": len(tbl), "zones": len(zones), "pairs": pairs,
            "table": {k: dict(v) for k, v in tbl.items()}, "names": names,
            "cares": cares, "no_zone": no_zone, "no_person": no_person,
            "updated": time.strftime("%m/%d %H:%M", time.localtime(last)) if last else None,
            "since": time.strftime("%m/%d %H:%M", time.localtime(since))}


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
            dx, dy, resp = _frame_match(ref, now)
            aim_now = {"dx": dx, "dy": dy, "off": max(abs(dx), abs(dy)),
                       "resp": round(resp, 3) if resp is not None else None}
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
    if not key_ok(key):
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
    if not key_ok(key):
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
async def get_log(limit: int = 200, before: float = 0):
    """研究データの取り出し口（judge/care/presenceの時系列）。新しい順。

    before を付けると、その時刻より前だけを返す（2026-09-22）。
    1回に返すのは最大1000件なので、混んだ日は1000件で半日しか届かず、
    9/19・9/20・9/21 と三日続けてその日の数字の一部を失った。記録は Firestore に
    残っているので、呼ぶ側が「いちばん古い t」を before に渡して遡れば全部読める。
    同じ項目の不等号と並べ替えだけなので、複合インデックスは要らない。"""
    try:
        q = get_db().collection("spirit_log")
        if before:
            q = q.where("t", "<", float(before))
        docs = q.order_by("t", direction="DESCENDING").limit(min(limit, 1000)).stream()
        return {"events": [d.to_dict() for d in docs]}
    except Exception as e:
        return {"events": [], "error": str(e)}
