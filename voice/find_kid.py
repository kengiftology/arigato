# -*- coding: utf-8 -*-
"""確定した4本を手がかりに、同じ声を探す（2026-09-06）。

本人の確認：67・71・72・89 が甥っ子。43・92 は姉。
前回の探索は手がかりに姉が混ざっていたので外れた。今回は綺麗な4本で探す。

前回もう一つ外した理由：長さの近さが効きすぎて、4秒の区間ばかりが上位に来た。
今回は手がかりと長さの近いものに絞ってから並べる。
"""
import io
import re
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
TEXTD = HERE / "work" / "kid_text"
OUT = HERE / "work" / "find"
SEEDS = [67, 71, 72, 89]
SISTER = [43, 92]


def main():
    import glob
    import wave
    import soundfile as sf

    E = np.load(TEXTD / "all_emb.npy")
    segs = np.load(TEXTD / "segs.npy")
    N = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    ref = N[SEEDS].mean(axis=0)
    ref = ref / (np.linalg.norm(ref) + 1e-9)
    sis = N[SISTER].mean(axis=0)
    sis = sis / (np.linalg.norm(sis) + 1e-9)

    # 甥に近く、かつ姉から遠いものを上に。姉との差を引く
    score = (N @ ref) - 0.6 * (N @ sis)

    lines = io.open(HERE / "kid_text.txt", encoding="utf-8").read().splitlines()[2:]
    tbl = {}
    for L in lines:
        m = re.match(r"\s*(\d+)\s", L)
        if m:
            tbl[int(m.group(1))] = L.strip()

    x = []
    for p in sorted(glob.glob(str(HERE / "work" / "kid" / "a*.wav"))):
        with wave.open(p, "rb") as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        x.append(a.astype(np.float32) / 32768.0)
    x = np.concatenate(x)

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    o = io.open(HERE / "find_result.txt", "w", encoding="utf-8")
    o.write("手がかり（甥）: %s / 姉として除外: %s%s"
            % (SEEDS, SISTER, chr(10) * 2))
    order = [int(i) for i in np.argsort(-score)]
    picked = []
    for i in order:
        if i in SEEDS or i in SISTER:
            continue
        d = segs[i][1] - segs[i][0]
        if d < 0.8:                       # 短すぎるものは声の形が取れない
            continue
        picked.append(i)
        if len(picked) >= 10:
            break
    for i in picked:
        s, e = segs[i]
        sf.write(str(OUT / ("seg%03d.wav" % i)), x[int(s * 16000):int(e * 16000)], 16000)
        o.write("seg%03d  近さ%.3f  %s%s" % (i, score[i], tbl.get(i, ""), chr(10)))
    o.write(chr(10) + "確定ぶん 4本 %.1f 秒。候補が当たれば増える"
            % sum(segs[i][1] - segs[i][0] for i in SEEDS) + chr(10))
    o.close()
    print(io.open(HERE / "find_result.txt", encoding="utf-8").read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
