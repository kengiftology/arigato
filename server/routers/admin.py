import os

import httpx
from fastapi import APIRouter, Header, HTTPException
from server.database import get_db
from server.storage import photo_url, upload_photo
from server import ai
from google.cloud import firestore

router = APIRouter(prefix="/admin", tags=["admin"])

# TIMELAPSE_KEY を管理操作の共通キーとして流用（未設定なら認証なし＝ローカル用）
ADMIN_KEY = os.environ.get("TIMELAPSE_KEY", "")


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


@router.post("/fix-photos")
async def fix_photos(x_key: str = Header("")):
    """HEIC等のまま保存された写真をJPEGへ変換し直し、欠けている場所の一言を再生成する。

    upload_photo が変換を内蔵する前に投稿されたレコードの修復用（何度実行しても安全）。
    """
    if ADMIN_KEY and x_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="invalid key")

    db = get_db()
    fixed = []
    async with httpx.AsyncClient(timeout=30) as http:
        for doc in db.collection("maintenance").stream():
            data = doc.to_dict()
            update = {}
            for field, kind, line_field in (
                ("before_photo", "before", "before_suggestion"),
                ("after_photo",  "after",  "place_line"),
            ):
                url = data.get(field) or ""
                if not url.lower().endswith((".heic", ".heif")):
                    continue
                resp = await http.get(url)
                resp.raise_for_status()
                new_url = upload_photo(resp.content, "photo.heic")
                update[field] = new_url
                if not data.get(line_field):
                    line = await ai.place_line_url(new_url, kind)
                    if line:
                        update[line_field] = line
            if update:
                doc.reference.update(update)
                fixed.append({"id": doc.id, **update})
    return {"fixed": len(fixed), "records": fixed}


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
