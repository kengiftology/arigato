import uuid
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, UploadFile, File, Form
from server.database import get_db
from server.storage import upload_photo, photo_url
from google.cloud import firestore

router = APIRouter(prefix="/maintenance", tags=["maintenance"])
JST = timezone(timedelta(hours=9))

def _fmt(doc) -> dict:
    d = {"id": doc.id, **doc.to_dict()}
    d["before_photo"] = photo_url(d.get("before_photo"))
    d["after_photo"]  = photo_url(d.get("after_photo"))
    return d

@router.get("")
def list_maintenance():
    db = get_db()
    docs = (db.collection("maintenance")
              .order_by("created_at", direction=firestore.Query.DESCENDING)
              .limit(50)
              .stream())
    return [_fmt(d) for d in docs]

@router.get("/zone/{zone_id}")
def list_by_zone(zone_id: str):
    db = get_db()
    docs = (db.collection("maintenance")
              .where("zone_id", "==", zone_id)
              .order_by("created_at", direction=firestore.Query.DESCENDING)
              .stream())
    return [_fmt(d) for d in docs]

@router.post("")
async def create_maintenance(
    zone_id: str = Form(...),
    person_name: str = Form(...),
    content: str = Form(""),
    before_photo: UploadFile = File(None),
    after_photo:  UploadFile = File(None),
):
    db = get_db()
    zone_doc = db.collection("zones").document(zone_id).get()
    zone_name = zone_doc.to_dict()["name"] if zone_doc.exists else zone_id

    before_url = None
    after_url  = None
    try:
        if before_photo and before_photo.filename:
            before_url = upload_photo(await before_photo.read(), before_photo.filename)
        if after_photo and after_photo.filename:
            after_url = upload_photo(await after_photo.read(), after_photo.filename)
    except Exception as e:
        print(f"[warn] photo upload failed (record will be saved without photo): {e}")

    record_id = str(uuid.uuid4())
    now = datetime.now(JST).isoformat()
    db.collection("maintenance").document(record_id).set({
        "zone_id":      zone_id,
        "zone_name":    zone_name,
        "person_name":  person_name,
        "content":      content,
        "before_photo": before_url,
        "after_photo":  after_url,
        "created_at":   now,
        "thanks_count": 0,
    })
    return {"id": record_id}
