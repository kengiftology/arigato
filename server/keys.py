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
    """鍵が合っているか。鍵が設定されていない（手元の試験）ときは通す。

    2026-09-27：バイト列にしてから比べる。`hmac.compare_digest` は
    **ASCII でない文字列を渡すと例外を投げる**ので、日本語混じりの鍵を
    送られただけで「合っていない（401）」ではなく「壊れた（500）」になっていた。
    どちらも通さない点は同じだが、**壊れた側に見えると原因を追う先を間違える。**
    バイト列どうしなら、かかる時間は中身によらない（横から覗けない）性質も保たれる。"""
    if not CURRENT:
        return True
    k0 = (key or "").encode("utf-8", "surrogatepass")
    return any(k and hmac.compare_digest(k0, k.encode("utf-8", "surrogatepass"))
               for k in (CURRENT, PREV))
