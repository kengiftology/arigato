"""機械どうしの合言葉（アップロード鍵）の照合を1か所にまとめる。

2026-09-16：鍵が公開リポジトリに載っていたので入れ替える。
ラズパイや声の係は一度に書き換えられないので、入れ替えの間だけ
新しい鍵（TIMELAPSE_KEY）と古い鍵（TIMELAPSE_KEY_PREV）の両方を通す。
全部の機械を新しい鍵に変えたら、TIMELAPSE_KEY_PREV を空にする。
"""
import hmac
import os

CURRENT = os.environ.get("TIMELAPSE_KEY", "")
PREV = os.environ.get("TIMELAPSE_KEY_PREV", "")


def key_ok(key: str | None) -> bool:
    """鍵が合っているか。鍵が設定されていない（手元の試験）ときは通す。"""
    if not CURRENT:
        return True
    key = key or ""
    return any(k and hmac.compare_digest(key, k) for k in (CURRENT, PREV))
