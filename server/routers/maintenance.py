import uuid
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import FileResponse
from pathlib import Path
from server.database import get_db

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

JST = timezone(timedelta(hours=9))
UPLOAD_DIR = Path(__file__).parent.parent.parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

def save_photo(file: UploadFile) -> str:
    ext = Path(file.filename).suffix
    filename = f"{uuid.uuid4()}{ext}"
    path = UPLOAD_DIR / filename
    with open(path, "wb") as f:
        f.write(file.file.read())
    return filename

@router.get("")
def list_maintenance():
    db = get_db()
    rows = db.execute("""
        SELECT m.*, z.name as zone_name,
               COUNT(t.id) as thanks_count
        FROM maintenance m
        JOIN zones z ON m.zone_id = z.id
        LEFT JOIN thanks t ON t.maintenance_id = m.id
        GROUP BY m.id
        ORDER BY m.created_at DESC
        LIMIT 50
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.get("/zone/{zone_id}")
def list_by_zone(zone_id: str):
    db = get_db()
    rows = db.execute("""
        SELECT m.*, z.name as zone_name,
               COUNT(t.id) as thanks_count
        FROM maintenance m
        JOIN zones z ON m.zone_id = z.id
        LEFT JOIN thanks t ON t.maintenance_id = m.id
        WHERE m.zone_id = ?
        GROUP BY m.id
        ORDER BY m.created_at DESC
    """, (zone_id,)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.post("")
async def create_maintenance(
    zone_id: str = Form(...),
    person_name: str = Form(...),
    content: str = Form(""),
    before_photo: UploadFile = File(None),
    after_photo: UploadFile = File(None),
):
    record_id = str(uuid.uuid4())
    now = datetime.now(JST).isoformat()
    before = save_photo(before_photo) if before_photo and before_photo.filename else None
    after = save_photo(after_photo) if after_photo and after_photo.filename else None

    db = get_db()
    db.execute(
        "INSERT INTO maintenance (id, zone_id, person_name, content, before_photo, after_photo, created_at) VALUES (?,?,?,?,?,?,?)",
        (record_id, zone_id, person_name, content, before, after, now)
    )
    db.commit()
    db.close()
    return {"id": record_id}

@router.get("/photo/{filename}")
def get_photo(filename: str):
    path = UPLOAD_DIR / filename
    return FileResponse(path)
