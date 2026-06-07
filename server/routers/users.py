import uuid
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, UploadFile, File, Form
from server.database import get_db
from server.storage import upload_photo

router = APIRouter(prefix="/users", tags=["users"])
JST = timezone(timedelta(hours=9))


@router.post("/register")
async def register_user(
    last_name: str = Form(...),
    first_name: str = Form(...),
    photo: UploadFile = File(None),
):
    db = get_db()
    photo_url = None
    if photo and photo.filename:
        try:
            photo_url = upload_photo(await photo.read(), photo.filename)
        except Exception as e:
            print(f"[warn] user photo upload failed: {e}")

    user_id = str(uuid.uuid4())
    now = datetime.now(JST).isoformat()
    db.collection("users").document(user_id).set({
        "last_name":  last_name,
        "first_name": first_name,
        "photo_url":  photo_url,
        "created_at": now,
    })
    return {
        "id":         user_id,
        "last_name":  last_name,
        "first_name": first_name,
        "photo_url":  photo_url,
    }


@router.get("")
def list_users():
    db = get_db()
    docs = db.collection("users").order_by("created_at").stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]
