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
    send_push_to(data["person_name"], title, push_body)
    return {"ok": True}

@router.post("/{maintenance_id}/auto")
def send_auto_thanks(maintenance_id: str):
    """アプリからの自動ありがとう（フィードを見た = 使った）"""
    db = get_db()
    ref = db.collection("maintenance").document(maintenance_id)
    doc = ref.get()
    if not doc.exists:
        return {"ok": False}

    # 1時間以内に同じ記録への自動ありがとうがあれば送らない
    one_hour_ago = datetime.now(JST).replace(tzinfo=None)
    recent = (db.collection("thanks")
               .where("maintenance_id", "==", maintenance_id)
               .where("source", "==", "auto")
               .limit(1)
               .stream())

    if not any(True for _ in recent):
        _record_thanks(db, maintenance_id, source="auto")
        ref.update({"thanks_count": firestore.Increment(1)})
        data = doc.to_dict()
        send_push_to(data["person_name"], "ありがとう 🌱", "あなたのお手伝いが使われています")

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
