# -*- coding: utf-8 -*-
"""プチプチを数える（2026-09-06）。

プチッと聞こえるのは、波形が前の値から急に飛ぶところ。
耳はその段差を「点」として聞く。雑音（ザーッ）とは別のもので、
ノイズ除去では消えない（段差は残り、まわりの声だけが削られる）。

隣り合う標本の差を見て、ふだんの差より極端に大きいところを数える。
"""
import io
import sys
from pathlib import Path

import numpy as np
import soundfile as sf


def score(p: Path):
    y, sr = sf.read(str(p))
    if y.ndim > 1:
        y = y.mean(axis=1)
    d = np.abs(np.diff(y))
    # 無音がまじると「ふだんの差」が0に寄り、何でもかんでも段差に見える。
    # 音が出ているところの差の分布を基準にして、そこから飛び抜けた分だけ数える。
    # 息の音は、もともと1標本ごとに大きく動く。文全体をひとつの基準で測ると
    # 息のところが全部「段差」に見えてしまうので、その場その場の基準で測る。
    w = int(0.03 * sr)
    pad = np.pad(d, (w, w), mode="edge")
    ref = np.array([np.percentile(pad[i:i + 2 * w], 90) for i in range(0, len(d), w)])
    ref = np.repeat(ref, w)[:len(d)] + 1e-12
    jumps = int((d > ref * 6).sum())
    return len(y) / sr, jumps, jumps / (len(y) / sr)


def main():
    rep = io.open(Path(__file__).parent / "clicks_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    say("%-18s %6s %8s %10s" % ("ファイル", "長さ秒", "段差の数", "1秒あたり"))
    tot = 0.0
    n = 0
    for a in sys.argv[1:]:
        for p in sorted(Path(a).glob("*.wav")) if Path(a).is_dir() else [Path(a)]:
            sec, j, rate = score(p)
            say("%-18s %6.2f %8d %10.1f" % (p.stem, sec, j, rate))
            tot += rate
            n += 1
    if n:
        say("")
        say("平均 1秒あたり %.1f 箇所" % (tot / n))
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
