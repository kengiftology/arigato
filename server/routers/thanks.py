import uuid
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Body
from server.database import get_db
from server.push import send_push_to
from google.cloud import firestore

router = APIRouter(prefix="/thanks", tags=["thanks"])
JST = timezone(timedelta(hours=9))

@router.post("/welcome")
def send_welcome_thanks(body: dict = Body(...)):
    """投稿直後にアプリからありがとうを届ける"""
    name = body.get("person_name", "")
    if name:
        send_push_to(name, "この場所を手伝ってくれてありがとう 🌱", "あなたのお手伝いが記録されました")
    return {"ok": True}

@router.post("/{maintenance_id}")
def send_thanks(maintenance_id: str, body: dict = Body(default={})):
    """人からのありがとう"""
    db = get_db()
    ref = db.collection("maintenance").document(maintenance_id)
    doc = ref.get()
    if not doc.exists:
        return {"error": "not found"}

    sender_name = body.get("sender_name", "")
    message     = body.get("message", "")
    data = doc.to_dict()
    _record_thanks(db, maintenance_id, source="user", sender_name=sender_name, message=message)
    ref.update({"thanks_count": firestore.Increment(1)})

    if sender_name:
        title = f"{sender_name}さんより"
        push_body = message if message else "ありがとう 🙏"
    else:
        title = "ありがとう 🙏"
        push_body = message if message else "あなたのお手伝いに感謝が届きました"
    # 実際に手を動かした人へ届ける（誰かのビフォーに応えた場合は helped_by）
    send_push_to(data.get("helped_by") or data["person_name"], title, push_body)
    return {"ok": True}

@router.post("/{maintenance_id}/auto")
def send_auto_thanks(maintenance_id: str):
    """アプリからの自動ありがとう（フィードを見た = 使った）"""
    db = get_db()
    ref = db.collection("maintenance").document(maintenance_id)
    doc = ref.get()
    if not doc.exists:
        return {"ok": False}

    # 直近1時間以内に同じ記録への自動ありがとうがあれば送らない。
    # それより古ければ新たな「利用」として記録する → useイベントが積み重なる。
    now = datetime.now(JST)
    one_hour_ago = (now - timedelta(hours=1)).isoformat()

    # 複合インデックス回避のため maintenance_id の等価フィルタのみで取得し、
    # source / created_at の判定はクライアント側で行う
    existing = (db.collection("thanks")
                  .where("maintenance_id", "==", maintenance_id)
                  .stream())
    auto_times = [
        t.to_dict().get("created_at", "")
        for t in existing
        if t.to_dict().get("source") == "auto"
    ]
    last_auto = max(auto_times) if auto_times else ""

    if not last_auto or last_auto < one_hour_ago:
        _record_thanks(db, maintenance_id, source="auto")
        ref.update({"thanks_count": firestore.Increment(1)})
        data = doc.to_dict()
        send_push_to(data.get("helped_by") or data["person_name"], "ありがとう 🌱", "あなたのお手伝いが使われています")

    return {"ok": True}

def _record_thanks(db, maintenance_id: str, source: str = "user", sender_name: str = "", message: str = ""):
    now = datetime.now(JST).isoformat()
    db.collection("thanks").add({
        "maintenance_id": maintenance_id,
        "source": source,
        "sender_name": sender_name,
        "message": message,
        "created_at": now,
    })
