# -*- coding: utf-8 -*-
"""人ごとの声の特徴を平均して、地霊の声を作る。

people/ の下にあるフォルダを全部読んで平均するだけ。
だから「抜けたい」と言われたら、そのフォルダを消してこれを走らせ直せば済む。
混ぜて学習させる方式だと、1人抜けるたびに最初からやり直しになる。

使い方:
    python blend.py            … いまある人たちで作り直す
    python blend.py --list     … いま誰から作られているかを見る
"""
import argparse
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
SPIRIT = HERE / "spirit"


def load_people() -> dict:
    """人ごとの声の特徴を読む。embedding.npy があるフォルダだけを見る。"""
    out = {}
    if not PEOPLE.exists():
        return out
    for d in sorted(PEOPLE.iterdir()):
        f = d / "embedding.npy"
        if d.is_dir() and f.exists():
            out[d.name] = np.load(f)
    return out


def blend(vecs: dict) -> np.ndarray:
    """全員の平均。誰の声でもない声になる。

    単純な平均で足りる。重みを付けると「誰の声が濃いか」が生まれ、
    それは場所の声ではなく特定の人の声に寄るということなので、付けない。"""
    arr = np.stack(list(vecs.values()))
    return arr.mean(axis=0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="いま誰から作られているか")
    a = ap.parse_args()

    people = load_people()
    if a.list:
        made = SPIRIT / "made_from.txt"
        if made.exists():
            print(made.read_text(encoding="utf-8"))
        else:
            print("まだ地霊の声は作られていません")
        print("いま声を預けている人: " + (", ".join(people) if people else "なし"))
        return 0

    if not people:
        print("people/ に誰もいません。先に1人ぶんの声を用意してください", file=sys.stderr)
        return 1
    if len(people) < 3:
        # 2人だと片方に寄って「その人の声」に聞こえてしまう。
        print("※ いま %d人。3人以上でないと、誰かの声に寄って聞こえます"
              % len(people), file=sys.stderr)

    SPIRIT.mkdir(exist_ok=True)
    v = blend(people)
    np.save(SPIRIT / "embedding.npy", v)
    (SPIRIT / "made_from.txt").write_text(
        "地霊の声は、いま次の%d人から作られています:\n" % len(people)
        + "\n".join("  - " + k for k in people)
        + "\n\n抜けたい人がいたら people/その人 のフォルダを消して、"
          "blend.py を走らせ直してください。\n",
        encoding="utf-8")
    print("%d人ぶんを平均しました → %s" % (len(people), SPIRIT / "embedding.npy"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
