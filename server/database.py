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
    db = get_db()
    zones_col = db.collection("zones")
    for z in DEFAULT_ZONES:
        doc = zones_col.document(z["id"]).get()
        if not doc.exists:
            zones_col.document(z["id"]).set({"name": z["name"]})
