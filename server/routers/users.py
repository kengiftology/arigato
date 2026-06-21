import uuid
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from server.database import get_db
from server.storage import upload_photo

router = APIRouter(prefix="/users", tags=["users"])
JST = timezone(timedelta(hours=9))


@router.post("/register")
async def register_user(
    last_name: str = Form(...),
    first_name: str = Form(...),
    photo: UploadFile = File(...),
):
    # 顔写真は必須（同姓同名を見分けるため）。失敗時はユーザーを作らず弾く
    if not photo or not photo.filename:
        raise HTTPException(status_code=400, detail="顔写真は必須です")
    try:
        photo_url = upload_photo(await photo.read(), photo.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"顔写真のアップロードに失敗しました: {e}")

    db = get_db()
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


@router.patch("/{user_id}/photo")
async def update_user_photo(
    user_id: str,
    photo: UploadFile = File(...),
):
    db = get_db()
    ref = db.collection("users").document(user_id)
    if not ref.get().exists:
        return {"error": "not found"}
    try:
        photo_url = upload_photo(await photo.read(), photo.filename)
    except Exception as e:
        return {"error": str(e)}
    ref.update({"photo_url": photo_url})
    return {"ok": True, "photo_url": photo_url}
