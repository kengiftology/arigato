# -*- coding: utf-8 -*-
"""高い声のまとまりを、さらに細かく分ける（2026-09-05）。

前の版は声の高さだけで選び、子どもと親（女性）が同じまとまりに入った。
どちらも250〜300Hzに出るので、高さでは分けられない。

そこで高い声だけを取り出し、特徴でもっと細かく割る。
あわせて声の太さ（響きが高い位置に出るか）も測る。
体が小さいほど響きは高い位置に出るので、高さと合わせれば手がかりになる。

最後は耳で決める。候補ごとに聴ける形にして並べる。
"""
import glob
import io
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
WORK = HERE / "work" / "kid"
OUT = HERE / "work" / "kid_cands"
SR = 16000
WIN, GAP = 0.03, 0.4
MIN_SEG, MAX_SEG = 1.5, 6.0

rep = io.open(HERE / "kid2_result.txt", "w", encoding="utf-8")


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
    thr = max(np.percentile(rms, 25) * 3.0, rms.mean() * 0.4)
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
    import torch
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager

    x = np.concatenate([read_wav(p) for p in sorted(glob.glob(str(WORK / "a*.wav")))])
    segs = segments(x)
    say("声の区間 %d本" % len(segs))

    path, _, _ = ModelManager().download_model(
        "tts_models/multilingual/multi-dataset/xtts_v2")
    cfg = XttsConfig()
    cfg.load_json(str(Path(path) / "config.json"))
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)

    tmp = WORK / "s.wav"
    E, keep, f0, cen = [], [], [], []
    for s, e in segs:
        seg = x[int(s * SR):int(e * SR)]
        try:
            f = librosa.yin(seg, fmin=70, fmax=500, sr=SR)
            f = f[np.isfinite(f)]
            p = float(np.median(f)) if len(f) else 0.0
        except Exception:
            p = 0.0
        if p < 200:                       # 低い声（大人の男性など）はここで落とす
            continue
        sf.write(str(tmp), seg, SR)
        try:
            _g, sp = model.get_conditioning_latents(audio_path=[str(tmp)])
        except Exception:
            continue
        E.append(sp.cpu().numpy().flatten())
        keep.append((s, e))
        f0.append(p)
        cen.append(float(np.mean(librosa.feature.spectral_centroid(y=seg, sr=SR))))
    E = np.stack(E)
    E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    f0, cen = np.array(f0), np.array(cen)
    say("高い声の区間 %d本（200Hz以上）" % len(E))

    from sklearn.cluster import AgglomerativeClustering
    lab = AgglomerativeClustering(n_clusters=5, metric="cosine",
                                  linkage="average").fit_predict(E)

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()
    say("")
    say("候補（声が高く・響きも高い方が子ども）")
    rows = []
    for k in np.unique(lab):
        idx = [i for i, l in enumerate(lab) if l == k]
        if len(idx) < 3:
            continue
        rows.append((float(np.median(f0[idx])), float(np.median(cen[idx])), k, idx))
    rows.sort(key=lambda r: -(r[0] + r[1] / 20))
    for rank, (p, c, k, idx) in enumerate(rows):
        name = "cand%d" % (rank + 1)
        idx.sort(key=lambda i: -(keep[i][1] - keep[i][0]))
        pieces = []
        for i in idx[:8]:
            s, e = keep[i]
            pieces.append(x[int(s * SR):int(e * SR)])
            pieces.append(np.zeros(int(0.2 * SR), dtype=np.float32))
        sf.write(str(OUT / (name + ".wav")), np.concatenate(pieces), SR)
        np.save(OUT / (name + "_idx.npy"), np.array(idx))
        say("  %s  高さ %3.0f Hz / 響き %4.0f Hz / 区間 %2d本 / 合計 %.0f 秒"
            % (name, p, c, len(idx), sum(keep[i][1] - keep[i][0] for i in idx)))
    np.save(OUT / "keep.npy", np.array(keep))
    say("")
    say("cand*.wav を聴いて、甥っ子さんだけのものを選んでください")
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
