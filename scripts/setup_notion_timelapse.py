# 一回限り：タイムラプス記録用のNotionデータベースを作成する
# 使い方:
#   python scripts/setup_notion_timelapse.py <NOTION_TOKEN> <親ページID>
# 親ページIDはNotionでページを開いたときのURL末尾32桁（ハイフンあり/なし可）。
# インテグレーションをそのページに「接続」しておくこと。
# 出力された database_id を NOTION_DATABASE_ID として設定する。

import sys
import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if len(sys.argv) != 3:
    print(__doc__ or "usage: setup_notion_timelapse.py TOKEN PARENT_PAGE_ID")
    sys.exit(1)

token, parent = sys.argv[1], sys.argv[2].replace("-", "")

payload = {
    "parent": {"type": "page_id", "page_id": parent},
    "title": [{"type": "text", "text": {"content": "タイムラプス記録"}}],
    "properties": {
        "名前": {"title": {}},
        "撮影時刻": {"date": {}},
        "ゾーン": {"select": {}},
        "画像": {"files": {}},
        "サイズ": {"number": {"format": "number"}},
    },
}

r = httpx.post(
    "https://api.notion.com/v1/databases",
    json=payload,
    headers={
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    },
    timeout=30,
)
if r.status_code != 200:
    print("作成失敗", r.status_code)
    print(r.text)
    sys.exit(1)

data = r.json()
print("データベース作成成功")
print("NOTION_DATABASE_ID =", data["id"].replace("-", ""))
print("URL =", data.get("url"))
