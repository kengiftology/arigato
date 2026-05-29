from fastapi import APIRouter
from server.database import get_db

router = APIRouter(prefix="/zones", tags=["zones"])

@router.get("")
def list_zones():
    db = get_db()
    zones = db.execute("SELECT * FROM zones ORDER BY name").fetchall()
    db.close()
    return [dict(z) for z in zones]

@router.post("")
def create_zone(name: str):
    import uuid
    db = get_db()
    zone_id = str(uuid.uuid4())[:8]
    db.execute("INSERT INTO zones (id, name) VALUES (?, ?)", (zone_id, name))
    db.commit()
    db.close()
    return {"id": zone_id, "name": name}
