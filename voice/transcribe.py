# -*- coding: utf-8 -*-
"""区間ごとに何を喋っているかを書き出す（2026-09-05）。

声の高さでは、子どもと親（女性）が分けられなかった。どの帯にも両方入る。
そこで内容で見分ける。何を喋っているかが分かれば、耳で全部聴かなくても
一覧を眺めるだけで「これは子ども」と拾える。

出すのは表：番号・時刻・長さ・声の高さ・聞き取れた言葉。
番号を教えてもらえれば、その区間だけを取り出す。
"""
import glob
import io
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
WORK = HERE / "work" / "kid"
OUT = HERE / "work" / "kid_text"
SR = 16000
WIN, GAP = 0.02, 0.25
MIN_SEG, MAX_SEG = 0.4, 4.0


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
    import whisper

    x = np.concatenate([read_wav(p) for p in sorted(glob.glob(str(WORK / "a*.wav")))])
    segs = segments(x)
    print("区間 %d本" % len(segs))

    OUT.mkdir(parents=True, exist_ok=True)
    np.save(OUT / "segs.npy", np.array(segs))

    print("文字起こしの用意...")
    model = whisper.load_model("small", device="cuda")

    tmp = OUT / "s.wav"
    rows = []
    for i, (s, e) in enumerate(segs):
        seg = x[int(s * SR):int(e * SR)]
        try:
            f = librosa.yin(seg, fmin=80, fmax=700, sr=SR)
            f = f[np.isfinite(f)]
            p = float(np.median(f)) if len(f) >= 3 else 0.0
        except Exception:
            p = 0.0
        sf.write(str(tmp), seg, SR)
        try:
            r = model.transcribe(str(tmp), language="ja", fp16=True,
                                 condition_on_previous_text=False)
            t = (r.get("text") or "").strip()
        except Exception:
            t = ""
        rows.append((i, s, e, p, t))
        if (i + 1) % 25 == 0:
            print("  %d / %d" % (i + 1, len(segs)))

    o = io.open(HERE / "kid_text.txt", "w", encoding="utf-8")
    o.write("番号  時刻      長さ  高さ   聞き取れた言葉" + chr(10))
    o.write("-" * 78 + chr(10))
    for i, s, e, p, t in rows:
        o.write("%3d  %5.1f分  %3.1f秒  %3.0fHz  %s%s"
                % (i, s / 60, e - s, p, t[:38], chr(10)))
    o.close()
    print("できました: kid_text.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
