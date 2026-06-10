from fastapi import APIRouter
from server.database import get_db
from server.storage import photo_url
from google.cloud import firestore

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/stats")
def get_stats():
    db = get_db()
    users_count   = len(list(db.collection("users").stream()))
    records_count = len(list(db.collection("maintenance").stream()))
    thanks_count  = len(list(db.collection("thanks").stream()))
    zones_count   = len(list(db.collection("zones").stream()))
    subs_count    = len(list(db.collection("push_subscriptions").stream()))
    return {
        "users":         users_count,
        "records":       records_count,
        "thanks":        thanks_count,
        "zones":         zones_count,
        "subscriptions": subs_count,
    }


@router.get("/thanks")
def list_thanks():
    db = get_db()
    docs = (db.collection("thanks")
              .order_by("created_at", direction=firestore.Query.DESCENDING)
              .limit(200)
              .stream())
    return [{"id": d.id, **d.to_dict()} for d in docs]


@router.get("/subscriptions")
def list_subscriptions():
    db = get_db()
    docs = db.collection("push_subscriptions").stream()
    result = {}
    for d in docs:
        name = d.to_dict().get("person_name", "")
        result[name] = result.get(name, 0) + 1
    return [{"person_name": k, "count": v} for k, v in sorted(result.items())]


@router.get("/records")
def list_records():
    db = get_db()
    docs = (db.collection("maintenance")
              .order_by("created_at", direction=firestore.Query.DESCENDING)
              .limit(100)
              .stream())
    result = []
    for d in docs:
        r = {"id": d.id, **d.to_dict()}
        r["before_photo"] = photo_url(r.get("before_photo"))
        r["after_photo"]  = photo_url(r.get("after_photo"))
        result.append(r)
    return result
