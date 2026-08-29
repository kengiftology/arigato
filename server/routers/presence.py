import os
import re
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Header, HTTPException

from server.database import get_db

router = APIRouter(prefix="/presence", tags=["presence"])
JST = timezone(timedelta(hours=9))
logger = logging.getLogger("presence")

# タイムラプスと同じキーで認証する（未設定ならローカル検証用に認証なし）
UPLOAD_KEY = os.environ.get("TIMELAPSE_KEY", "")


@router.post("")
async def report_presence(
    zone: str = "default",
    x_upload_key: str = Header(None),
):
    """AtomS3（PIR）から「人の気配を検知した」イベントを受け取り、Firestoreに記録する。"""
    if UPLOAD_KEY and x_upload_key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")

    zone = re.sub(r"[^A-Za-z0-9_-]", "", zone) or "default"
    now = datetime.now(JST)
    db = get_db()
    ref = db.collection("presence").add(
        {
            "zone": zone,
            "at": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
        }
    )
    logger.info("presence recorded zone=%s at=%s", zone, now.isoformat())
    return {"ok": True, "zone": zone, "at": now.isoformat(), "id": ref[1].id}


@router.get("/recent")
async def recent_presence(
    zone: str = None,
    limit: int = 50,
    x_upload_key: str = Header(None),
):
    """直近の気配イベントを返す（確認・分析用）。"""
    if UPLOAD_KEY and x_upload_key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")

    db = get_db()
    docs = db.collection("presence").limit(500).stream()
    events = [{"id": d.id, **d.to_dict()} for d in docs]
    if zone:
        events = [e for e in events if e.get("zone") == zone]
    # 複合インデックス回避のためクライアントソート（既存の作法に合わせる）
    events.sort(key=lambda e: e.get("at", ""), reverse=True)
    return {"events": events[: max(1, min(limit, 500))]}
