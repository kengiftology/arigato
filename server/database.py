from google.cloud import firestore as _fs
import os

_db = None

def get_db() -> _fs.Client:
    global _db
    if _db is None:
        _db = _fs.Client()
    return _db

DEFAULT_ZONES = [
    {"id": "kitchen",  "name": "キッチン"},
    {"id": "bathroom", "name": "バスルーム"},
    {"id": "common",   "name": "共有スペース"},
    {"id": "entrance", "name": "入り口"},
    {"id": "trash",    "name": "ゴミ捨て場"},
]

def init_db():
    """Firestoreにデフォルトゾーンを初期化。DBが未作成でも起動を止めない。"""
    try:
        db = get_db()
        zones_col = db.collection("zones")
        for z in DEFAULT_ZONES:
            doc = zones_col.document(z["id"]).get()
            if not doc.exists:
                zones_col.document(z["id"]).set({"name": z["name"]})
        print("init_db: zones initialized")
    except Exception as e:
        # Firestoreが未作成の場合でもサーバー起動を続行（ゾーンは後で初期化）
        print(f"init_db warning: {e}. Server will start anyway.")
        print("Create Firestore DB at: https://console.cloud.google.com/datastore/setup?project=farm-locker-490913")
