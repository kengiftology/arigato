import uuid
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Body
from server.database import get_db
from server.push import send_push_to
from google.cloud import firestore

router = APIRouter(prefix="/thanks", tags=["thanks"])
JST = timezone(timedelta(hours=9))

@router.post("/welcome")
def send_welcome_thanks(body: dict = Body(...)):
    """投稿直後にアプリからありがとうを届ける"""
    name = body.get("person_name", "")
    if name:
        send_push_to(name, "この場所を手伝ってくれてありがとう 🌱", "あなたのお手伝いが記録されました")
    return {"ok": True}

@router.post("/{maintenance_id}")
def send_thanks(maintenance_id: str, body: dict = Body(default={})):
    """人からのありがとう"""
    db = get_db()
    ref = db.collection("maintenance").document(maintenance_id)
    doc = ref.get()
    if not doc.exists:
        return {"error": "not found"}

    sender_name = body.get("sender_name", "")
    message     = body.get("message", "")
    data = doc.to_dict()
    _record_thanks(db, maintenance_id, source="user", sender_name=sender_name, message=message)
    ref.update({"thanks_count": firestore.Increment(1)})

    # 匿名：通知は「環境（場所）からのありがとう」として届く。送り主名は出さない
    zone_name = data.get("zone_name", "") or "この場所"
    title = f"{zone_name}からありがとう 🌱"
    push_body = message if message else "あなたの手入れに、ありがとうが届きました"
    # 実際に手を動かした人へ届ける（誰かのビフォーに応えた場合は helped_by）
    send_push_to(data.get("helped_by") or data["person_name"], title, push_body)
    return {"ok": True}

@router.post("/{maintenance_id}/auto")
def send_auto_thanks(maintenance_id: str, user_name: str = ""):
    """アプリからの自動ありがとう（フィードを見た = 使った）。
    user_name があれば「誰が使ったか」を記録する。"""
    db = get_db()
    ref = db.collection("maintenance").document(maintenance_id)
    doc = ref.get()
    if not doc.exists:
        return {"ok": False}

    data = doc.to_dict()
    doer = data.get("helped_by") or data.get("person_name", "")

    # 自分の手入れを自分が見た場合は「利用」に数えない
    if user_name and user_name == doer:
        return {"ok": True, "skipped": "self"}

    # 直近1時間以内に「同じ人」が同じ記録を使っていれば二重に数えない。
    # 別の人 or 1時間以上経過なら新たな「利用」として記録 → useが積み重なる。
    now = datetime.now(JST)
    one_hour_ago = (now - timedelta(hours=1)).isoformat()

    # 複合インデックス回避のため maintenance_id の等価フィルタのみで取得し、
    # source / sender_name / created_at の判定はクライアント側で行う
    existing = (db.collection("thanks")
                  .where("maintenance_id", "==", maintenance_id)
                  .stream())
    same_user_times = [
        t.to_dict().get("created_at", "")
        for t in existing
        if t.to_dict().get("source") == "auto"
        and t.to_dict().get("sender_name", "") == user_name
    ]
    last_use = max(same_user_times) if same_user_times else ""

    if not last_use or last_use < one_hour_ago:
        _record_thanks(db, maintenance_id, source="auto", sender_name=user_name)
        ref.update({"thanks_count": firestore.Increment(1)})
        # 匿名：環境（場所）からのありがとうとして届く。使った人の名前は出さない
        zone_name = data.get("zone_name", "") or "この場所"
        send_push_to(doer, f"{zone_name}からありがとう 🌱", "あなたの手入れが、誰かに使われました")

    return {"ok": True}

def _record_thanks(db, maintenance_id: str, source: str = "user", sender_name: str = "", message: str = ""):
    now = datetime.now(JST).isoformat()
    db.collection("thanks").add({
        "maintenance_id": maintenance_id,
        "source": source,
        "sender_name": sender_name,
        "message": message,
        "created_at": now,
    })
