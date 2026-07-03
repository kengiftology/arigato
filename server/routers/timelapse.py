import os
import re
import logging
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import APIRouter, Request, Header, HTTPException, BackgroundTasks

from server.storage import upload_to

router = APIRouter(prefix="/timelapse", tags=["timelapse"])
JST = timezone(timedelta(hours=9))
logger = logging.getLogger("timelapse")

# Cloud Run の環境変数 TIMELAPSE_KEY と一致したときだけ受け付ける。
# 未設定なら認証なし（ローカル検証用）。
UPLOAD_KEY = os.environ.get("TIMELAPSE_KEY", "")

# Notion 連携（両方設定されているときだけ動く）
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
NOTION_API = "https://api.notion.com/v1/pages"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def add_notion_row(zone: str, when: datetime, image_url: str, size_bytes: int):
    """Notionデータベースに1行追加する（撮影時刻・ゾーン・画像）。
    失敗してもアップロード自体には影響させない。"""
    if not (NOTION_TOKEN and NOTION_DATABASE_ID):
        return
    title = when.strftime("%Y-%m-%d %H:%M")
    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "cover": {"type": "external", "external": {"url": image_url}},
        "properties": {
            "名前": {"title": [{"text": {"content": f"{title} {zone}"}}]},
            "撮影時刻": {"date": {"start": when.isoformat()}},
            "ゾーン": {"select": {"name": zone}},
            "画像": {
                "files": [
                    {
                        "type": "external",
                        "name": f"{title}.jpg",
                        "external": {"url": image_url},
                    }
                ]
            },
            "サイズ": {"number": size_bytes},
        },
    }
    try:
        r = httpx.post(NOTION_API, json=payload, headers=NOTION_HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning("Notion行の作成に失敗 %s: %s", r.status_code, r.text[:300])
    except httpx.HTTPError as e:
        logger.warning("Notion接続エラー: %s", e)


@router.post("")
async def upload_frame(
    request: Request,
    background: BackgroundTasks,
    zone: str = "default",
    x_upload_key: str = Header(None),
):
    """ESP32-CAMから生のJPEGを受け取り、GCSの timelapse/ 配下に保存する。
    body = JPEGバイト列そのまま（multipartではない）。
    保存後、バックグラウンドでNotionデータベースにも1行追加する。"""
    if UPLOAD_KEY and x_upload_key != UPLOAD_KEY:
        raise HTTPException(status_code=401, detail="bad key")

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")

    zone = re.sub(r"[^A-Za-z0-9_-]", "", zone) or "default"
    now = datetime.now(JST)
    name = f"timelapse/{zone}/{now:%Y%m%d}/{now:%H%M%S}.jpg"
    url = upload_to(name, data, "image/jpeg")
    background.add_task(add_notion_row, zone, now, url, len(data))
    return {"ok": True, "name": name, "url": url, "bytes": len(data)}
