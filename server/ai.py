"""写真を見て「場所」が一人称で話す一言をAI生成する。

ANTHROPIC_API_KEY が無い／生成に失敗した場合は None を返し、
呼び出し側は固定の汎用文へフォールバックする（壊さない）。
"""
import base64
import os

from anthropic import AsyncAnthropic

_client = None

# 短い flavor text なので thinking は使わない（省略＝オフで低レイテンシ）。
# モデルは Opus 4.8。レイテンシ/コストが気になれば claude-haiku-4-5 等に変更可。
MODEL = "claude-opus-4-8"

_ALLOWED_MEDIA = {"image/jpeg", "image/png", "image/gif", "image/webp"}

_SYSTEM = (
    "あなたは共有スペースの中の『ある場所』そのものです。"
    "渡される写真は、あなた自身の“いまの姿”です。"
    "そこに来た人に、一人称で、短く、やわらかく話しかけてください。"
    "出力は日本語の一文だけ（最大40文字程度）。"
    "鉤括弧・絵文字・前置き・説明は付けない。"
    "毎回ちがう言い回しで、写真に実際に写っているものに具体的に触れること。"
)

_PROMPT = {
    # 課題（before）：散らかった現状を見て、責めずにそっと手を貸してほしいと願う一言
    "before": (
        "いまのわたし（この場所）は、少し散らかっているみたい。"
        "写っているものを見て、『どこをどうしてくれたら気持ちよくなるか』を、"
        "誰も責めずに、そっとお願いする一言にして。"
    ),
    # 整え（after）：誰かが整えてくれた変化を見て、心地よさ・うれしさを表す一言
    "after": (
        "誰かがわたし（この場所）を整えてくれた。"
        "写っている“いまの状態”を見て、その心地よさやうれしさを表す一言にして。"
    ),
}


def _get_client():
    global _client
    if _client is None:
        _client = AsyncAnthropic()
    return _client


async def place_line(image_bytes: bytes, media_type: str, kind: str) -> str | None:
    """写真から場所の一言を生成。失敗時は None（呼び出し側が汎用文へフォールバック）。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    if not image_bytes:
        return None
    mt = media_type if media_type in _ALLOWED_MEDIA else "image/jpeg"
    prompt = _PROMPT.get(kind)
    if not prompt:
        return None
    try:
        client = _get_client()
        b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        msg = await client.messages.create(
            model=MODEL,
            max_tokens=120,
            system=_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": mt, "data": b64,
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        for block in msg.content:
            if block.type == "text":
                line = block.text.strip()
                return line or None
        return None
    except Exception as e:
        print(f"[warn] place_line generation failed: {e}")
        return None


async def place_line_url(image_url: str, kind: str) -> str | None:
    """公開URLの写真から場所の一言を生成（過去レコードのバックフィル用）。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    if not image_url:
        return None
    prompt = _PROMPT.get(kind)
    if not prompt:
        return None
    try:
        client = _get_client()
        msg = await client.messages.create(
            model=MODEL,
            max_tokens=120,
            system=_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "url", "url": image_url}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        for block in msg.content:
            if block.type == "text":
                line = block.text.strip()
                return line or None
        return None
    except Exception as e:
        print(f"[warn] place_line_url generation failed: {e}")
        return None
