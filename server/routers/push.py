import os
from fastapi import APIRouter, Body
from server.database import get_db

router = APIRouter(prefix="/push", tags=["push"])

@router.get("/vapid-public-key")
def get_vapid_public_key():
    return {"key": os.environ.get("VAPID_PUBLIC_KEY", "")}

@router.post("/subscribe")
def subscribe(person_name: str, subscription: dict = Body(...)):
    db = get_db()
    db.collection("push_subscriptions").add({
        "person_name":  person_name,
        "subscription": subscription,
    })
    return {"ok": True}
