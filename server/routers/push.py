import uuid
import json
import os
from fastapi import APIRouter
from server.database import get_db

router = APIRouter(prefix="/push", tags=["push"])

@router.get("/vapid-public-key")
def get_vapid_public_key():
    return {"key": os.environ.get("VAPID_PUBLIC_KEY", "")}

@router.post("/subscribe")
def subscribe(person_name: str, subscription: dict):
    db = get_db()
    sub_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO push_subscriptions (id, person_name, subscription) VALUES (?,?,?)",
        (sub_id, person_name, json.dumps(subscription))
    )
    db.commit()
    db.close()
    return {"ok": True}
