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
        send_push_to(name, "整備してくれてありがとう 🌱", "あなたの整備が記録されました")
    return {"ok": True}

@router.post("/{maintenance_id}")
def send_thanks(maintenance_id: str):
    """人からのありがとう"""
    db = get_db()
    ref = db.collection("maintenance").document(maintenance_id)
    doc = ref.get()
    if not doc.exists:
        return {"error": "not found"}

    data = doc.to_dict()
    _record_thanks(db, maintenance_id, source="user")
    ref.update({"thanks_count": firestore.Increment(1)})

    send_push_to(data["person_name"], "ありがとうが届きました 🙏", "あなたの整備に感謝が届きました")
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
        send_push_to(data["person_name"], "ありがとう 🌱", "あなたの整備が使われています")

    return {"ok": True}

def _record_thanks(db, maintenance_id: str, source: str = "user"):
    now = datetime.now(JST).isoformat()
    db.collection("thanks").add({
        "maintenance_id": maintenance_id,
        "source": source,
        "created_at": now,
    })
