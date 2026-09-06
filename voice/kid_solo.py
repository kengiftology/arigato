# -*- coding: utf-8 -*-
"""甥っ子さんの声だけで喋らせる（2026-09-06）。

混ぜるのをやめ、本人の声そのままで、宛名のある喋り方を試す。
混ぜたことが違和感の一因なら、これで消えるはず。

「誰の声でもない」という研究上の筋は一旦置く。まず耳で成立するかを見る。
"""
import glob
import io
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
OUT = HERE / "work" / "kidsolo"
SR = 24000

LINES = [
    ("1_流暢",     "お鍋がたくさん出ています。", 1.10, 0.0),
    ("2_発語片",   "あのね、お鍋がね、こんなに出ててね。なんだか、そわそわするなあ。", 1.02, 0.0),
    ("3_ためらい", "あのね、えーとね……お鍋がね、こんなに……なんだろうね、そわそわするなあ。", 0.98, 0.0),
    ("4_知らせ",   "あのね、シンクがね、きれいになっててね……だれかがやってくれたのかなあ。", 1.00, 0.0),
    ("5_短い",     "あ、きた。", 1.00, 0.0),
    ("6_発語片_少し高く", "あのね、お鍋がね、こんなに出ててね。なんだか、そわそわするなあ。", 1.02, 1.6),
]


def shift(x, semitones):
    r = 2 ** (semitones / 12.0)
    idx = np.arange(0, len(x), r)
    idx = idx[idx < len(x) - 1].astype(np.float32)
    lo = idx.astype(np.int32)
    fr = idx - lo
    return x[lo] * (1 - fr) + x[lo + 1] * fr


def main():
    rep = io.open(HERE / "kidsolo_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    import torch
    import soundfile as sf
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    # 比較のため、本人の録音そのものも置く
    parts = []
    for c in sorted(glob.glob(str(PEOPLE / "p_kid" / "clips" / "*.wav"))):
        with wave.open(c, "rb") as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        parts.append(a.astype(np.float32) / 32768.0)
        parts.append(np.zeros(3200, dtype=np.float32))
    sf.write(str(OUT / "0_本物の録音.wav"), np.concatenate(parts), 16000)
    say("0 本物の録音（甥っ子さん・合成なし）")

    path, _, _ = ModelManager().download_model(
        "tts_models/multilingual/multi-dataset/xtts_v2")
    cfg = XttsConfig()
    cfg.load_json(str(Path(path) / "config.json"))
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)

    g = torch.tensor(np.load(PEOPLE / "p_kid" / "latent.npy")).to(dev)
    s = torch.tensor(np.load(PEOPLE / "p_kid" / "embedding.npy")).to(dev)

    for tag, text, sp, pitch in LINES:
        wav = np.asarray(model.inference(text, "ja", g, s,
                                         temperature=0.8, speed=sp)["wav"])
        if pitch:
            wav = shift(wav, pitch)
        y = np.concatenate([np.zeros(int(0.45 * SR), dtype=np.float32), wav])
        y = y / (np.abs(y).max() + 1e-9) * 0.8
        sf.write(str(OUT / (tag + ".wav")), y, SR)
        say("%-18s 「%s」" % (tag, text))

    say("")
    say("0と2を聴き比べれば、合成でどれだけ崩れたかが分かります。")
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
