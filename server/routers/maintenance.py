import uuid
import os
import hmac
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, UploadFile, File, Form, Body, HTTPException
from server.database import get_db
from server.storage import upload_photo, photo_url
from server.push import send_push_to
from server import ai
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
              .stream())
    results = [_fmt(d) for d in docs]
    results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return results

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

    before_bytes = None
    after_bytes  = None
    before_mt = after_mt = "image/jpeg"
    before_url = None
    after_url  = None
    try:
        if before_photo and before_photo.filename:
            before_bytes = await before_photo.read()
            before_mt = before_photo.content_type or "image/jpeg"
            before_url = upload_photo(before_bytes, before_photo.filename)
        if after_photo and after_photo.filename:
            after_bytes = await after_photo.read()
            after_mt = after_photo.content_type or "image/jpeg"
            after_url = upload_photo(after_bytes, after_photo.filename)
    except Exception as e:
        print(f"[warn] photo upload failed: {e}")

    # statusを写真の有無で決定
    # in_progress = ビフォーだけの状態。本人の途中経過でもあり、
    # 誰かが代わりに手伝ってもいい「招待状」でもある（ここ気になる統合）
    if before_url and not after_url:
        status = "in_progress"
    elif after_url and not before_url:
        status = "after_only"
    else:
        status = "completed"

    # 場所の一言をAI生成（写真ベース）。失敗時はNone→フロントが汎用文へフォールバック
    before_suggestion = await ai.place_line(before_bytes, before_mt, "before") if before_url else None
    place_line        = await ai.place_line(after_bytes,  after_mt,  "after")  if after_url  else None

    record_id = str(uuid.uuid4())
    now = datetime.now(JST).isoformat()
    db.collection("maintenance").document(record_id).set({
        "zone_id":           zone_id,
        "zone_name":         zone_name,
        "person_name":       person_name,
        "content":           content,
        "before_photo":      before_url,
        "after_photo":       after_url,
        "before_suggestion": before_suggestion,
        "place_line":        place_line,
        "created_at":        now,
        "thanks_count":      0,
        "status":            status,
    })

    # BEFORE投稿時は本人に（自分でやっても、誰かに任せてもいい）
    if status == "in_progress":
        send_push_to(person_name, "気づいてくれてありがとう 🌱", "きれいになったら、まっさきにお知らせします")

    return {"id": record_id, "status": status}


@router.post("/backfill-ai")
async def backfill_ai(body: dict = Body(default={})):
    """過去レコードに AI 一言（before_suggestion / place_line）を後付けする一回限りの管理用。
    保護：本番 env の ANTHROPIC_API_KEY と一致する token を要求（実行後はこのエンドポイントを削除する）。"""
    token = body.get("token", "")
    expected = os.environ.get("ANTHROPIC_API_KEY", "")
    if not expected or not hmac.compare_digest(str(token), expected):
        raise HTTPException(status_code=403, detail="forbidden")

    db = get_db()
    docs = list(db.collection("maintenance").stream())
    scanned = len(docs)
    filled_before = 0
    filled_after = 0
    for doc in docs:
        m = doc.to_dict()
        upd = {}
        bp = m.get("before_photo")
        if bp and not m.get("before_suggestion"):
            line = await ai.place_line_url(photo_url(bp), "before")
            if line:
                upd["before_suggestion"] = line
        ap = m.get("after_photo")
        if ap and not m.get("place_line"):
            line = await ai.place_line_url(photo_url(ap), "after")
            if line:
                upd["place_line"] = line
        if upd:
            doc.reference.update(upd)
            filled_before += 1 if "before_suggestion" in upd else 0
            filled_after += 1 if "place_line" in upd else 0

    return {"scanned": scanned, "filled_before": filled_before, "filled_after": filled_after}


@router.patch("/{record_id}/complete")
async def complete_maintenance(
    record_id: str,
    after_photo: UploadFile = File(...),
    helper_name: str = Form(""),
):
    db = get_db()
    ref = db.collection("maintenance").document(record_id)
    doc = ref.get()
    if not doc.exists:
        return {"error": "not found"}
    data = doc.to_dict()

    after_bytes = None
    after_mt = "image/jpeg"
    after_url = None
    try:
        after_bytes = await after_photo.read()
        after_mt = after_photo.content_type or "image/jpeg"
        after_url = upload_photo(after_bytes, after_photo.filename)
    except Exception as e:
        print(f"[warn] after photo upload failed: {e}")

    # 整えた姿の一言をAI生成（失敗時はNone→フロントが汎用文へフォールバック）
    place_line = await ai.place_line(after_bytes, after_mt, "after") if after_url else None

    update = {
        "after_photo":  after_url,
        "status":       "completed",
        "completed_at": datetime.now(JST).isoformat(),
    }
    if place_line:
        update["place_line"] = place_line

    # ビフォーを置いた人と別の人が手伝った場合：受け渡しを記録して通知
    # 匿名：誰が手伝ったかは出さない。場所が主語
    poster = data.get("person_name", "")
    if helper_name and helper_name != poster:
        update["helped_by"] = helper_name
        if poster:
            zone_name = data.get("zone_name", "") or "気になっていた場所"
            send_push_to(
                poster,
                f"{zone_name}が手伝われました 🌱",
                "あなたが気にかけた場所が、きれいになりました。ありがとうを届けませんか？",
            )

    ref.update(update)
    return {"ok": True, "status": "completed"}
