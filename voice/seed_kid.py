# -*- coding: utf-8 -*-
"""本人が指した数本を手がかりに、似た声を全体から集める（2026-09-05）。

194本すべてを人に聴かせるのは無理がある。数本だけ「これは甥っ子」と
教えてもらい、その平均に近いものを機械が拾う。

指してもらったのは 43,67,71,72,89,92（合計16.5秒）。
足りないので、ここから広げる。
"""
import argparse
import glob
import io
import re
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
WORK = HERE / "work" / "kid"
TEXTD = HERE / "work" / "kid_text"
SR = 16000


def read_wav(p):
    with wave.open(str(p), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return a.astype(np.float32) / 32768.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="43,67,71,72,89,92")
    ap.add_argument("--top", type=int, default=40)
    a = ap.parse_args()

    import soundfile as sf
    import torch
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager

    x = np.concatenate([read_wav(p) for p in sorted(glob.glob(str(WORK / "a*.wav")))])
    segs = np.load(TEXTD / "segs.npy")
    seeds = [int(n) for n in a.seeds.split(",")]

    emb_path = TEXTD / "all_emb.npy"
    if emb_path.exists():
        E = np.load(emb_path)
        print("特徴は前に出したものを使います")
    else:
        path, _, _ = ModelManager().download_model(
            "tts_models/multilingual/multi-dataset/xtts_v2")
        cfg = XttsConfig()
        cfg.load_json(str(Path(path) / "config.json"))
        model = Xtts.init_from_config(cfg)
        model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        model.to(dev)
        tmp = TEXTD / "e.wav"
        out = []
        print("194本の特徴を出しています...")
        for i, (s, e) in enumerate(segs):
            sf.write(str(tmp), x[int(s * SR):int(e * SR)], SR)
            try:
                _g, sp = model.get_conditioning_latents(audio_path=[str(tmp)])
                out.append(sp.cpu().numpy().flatten())
            except Exception:
                out.append(np.zeros(512, dtype=np.float32))
            if (i + 1) % 50 == 0:
                print("  %d / %d" % (i + 1, len(segs)), flush=True)
        E = np.stack(out)
        np.save(emb_path, E)

    N = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    ref = N[seeds].mean(axis=0)
    ref = ref / (np.linalg.norm(ref) + 1e-9)
    sim = N @ ref

    lines = io.open(HERE / "kid_text.txt", encoding="utf-8").read().splitlines()[2:]
    tbl = {}
    for L in lines:
        m = re.match(r"\s*(\d+)\s", L)
        if m:
            tbl[int(m.group(1))] = L.strip()

    o = io.open(HERE / "kid_seed.txt", "w", encoding="utf-8")
    o.write("手がかりにした区間: %s（合計 %.1f 秒）%s"
            % (a.seeds, sum(segs[i][1] - segs[i][0] for i in seeds), chr(10)))
    o.write("それに近い順:" + chr(10) + chr(10))
    order = np.argsort(-sim)
    picked, total = [], 0.0
    pieces = []
    for rank, i in enumerate(order[: a.top]):
        i = int(i)
        d = segs[i][1] - segs[i][0]
        mark = "★手がかり" if i in seeds else "         "
        o.write("%5.1f秒〜 近さ%.3f %s %s%s"
                % (total, sim[i], mark, tbl.get(i, str(i)), chr(10)))
        picked.append(i)
        pieces.append(x[int(segs[i][0] * SR):int(segs[i][1] * SR)])
        pieces.append(np.zeros(int(0.25 * SR), dtype=np.float32))
        total += d + 0.25
    o.write(chr(10) + "合計 %.1f 秒" % total + chr(10))
    o.close()
    sf.write(str(HERE / "work" / "kid_seed.wav"), np.concatenate(pieces), SR)
    np.save(TEXTD / "seed_order.npy", np.array(picked))
    print("できました。kid_seed.wav と kid_seed.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
