"""アップロード写真をブラウザ表示可能な形式に正規化する。

iPhoneのHEIC等はブラウザで表示できず、Claude APIも受け付けないため、
Web安全な形式（JPEG/PNG/GIF/WebP）以外はJPEGへ変換する。
"""
import io

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()

# ブラウザ・Claude API双方が扱える形式はそのまま通す
_WEB_SAFE = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG":  ("image/png",  ".png"),
    "GIF":  ("image/gif",  ".gif"),
    "WEBP": ("image/webp", ".webp"),
}


def normalize_photo(data: bytes) -> tuple[bytes, str, str]:
    """(バイト列, media_type, 拡張子) を返す。Web安全な形式はそのまま、それ以外はJPEGへ変換。

    画像として読めない場合は元のまま返す（アップロード自体は壊さない）。
    """
    try:
        img = Image.open(io.BytesIO(data))
        fmt = (img.format or "").upper()
        if fmt in _WEB_SAFE:
            mt, ext = _WEB_SAFE[fmt]
            return data, mt, ext
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return buf.getvalue(), "image/jpeg", ".jpg"
    except Exception as e:
        print(f"[warn] photo normalize failed: {e}")
        return data, "image/jpeg", ".jpg"
