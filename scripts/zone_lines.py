# -*- coding: utf-8 -*-
"""区画の「帯」と「線」を、その場所で測って決める（2026-09-27）。

**他所で決めた値を流用しない。**9/24 にキッチンで決めた線 0.30 が 9/27 まで残り、
**ずれ0pxの正しい写真が確かさ 0.223 で弾かれていた。**測り直して 0.08 にした。

  帯 … 写真のどこを見て「同じ向きか」を測るか（上下の範囲）。
        シンクの中は水と光で毎回変わるので、全体で測ると同じ向きでも確かさが落ちる。
  線 … これ未満なら「違う景色」として使わない、という境目。

    python scripts/zone_lines.py 写真の置き場
      置き場の中身： <区画名>.jpg を人数ぶん（見回りと同じ道順で撮ったもの）

同じ向きどうし・違う向きどうしの確かさを総当たりで出し、
**いちばん差が開く帯**と、**その間を取った線**を提案する。
提案は提案であって、**採るかどうかは人が決める。決めたら `decided` に数字ごと残す。**
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server.routers.spirit as sp

BANDS = [(0.0, 1.0), (0.0, 0.5), (0.5, 1.0), (0.25, 0.75), (0.0, 0.35), (0.65, 1.0)]


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    d = sys.argv[1]
    shots = {os.path.splitext(os.path.basename(p))[0]: open(p, "rb").read()
             for p in sorted(glob.glob(os.path.join(d, "*.jpg")))}
    if len(shots) < 2:
        raise SystemExit("写真が2枚以上要ります（区画ごとに1枚ずつ）: %s" % d)
    print("写真 %d枚: %s\n" % (len(shots), "・".join(shots)))
    for name, own in shots.items():
        best = None
        print("%s" % name)
        for b in BANDS:
            same = sp._frame_match(own, own, b)[2]
            others = [(n2, sp._frame_match(own, s2, b)) for n2, s2 in shots.items() if n2 != name]
            hi = max(r[2] for _, r in others)
            who = [n2 for n2, r in others if r[2] == hi][0]
            print("   帯 %-12s 自分 %.3f ／ ほかの区画 最大 %.3f（%s）" % (str(b), same, hi, who))
            if best is None or hi < best[1]:
                best = (b, hi)
        b, hi = best
        print("   → **いちばん紛れにくいのは 帯 %s**（ほかの区画が最大 %.3f）" % (str(b), hi))
        print("      線は、その場所で撮った**同じ向きの写真どうし**の確かさを見てから決める。")
        print("      目安：ほかの区画の最大 %.3f の 1.5〜3倍（= %.2f〜%.2f）。\n"
              % (hi, hi * 1.5, hi * 3))
    print("**注意：この道具が出すのは「違う区画と紛れないか」だけ**である。")
    print("同じ区画を時刻を変えて何枚か撮り、**同じ向きどうしの確かさがどこまで下がるか**も")
    print("見ること（光・物の増減で下がる）。線は**その下限より下**に置く。")


if __name__ == "__main__":
    main()
