# -*- coding: utf-8 -*-
"""番号で指定した区間だけを取り出す（2026-09-05）。

声の高さでは子どもと親（女性）が分けられなかった。どの帯にも両方入る。
文字起こしを見れば、崩れた言い方や短い呼びかけで見分けがつく。
（甥は「そーちゃん」。そーちゃんに呼びかけている区間は親のもの）

  python take_kid.py 3,38,40,41 --name p_kid
  python take_kid.py 3,38,40,41 --preview      # 繋げて聴くだけ
"""
import argparse
import glob
import shutil
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
WORK = HERE / "work" / "kid"
TEXTD = HERE / "work" / "kid_text"
PEOPLE = HERE / "people"
SR = 16000


def read_wav(p):
    with wave.open(str(p), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return a.astype(np.float32) / 32768.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("nums")
    ap.add_argument("--name", default="p_kid")
    ap.add_argument("--preview", action="store_true")
    a = ap.parse_args()

    import soundfile as sf
    x = np.concatenate([read_wav(p) for p in sorted(glob.glob(str(WORK / "a*.wav")))])
    segs = np.load(TEXTD / "segs.npy")
    idx = [int(n) for n in a.nums.replace(" ", "").split(",") if n != ""]
    total = sum(segs[i][1] - segs[i][0] for i in idx)
    print("選んだ区間 %d本 / 合計 %.1f 秒" % (len(idx), total))

    if a.preview:
        pieces = []
        for i in idx:
            s, e = segs[i]
            pieces.append(x[int(s * SR):int(e * SR)])
            pieces.append(np.zeros(int(0.25 * SR), dtype=np.float32))
        out = HERE / "work" / "kid_pick.wav"
        sf.write(str(out), np.concatenate(pieces), SR)
        print("聴いてみる: %s" % out)
        return 0

    d = PEOPLE / a.name
    if d.exists():
        shutil.rmtree(d)
    (d / "clips").mkdir(parents=True)
    for j, i in enumerate(idx):
        s, e = segs[i]
        sf.write(str(d / "clips" / ("%02d.wav" % j)), x[int(s * SR):int(e * SR)], SR)
    shutil.copy2(HERE / "consent_template.md", d / "consent.md")

    print("特徴を出しています...")
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
    print("%s に %d本 置きました" % (d, len(clips)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
