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
from fastapi.responses import PlainTextResponse, HTMLResponse, Response

from server.database import get_db
from server.storage import upload_to

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

_SYSTEM = (
    "写真はあなたが見ている場所のいまの姿。"
    "『きれいに保たれているか／散らかっているか』を判断します。\n"
    "【最初に確認】写真に人が写っていたら、何も判断せず {\"skip\": true} だけを返す。\n"
    "【scoreの定義】score は散らかり度。0.0=完全にきれい、0.3=少し物がある、"
    "0.6=それなりに散らかっている、1.0=ひどく散らかっている。"
    "きれいなほど0に近い。間違えないこと。\n"
    "【commentの掟】地霊が自分の気持ちをつぶやく独り言だけ。"
    "人に指図・お願い・提案は絶対にしない（『片付けましょう』『〜してね』は禁止）。"
    "『そわそわするなあ』『すっきりして気持ちいいなあ』のように自分の心もちだけ。"
    "責めない・皮肉らない・数字を言わない。\n"
    "【objectsの書き方】写真に写っている物を、本来の置き場から出ているものを中心に挙げる。"
    "各項目は {\"name\":\"もの\", \"where\":\"場所\", \"n\":個数} の形。"
    "nameは日本語の一般名詞（皿・コップ・鍋・箱・袋・布巾など）。"
    "whereは写真内の位置を大まかに（テーブル・シンク・コンロ・床・棚）。"
    "備え付けの設備（冷蔵庫・シンクそのもの・棚そのもの）は挙げない。"
    "多くても10個まで。\n"
    "必ずJSONだけを返す: "
    "{\"score\": 0〜1の小数, \"comment\": \"15字以内の独り言\", \"objects\": [...]} "
    "または {\"skip\": true}"
)

# 検閲: 責める・命令・提案の語（憲法違反）。含んだら穏当な既定文へ。
_BAD = ("汚い", "汚な", "片付", "片づけ", "掃除", "洗っ", "洗い", "戻し", "捨て",
        "しましょう", "ましょう", "ください", "してね", "しよう", "すべき", "たほうがいい",
        "だらしな", "ひどい", "最低", "ダメな人", "使えない", "気持ち悪", "サボ")

FACE_ROTATE = 270        # カメラの取り付け向きの補正（2026-08-31の実測で270度が正しいと判明）
FACE_ENABLED = os.environ.get("FACE_ENABLED", "") == "1"   # 掲示が済むまでは既定でオフ

_state_cache: dict | None = None   # Firestore読み書き削減用（同一インスタンス内）


def _doc():
    return get_db().collection("spirit").document("state")


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
        return {}
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        b64 = base64.standard_b64encode(image_bytes).decode()
        system = (persona or _DEFAULT_PERSONA) + "\n" + _SYSTEM
        msg = await client.messages.create(
            model=MODEL, max_tokens=200, system=system,
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


def _identify(data: bytes):
    """写真から顔を探して匿名IDに結びつける。顔が無ければ None。
    実名は扱わない。初めての顔には新しい匿名IDを発行して「卵」にする。"""
    from server import face
    crop = face.detect_face(data, rotate=FACE_ROTATE)
    if crop is None:
        return None
    vec = face.embed(crop)
    if vec is None:
        return None
    known = _known_faces()
    pid, sim = face.match(vec, known)
    db = get_db()
    if pid is None:                                  # 初めて見る顔 → 匿名IDを発行
        pid = _new_person_id()
        db.collection("faces").document(pid).set(
            {"vecs": [vec], "born": time.time(), "persona": "", "state": "egg"})
        _log_event("arrive", {"person": pid, "state": "new_egg", "sim": round(sim, 3)})
        return {"person": pid, "state": "egg"}
    doc = db.collection("faces").document(pid).get().to_dict() or {}
    vecs = doc.get("vecs", [])
    if len(vecs) < 5:                                # 見るたび少しずつ覚え直す（眼鏡・照明差に強くする）
        vecs.append(vec)
        db.collection("faces").document(pid).update({"vecs": vecs})
    state = "ready" if doc.get("persona") else "egg"
    _log_event("arrive", {"person": pid, "state": state, "sim": round(sim, 3)})
    return {"person": pid, "state": state}


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
                _save(st)
                return {"ok": True, "person": res["person"], "state": res["state"],
                        "judged": False, "why": "person_seen"}
        except Exception as e:
            logger.warning("identify failed: %s", e)

    # 人が去った直後（5分以内）は細かく見る。それ以外は間隔を空けて無駄打ちを避ける
    recent_visit = (now - st.get("last_seen", 0)) < 300
    gap = JUDGE_GAP_AFTER_VISIT if recent_visit else JUDGE_GAP_IDLE
    if now - st["last_judge"] < gap:
        return {"ok": True, "judged": False, "why": "throttled"}
    if now - st["day_start"] > 86400:
        st["day_start"], st["day_calls"] = now, 0
    if st["day_calls"] >= JUDGE_DAILY_CAP:
        return {"ok": True, "judged": False, "why": "daily_cap"}

    try:                                  # 状態ページ用に最新1枚だけ保存（無人時のみの写真・上書き）
        url = upload_to("spirit/latest.jpg", data, "image/jpeg")
        st["photo_url"] = url
        st["photo_at"] = now
    except Exception as e:
        logger.warning("latest photo save failed: %s", e)

    r = await _judge_image(data, st.get("persona", ""))
    st["last_judge"] = now
    st["day_calls"] += 1
    if r.get("skip"):
        _log_event("judge", {"skip": True})
        _save(st)
        return {"ok": True, "judged": False, "why": "person_in_frame"}
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
    _save(st)
    objs = r.get("objects")
    if isinstance(objs, list):
        objs = [o for o in objs if isinstance(o, dict) and o.get("name")][:10]
        st["objects"] = objs
    if sc is not None:
        _log_event("judge", {"raw": sc, "score": round(st["score"], 3), "pose": pose,
                             "N": round(_calc_n(st, now), 3), "comment": st.get("comment", ""),
                             "objects": st.get("objects", [])})
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
async function load(){
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
        return {d.id: (d.to_dict() or {}).get("vecs", []) for d in docs}
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
        if pid is None:                                  # 初めて見る顔 → 匿名IDを発行
            pid = _new_person_id()
            db.collection("faces").document(pid).set(
                {"vecs": [vec], "born": time.time(), "persona": "", "state": "egg"})
            _log_event("arrive", {"person": pid, "state": "new_egg", "sim": round(sim, 3)})
            return {"person": pid, "state": "egg"}
        doc = db.collection("faces").document(pid).get().to_dict() or {}
        vecs = doc.get("vecs", [])
        if len(vecs) < 5:                                # 見るたび少しずつ覚え直す（眼鏡・照明差に強くする）
            vecs.append(vec)
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
    return {"faces": out, "enabled": FACE_ENABLED}



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
        return None


@router.get("/voice.pcm")
async def voice_pcm():
    """C3が取りに来る声。いまの一言を16kHz/16bit/モノラルの生PCMで返す。"""
    st = _load()
    text = st.get("comment", "")
    if _voice_cache["text"] != text or _voice_cache["pcm"] is None:
        pcm = _synth_ja(text)
        if pcm is None:
            raise HTTPException(status_code=503, detail="no voice")
        _voice_cache["pcm"] = pcm
        _voice_cache["text"] = text
    return Response(content=_voice_cache["pcm"], media_type="application/octet-stream")


@router.get("/log")
async def get_log(limit: int = 200):
    """研究データの取り出し口（judge/care/presenceの時系列）。"""
    try:
        docs = get_db().collection("spirit_log").order_by(
            "t", direction="DESCENDING").limit(min(limit, 1000)).stream()
        return {"events": [d.to_dict() for d in docs]}
    except Exception as e:
        return {"events": [], "error": str(e)}
