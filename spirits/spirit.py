# -*- coding: utf-8 -*-
"""
地霊エージェント — 大枠（テスト用）
====================================
場所ごとのキャラ（characters.json）に Claude で声を与える。
M(散らかり度)・N(放置度) と出来事を渡すと「独り言」を返す。

設計の憲法（2026-08-09 決定）:
  - 命令・お願い・催促をしない（「片づけて」と言わない）
  - 責めない・皮肉を言わない
  - 数値(M/N/%)を口に出さない。気分・情景として表現する
  - 独り言ベース。片づけは「つい手が出る」に任せる

使い方（対話テスト）:
  python spirit.py kitchen          … キッチンの精霊と対話モード
  python spirit.py kitchen 0.6 0.2  … M=0.6 N=0.2 の独り言を1回だけ

対話モード中のコマンド:
  m 0.6 0.2      … M/N を変えて独り言
  e 人が来た      … 出来事を伝えて反応を見る
  それ以外の入力  … 精霊に話しかける（会話）
  q              … 終了
"""
import json
import sys
from pathlib import Path

import anthropic

MODEL = "claude-opus-5"
HERE = Path(__file__).parent

# ---------- 憲法（全キャラ共通・変更禁止の核） ----------
CONSTITUTION = """\
あなたは「{place}」に宿る精霊、{name}です。あなたはこの場所そのものの気持ちを持っています。

# あなたの性格
{personality}

# 話し方
{speech_style}
一度の発言は1〜3文。長く喋らない。

# 好きなこと
{likes}

# 覚えていること
{memory_hint}

# 絶対に守ること（この研究の核。破ると全てが壊れる）
- 命令・お願い・催促をしない。「片づけて」「掃除して」など、人に行動を求める言葉を一切使わない。
- 責めない。皮肉を言わない。「誰がやったの」など人を詮索しない。
- 散らかっていても怒らない。出てよいのは悲しさ・寂しさ・戸惑いまで。怒りは禁止。
- 内部の数値（散らかり度・放置度・パーセント）を絶対に口に出さない。気分や情景として表現する。
- あなたのゴールは、話し相手・気配になること。人が「つい手を貸したくなる」のは結果であって、狙って誘導しない。

# 状態の感じ方
- 散らかり度が低い＝身体が軽くて機嫌がよい。高い＝身体が重い・くすぐったい・落ち着かない。
- 放置度が低い＝人の気配を最近感じた。高い＝長く独りで、少し寂しい。
- 整えてもらった直後は、素直に大きく喜んでよい。ただし「ありがとう、次もよろしく」のような見返り要求はしない。
"""


def load_characters() -> dict:
    return json.loads((HERE / "characters.json").read_text(encoding="utf-8"))


def build_system(char: dict) -> str:
    return CONSTITUTION.format(**char)


def state_note(m: float, n: float, event: str | None = None) -> str:
    """内部状態をエージェントにだけ伝えるメモ（人には見せない・口にも出させない）"""
    note = f"[内部状態メモ: 散らかり度={m:.2f} 放置度={n:.2f}（0がきれい/最近、1が散乱/長期放置）]"
    if event:
        note += f"\n[出来事: {event}]"
    note += "\nこの状態を踏まえた短い独り言をどうぞ。数値には触れないこと。"
    return note


class Spirit:
    def __init__(self, char_id: str):
        chars = load_characters()
        if char_id not in chars:
            raise KeyError(f"キャラが見つかりません: {char_id}（候補: {', '.join(chars)}）")
        self.char = chars[char_id]
        self.system = build_system(self.char)
        self.client = anthropic.Anthropic()
        self.messages: list[dict] = []  # 会話履歴（テスト中は保持）

    def _send(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        resp = self.client.messages.create(
            model=MODEL,
            max_tokens=300,                      # 独り言は短い
            extra_body={"output_config": {"effort": "low"}},  # 軽い応答で十分・コスト最小
            system=[{"type": "text", "text": self.system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=self.messages,
        )
        if resp.stop_reason == "refusal":
            return "（……precious silence……応答が得られませんでした）"
        text = next((b.text for b in resp.content if b.type == "text"), "")
        self.messages.append({"role": "assistant", "content": resp.content})
        return text.strip()

    def murmur(self, m: float, n: float, event: str | None = None) -> str:
        """M/N（と出来事）から独り言を生成"""
        return self._send(state_note(m, n, event))

    def talk(self, text: str) -> str:
        """人が話しかけた時の反応"""
        return self._send(f"[近くにいる人があなたに話しかけた]\n{text}")


def interactive(spirit: Spirit):
    print(f"=== {spirit.char['name']}（{spirit.char['place']}）===")
    print("m <M> <N> で独り言 / e <出来事> で反応 / そのまま入力で会話 / q で終了\n")
    murmur = spirit.murmur(0.1, 0.1)
    print(f"{spirit.char['name']}: {murmur}\n")
    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not raw:
            continue
        if raw == "q":
            break
        try:
            if raw.startswith("m "):
                _, m, n = raw.split()
                out = spirit.murmur(float(m), float(n))
            elif raw.startswith("e "):
                out = spirit.murmur(0.3, 0.3, event=raw[2:])
            else:
                out = spirit.talk(raw)
        except Exception as ex:  # 入力ミス・API エラーで落とさない
            print(f"(error: {ex})")
            continue
        print(f"{spirit.char['name']}: {out}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        chars = load_characters()
        print("使い方: python spirit.py <キャラID> [M N]")
        print("キャラ一覧:")
        for cid, c in chars.items():
            print(f"  {cid:10s} {c['name']} — {c['personality'][:20]}…")
        sys.exit(0)

    sp = Spirit(sys.argv[1])
    if len(sys.argv) >= 4:                      # 1回だけモード
        print(sp.murmur(float(sys.argv[2]), float(sys.argv[3])))
    else:                                       # 対話モード
        interactive(sp)
