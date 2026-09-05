# -*- coding: utf-8 -*-
"""短い声も拾って、高い順に並べる（2026-09-05）。

候補を2つ出したが、どちらも親だった。
原因は 1.5秒より短い声を捨てていたこと。子どもの発話は短く、
家庭の動画では親がずっと喋っているので、長い区間は親ばかりになる。

今回は 0.4秒から拾い、まとめずに声の高さで並べる。
高い方から順に聴けば、どこで子どもに変わるかが耳で分かる。
"""
import glob
import io
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
WORK = HERE / "work" / "kid"
OUT = HERE / "work" / "kid_pitch"
SR = 16000
WIN, GAP = 0.02, 0.25
MIN_SEG, MAX_SEG = 0.4, 4.0

rep = io.open(HERE / "kid3_result.txt", "w", encoding="utf-8")


def say(s=""):
    print(s)
    rep.write(s + chr(10))
    rep.flush()


def read_wav(p):
    with wave.open(str(p), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return a.astype(np.float32) / 32768.0


def segments(x):
    n = int(WIN * SR)
    f = x[: len(x) // n * n].reshape(-1, n)
    rms = np.sqrt((f ** 2).mean(axis=1) + 1e-12)
    thr = max(np.percentile(rms, 30) * 2.2, rms.mean() * 0.25)
    out, start, gap = [], None, 0.0
    for i, v in enumerate(rms > thr):
        t = i * WIN
        if v:
            if start is None:
                start = t
            gap = 0.0
        elif start is not None:
            gap += WIN
            if gap >= GAP:
                end = t - gap
                while end - start > MAX_SEG:
                    out.append((start, start + MAX_SEG))
                    start += MAX_SEG
                if end - start >= MIN_SEG:
                    out.append((start, end))
                start = None
    return out


def main():
    import librosa
    import soundfile as sf

    x = np.concatenate([read_wav(p) for p in sorted(glob.glob(str(WORK / "a*.wav")))])
    segs = segments(x)
    say("短いものも含めた声の区間 %d本（前回は81本）" % len(segs))

    rows = []
    for s, e in segs:
        seg = x[int(s * SR):int(e * SR)]
        try:
            f = librosa.yin(seg, fmin=80, fmax=700, sr=SR)
            f = f[np.isfinite(f)]
            if len(f) < 3:
                continue
            p = float(np.median(f))
        except Exception:
            continue
        rows.append((p, s, e))
    rows.sort(reverse=True)
    ps = np.array([r[0] for r in rows])
    say("声の高さ  最大 %.0f / 上位5%% %.0f / 中央 %.0f / 最小 %.0f Hz"
        % (ps.max(), np.percentile(ps, 95), np.median(ps), ps.min()))

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()
    say("")
    say("高い順に、帯ごとにまとめました")
    bands = [(0, 12), (12, 25), (25, 40), (40, 60)]
    for bi, (a, b) in enumerate(bands):
        chunk = rows[a:b]
        if not chunk:
            continue
        pieces = []
        for p, s, e in chunk:
            pieces.append(x[int(s * SR):int(e * SR)])
            pieces.append(np.zeros(int(0.25 * SR), dtype=np.float32))
        name = "band%d" % (bi + 1)
        sf.write(str(OUT / (name + ".wav")), np.concatenate(pieces), SR)
        np.save(OUT / (name + "_seg.npy"), np.array([[s, e] for _p, s, e in chunk]))
        say("  %s  %3.0f〜%3.0f Hz / %d本 / 合計 %.0f 秒"
            % (name, chunk[-1][0], chunk[0][0], len(chunk),
               sum(e - s for _p, s, e in chunk)))
    say("")
    say("band1 から順に聴いて、どこから甥っ子さんか教えてください")
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
