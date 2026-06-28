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
    "あなたは共有スペースのある「場所」です。送られてくる写真は、あなた自身のいまの姿。"
    "そこに来た人へ、メッセージアプリのひとことのように、自然な話し言葉で短く話しかけてください。\n"
    "守ること：\n"
    "・声に出して違和感のない、自然でなめらかな日本語にする。翻訳調や片言は使わない"
    "（×「わたし気持ちいい」→○「なんだか気持ちいいな」）。\n"
    "・1文。長くても35字くらいまで。\n"
    "・写真に写っているものに触れるのは自然な範囲で。無理に詰め込まず、自然さを最優先。\n"
    "・鉤括弧・絵文字・前置き・説明・自分の名前は付けない。本文の一言だけ。\n"
    "・人がしたこと（「誰かが片づけてくれた」など）には触れず、いまの自分の状態や気持ちだけを話す。\n"
    "・毎回ちがう言い回しにする。"
)

_PROMPT = {
    # 課題（before）：散らかった現状を見て、責めずにそっと手を貸してほしいと願う一言
    "before": (
        "これは、少し散らかったいまのあなた（この場所）の姿です。"
        "気になる所を、誰も責めずに「こうしてくれたら嬉しいな」とやわらかくお願いする一言を、"
        "自然な話し言葉で。\n"
        "例：「机の上のケーブル、まとめてくれると気持ちいいな。」"
        "「床のダンボール、どかせたら広く感じそう。」"
    ),
    # 整え（after）：誰かが整えてくれた変化を見て、心地よさ・うれしさを表す一言
    "after": (
        "これは、誰かが整えてくれたいまのあなた（この場所）の姿です。"
        "きれいになったうれしさや心地よさを、自然な話し言葉で一言。\n"
        "例：「机がすっきりして、気持ちいい。」「光が入って、気分がいいな。」"
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
