# -*- coding: utf-8 -*-
"""家族の動画から、子どもの声だけを取り出す（2026-09-05）。

家で撮った動画には、子ども以外の声も入っている。
まとめて平均に混ぜると、誰の声か分からないものになる。

やり方:
  1) 全部の動画から音声を取り出して繋げる
  2) 声の区間を拾って、特徴でまとめる
  3) まとまりごとに声の高さを測る
  4) いちばん高いまとまり＝子ども、として取り出す

声の高さで選ぶのは、大人と子どもがそこで確実に分かれるから。
大人はおよそ100〜220Hz、子どもは250Hz以上に出る。
"""
import glob
import io
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
KID = HERE / "kid"
WORK = HERE / "work" / "kid"
PEOPLE = HERE / "people"
SR = 16000
WIN, GAP = 0.03, 0.4
MIN_SEG, MAX_SEG = 1.5, 6.0

rep = io.open(HERE / "kid_result.txt", "w", encoding="utf-8")


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
    WORK.mkdir(parents=True, exist_ok=True)
    vids = sorted(glob.glob(str(KID / "*.mp4")))
    say("動画 %d本 から音声を取り出しています..." % len(vids))
    parts = []
    for i, v in enumerate(vids):
        p = WORK / ("a%02d.wav" % i)
        if not p.exists():
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", v,
                            "-ac", "1", "-ar", str(SR), "-sample_fmt", "s16", str(p)],
                           check=False)
        if p.exists():
            parts.append(read_wav(p))
    x = np.concatenate(parts)
    say("音声の合計 %.1f 分" % (len(x) / SR / 60))

    segs = segments(x)
    say("声の区間 %d本（合計 %.1f 分）" % (len(segs), sum(e - s for s, e in segs) / 60))
    if len(segs) > 500:
        step = len(segs) / 500.0
        segs = [segs[int(i * step)] for i in range(500)]
        say("全体から %d本 を等間隔で選びました" % len(segs))

    import librosa
    say("")
    say("声の高さを測っています...")
    f0s = []
    for s, e in segs:
        seg = x[int(s * SR):int(e * SR)]
        try:
            f = librosa.yin(seg, fmin=70, fmax=500, sr=SR)
            f = f[np.isfinite(f)]
            f0s.append(float(np.median(f)) if len(f) else 0.0)
        except Exception:
            f0s.append(0.0)
    f0s = np.array(f0s)
    say("  全体の中央値 %.0f Hz / 上位10%% は %.0f Hz 以上"
        % (np.median(f0s[f0s > 0]), np.percentile(f0s[f0s > 0], 90)))

    say("")
    say("声の特徴を出しています（GPU）...")
    import torch
    import soundfile as sf
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager
    path, _, _ = ModelManager().download_model(
        "tts_models/multilingual/multi-dataset/xtts_v2")
    cfg = XttsConfig()
    cfg.load_json(str(Path(path) / "config.json"))
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)

    tmp = WORK / "seg.wav"
    embs, keep, kf0 = [], [], []
    for i, (s, e) in enumerate(segs):
        sf.write(str(tmp), x[int(s * SR):int(e * SR)], SR)
        try:
            _g, sp = model.get_conditioning_latents(audio_path=[str(tmp)])
        except Exception:
            continue
        embs.append(sp.cpu().numpy().flatten())
        keep.append((s, e))
        kf0.append(f0s[i])
        if (i + 1) % 100 == 0:
            say("  %d / %d" % (i + 1, len(segs)))
    E = np.stack(embs)
    E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    kf0 = np.array(kf0)

    from sklearn.cluster import AgglomerativeClustering
    d = 1.0 - (E @ E.T)
    thr = float(np.percentile(d[np.triu_indices(len(E), 1)], 25))
    lab = AgglomerativeClustering(n_clusters=None, distance_threshold=thr,
                                  metric="cosine", linkage="average").fit_predict(E)
    say("")
    say("まとまりごとの声の高さ（高いほど子ども）")
    rows = []
    for k in np.unique(lab):
        idx = [i for i, l in enumerate(lab) if l == k]
        if len(idx) < 5:
            continue
        pit = np.median([kf0[i] for i in idx if kf0[i] > 0] or [0])
        rows.append((pit, k, idx))
    rows.sort(reverse=True)
    for pit, k, idx in rows[:8]:
        say("  %3.0f Hz  区間%3d本" % (pit, len(idx)))

    if not rows:
        say("まとまりが作れませんでした")
        return 1
    pit, k, idx = rows[0]
    say("")
    say("いちばん高いまとまり（%.0f Hz）を子どもの声として取り出します" % pit)
    d2 = PEOPLE / "p_kid"
    if d2.exists():
        import shutil
        shutil.rmtree(d2)
    (d2 / "clips").mkdir(parents=True)
    idx.sort(key=lambda i: -(keep[i][1] - keep[i][0]))
    tot = 0.0
    for j, i in enumerate(idx[:14]):
        s, e = keep[i]
        sf.write(str(d2 / "clips" / ("%02d.wav" % j)), x[int(s * SR):int(e * SR)], SR)
        tot += e - s
    import shutil
    shutil.copy2(HERE / "consent_template.md", d2 / "consent.md")
    clips = [str(p) for p in sorted((d2 / "clips").glob("*.wav"))]
    g, sp = model.get_conditioning_latents(audio_path=clips)
    np.save(d2 / "latent.npy", g.cpu().numpy())
    np.save(d2 / "embedding.npy", sp.cpu().numpy())
    say("people/p_kid に %d本 / 合計 %.0f 秒 置きました" % (len(clips), tot))
    say("clips を聴いて、子どもの声だけか確かめてください")
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
