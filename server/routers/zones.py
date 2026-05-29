from fastapi import APIRouter
from server.database import get_db

router = APIRouter(prefix="/zones", tags=["zones"])

@router.get("")
def list_zones():
    db = get_db()
    docs = db.collection("zones").stream()
    return [{"id": d.id, "name": d.to_dict()["name"]} for d in docs]

@router.post("")
def create_zone(name: str):
    import uuid
    db = get_db()
    zone_id = str(uuid.uuid4())[:8]
    db.collection("zones").document(zone_id).set({"name": name})
    return {"id": zone_id, "name": name}
