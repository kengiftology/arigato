from google.cloud import storage as gcs
import uuid, os

from server.images import normalize_photo

BUCKET_NAME = os.environ.get("GCS_BUCKET", "arigato-photos")

_client = None

def get_client():
    global _client
    if _client is None:
        _client = gcs.Client()
    return _client

def upload_photo(file_bytes: bytes, filename: str) -> str:
    # HEIC等はここでJPEGに変換される（Web安全な形式はそのまま通る）
    data, content_type, ext = normalize_photo(file_bytes)
    name = f"{uuid.uuid4()}{ext}"
    bucket = get_client().bucket(BUCKET_NAME)
    blob = bucket.blob(name)
    blob.upload_from_string(data, content_type=content_type)
    # bucket is already public via allUsers:objectViewer IAM — no make_public() needed
    return blob.public_url

def upload_to(object_name: str, file_bytes: bytes, content_type: str = "image/jpeg") -> str:
    """object_name（パス込み）をそのまま使って保存する。タイムラプス用。"""
    bucket = get_client().bucket(BUCKET_NAME)
    blob = bucket.blob(object_name)
    blob.upload_from_string(file_bytes, content_type=content_type)
    return blob.public_url

def photo_url(name: str) -> str:
    if not name:
        return None
    if name.startswith("http"):
        return name
    return f"https://storage.googleapis.com/{BUCKET_NAME}/{name}"


def list_prefix(prefix: str) -> list:
    """その置き場にあるものを一覧する（名前・大きさ・作られた時刻）。"""
    out = []
    for b in get_client().bucket(BUCKET_NAME).list_blobs(prefix=prefix):
        out.append({"name": b.name, "size": b.size,
                    "at": b.time_created.timestamp() if b.time_created else None,
                    "url": photo_url(b.name)})
    return out


def delete_prefix(prefix: str) -> int:
    """その置き場を丸ごと消す。消した数を返す。

    人が写る写真は「確かめが済んだら消す」約束で残すものなので、
    消す手段を最初から用意しておく。手で消し忘れるのが一番まずい。"""
    n = 0
    for b in get_client().bucket(BUCKET_NAME).list_blobs(prefix=prefix):
        b.delete()
        n += 1
    return n
