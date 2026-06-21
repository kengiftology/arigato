from fastapi import APIRouter
from server.database import get_db
from server.storage import photo_url

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


@router.get("/{zone_id}/timeline")
def zone_timeline(zone_id: str):
    """場所の変遷ビュー：care(手入れ) と use(利用=自動ありがとう) を時系列マージ。"""
    db = get_db()

    zone_doc = db.collection("zones").document(zone_id).get()
    zone_name = zone_doc.to_dict()["name"] if zone_doc.exists else zone_id

    # この場所の手入れ記録
    records = list(db.collection("maintenance")
                     .where("zone_id", "==", zone_id)
                     .stream())

    events = []
    contributors = set()
    care_count = 0
    use_count = 0
    care_times = []

    for doc in records:
        m = {"id": doc.id, **doc.to_dict()}
        care_count += 1
        created = m.get("created_at", "")
        if created:
            care_times.append(created)
        if m.get("person_name"):
            contributors.add(m["person_name"])
        if m.get("helped_by"):
            contributors.add(m["helped_by"])

        # この記録に紐づくありがとう（手動=care内に内包、自動=useイベント）
        thanks_docs = list(db.collection("thanks")
                             .where("maintenance_id", "==", doc.id)
                             .stream())
        user_thanks = []
        for t in thanks_docs:
            td = t.to_dict()
            if td.get("source") == "auto":
                use_count += 1
                user_name = td.get("sender_name", "")
                events.append({
                    "type": "use",
                    "id": t.id,
                    "maintenance_id": doc.id,
                    "user_name": user_name,
                    "created_at": td.get("created_at", ""),
                })
            else:
                user_thanks.append({
                    "sender_name": td.get("sender_name", ""),
                    "message": td.get("message", ""),
                    "created_at": td.get("created_at", ""),
                })

        events.append({
            "type": "care",
            "id": doc.id,
            "person_name": m.get("person_name", ""),
            "helped_by": m.get("helped_by"),
            "before_photo": photo_url(m.get("before_photo")),
            "after_photo": photo_url(m.get("after_photo")),
            "before_suggestion": m.get("before_suggestion"),
            "place_line": m.get("place_line"),
            "status": m.get("status", ""),
            "thanks_count": m.get("thanks_count", 0),
            "thanks": user_thanks,
            "created_at": created,
        })

    # 新しい順
    events.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    care_times.sort()

    return {
        "zone": {"id": zone_id, "name": zone_name},
        "summary": {
            "care_count": care_count,
            "use_count": use_count,
            "contributor_count": len(contributors),
            "first_care_at": care_times[0] if care_times else None,
            "last_care_at": care_times[-1] if care_times else None,
        },
        "events": events,
    }
