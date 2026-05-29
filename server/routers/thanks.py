import uuid
import json
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Body
from server.database import get_db
from server.push import send_push_to

router = APIRouter(prefix="/thanks", tags=["thanks"])
JST = timezone(timedelta(hours=9))

@router.post("/{maintenance_id}")
def send_thanks(maintenance_id: str):
    """人からのありがとう"""
    db = get_db()
    record = db.execute(
        "SELECT person_name, content, zone_id FROM maintenance WHERE id = ?",
        (maintenance_id,)
    ).fetchone()
    if not record:
        db.close()
        return {"error": "not found"}

    _record_thanks(db, maintenance_id)
    db.close()

    send_push_to(
        record["person_name"],
        f"ありがとうが届きました 🙏",
        f"{record['content']} への感謝です"
    )
    return {"ok": True}

@router.post("/{maintenance_id}/auto")
def send_auto_thanks(maintenance_id: str):
    """アプリからの自動ありがとう（フィードを見た = 使った）"""
    db = get_db()
    record = db.execute(
        "SELECT person_name, content FROM maintenance WHERE id = ?",
        (maintenance_id,)
    ).fetchone()
    if not record:
        db.close()
        return {"ok": False}

    # 同じ記録への自動ありがとうは1時間に1回まで
    recent = db.execute("""
        SELECT id FROM thanks
        WHERE maintenance_id = ? AND source = 'auto'
          AND created_at > datetime('now', '-1 hour')
    """, (maintenance_id,)).fetchone()

    if not recent:
        _record_thanks(db, maintenance_id, source="auto")
        db.close()
        send_push_to(
            record["person_name"],
            "ありがとう 🌱",
            f"あなたの整備が使われています"
        )
    else:
        db.close()

    return {"ok": True}

@router.post("/welcome")
def send_welcome_thanks(body: dict = Body(...)):
    """投稿直後にアプリからありがとうを届ける"""
    person_name = body.get("person_name", "")
    if person_name:
        send_push_to(
            person_name,
            "整備してくれてありがとう 🌱",
            "あなたの整備が記録されました"
        )
    return {"ok": True}

def _record_thanks(db, maintenance_id: str, source: str = "user"):
    thanks_id = str(uuid.uuid4())
    now = datetime.now(JST).isoformat()
    # sourceカラムが存在しない場合を考慮して安全に追記
    try:
        db.execute(
            "INSERT INTO thanks (id, maintenance_id, created_at, source) VALUES (?,?,?,?)",
            (thanks_id, maintenance_id, now, source)
        )
    except Exception:
        db.execute(
            "INSERT INTO thanks (id, maintenance_id, created_at) VALUES (?,?,?)",
            (thanks_id, maintenance_id, now)
        )
    db.commit()
