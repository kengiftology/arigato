# -*- coding: utf-8 -*-
"""録音を1本もらって、地霊の声に加える。

  python add_voice.py <音声ファイル> --name p_nephew

やること:
  1) 16kHzモノラルに直す
  2) 声が出ている区間を拾い、短すぎるものを捨てる
  3) people/<名前>/clips/ に置き、同意の用紙を添える
  4) その人の特徴を出して保存する

このあと make_voice.py を走らせれば、その人を含めた平均になる。
抜けたくなったら people/<名前> を消して走らせ直すだけ。
"""
import argparse
import io
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
WORK = HERE / "work"
SR = 16000
MIN_SEG, MAX_SEG, GAP, WIN = 2.0, 8.0, 0.4, 0.03


def read_wav(p: Path) -> np.ndarray:
    with wave.open(str(p), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return a.astype(np.float32) / 32768.0


def segments(x: np.ndarray) -> list:
    n = int(WIN * SR)
    f = x[: len(x) // n * n].reshape(-1, n)
    rms = np.sqrt((f ** 2).mean(axis=1) + 1e-12)
    thr = max(np.percentile(rms, 20) * 3.0, rms.mean() * 0.35)
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--name", required=True, help="people/ の下に作る名前（本名は使わない）")
    ap.add_argument("--clips", type=int, default=12)
    a = ap.parse_args()

    WORK.mkdir(exist_ok=True)
    wav = WORK / "add.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", a.src,
                    "-ac", "1", "-ar", str(SR), "-sample_fmt", "s16", str(wav)],
                   check=True)
    x = read_wav(wav)
    segs = segments(x)
    total = sum(e - s for s, e in segs)
    print("長さ %.1f 秒 / 声の区間 %d本（合計 %.1f 秒）"
          % (len(x) / SR, len(segs), total))
    if total < 20:
        print("声が20秒に満たないので、特徴が安定しません。もう少し長く録ってください")
        return 1

    d = PEOPLE / a.name
    if d.exists():
        shutil.rmtree(d)
    (d / "clips").mkdir(parents=True)
    import soundfile as sf
    segs.sort(key=lambda p: -(p[1] - p[0]))
    for j, (s, e) in enumerate(segs[: a.clips]):
        sf.write(str(d / "clips" / ("%02d.wav" % j)), x[int(s * SR):int(e * SR)], SR)
    shutil.copy2(HERE / "consent_template.md", d / "consent.md")
    print("%s に %d本 置きました" % (d, min(len(segs), a.clips)))

    print("特徴を出しています…")
    import torch
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
    clips = [str(p) for p in sorted((d / "clips").glob("*.wav"))]
    g, s = model.get_conditioning_latents(audio_path=clips)
    np.save(d / "latent.npy", g.cpu().numpy())
    np.save(d / "embedding.npy", s.cpu().numpy())
    print("できました。make_voice.py を走らせると、この人を含めた平均になります")

    io.open(HERE / "add_result.txt", "w", encoding="utf-8").write(
        "%s を加えました（区間%d本 / 合計%.1f秒）\n" % (a.name, min(len(segs), a.clips), total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
