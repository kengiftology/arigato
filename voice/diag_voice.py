# -*- coding: utf-8 -*-
"""どこで違和感が出ているかを切り分ける（2026-09-06）。

本人：「すべての声がなにかしっくりこない。なぜだろうか」

疑わしいものを一つずつ外して比べる。
  A 本物の録音（p_a本人）          … これが基準。合成は一切していない
  B XTTSにp_a本人の声で喋らせる    … Aとの差＝合成エンジンのせい
  C 混ぜた声（加工なし）            … Bとの差＝混ぜたせい
  D 混ぜた声＋ピッチ上げ            … Cとの差＝音程をいじったせい

AとBの差がいちばん大きければ、原因はXTTSの日本語。
その場合は、喋る仕事を日本語専用のエンジンに移し、
声の質だけをこちらから借りる形にする。
"""
import glob
import io
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
OUT = HERE / "work" / "diag"
SR = 24000
LINE = "あのね、お鍋がね、こんなに出ててね。なんだか、そわそわするなあ。"


def shift(x, semitones):
    r = 2 ** (semitones / 12.0)
    idx = np.arange(0, len(x), r)
    idx = idx[idx < len(x) - 1].astype(np.float32)
    lo = idx.astype(np.int32)
    fr = idx - lo
    return x[lo] * (1 - fr) + x[lo + 1] * fr


def main():
    rep = io.open(HERE / "diag_result.txt", "w", encoding="utf-8")

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

    # A 本物の録音。合成していない、そのままの声
    clips = sorted(glob.glob(str(PEOPLE / "p_a" / "clips" / "*.wav")))[:4]
    parts = []
    for c in clips:
        with wave.open(c, "rb") as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        parts.append(a.astype(np.float32) / 32768.0)
        parts.append(np.zeros(3200, dtype=np.float32))
    sf.write(str(OUT / "A_本物の録音.wav"), np.concatenate(parts), 16000)
    say("A 本物の録音（p_a本人・合成なし）")

    path, _, _ = ModelManager().download_model(
        "tts_models/multilingual/multi-dataset/xtts_v2")
    cfg = XttsConfig()
    cfg.load_json(str(Path(path) / "config.json"))
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)

    la = np.load(PEOPLE / "p_a" / "latent.npy")
    ea = np.load(PEOPLE / "p_a" / "embedding.npy")
    adults = [d for d in sorted(PEOPLE.glob("p_*")) if d.name != "p_kid"]
    al = np.mean([np.load(d / "latent.npy") for d in adults], axis=0)
    ae = np.mean([np.load(d / "embedding.npy") for d in adults], axis=0)
    kl = np.load(PEOPLE / "p_kid" / "latent.npy")
    ke = np.load(PEOPLE / "p_kid" / "embedding.npy")

    for tag, g, s, pitch in (
            ("B_XTTSが本人を真似", la, ea, 0.0),
            ("C_混ぜた声", (al + kl) / 2, (ae + ke) / 2, 0.0),
            ("D_混ぜた声とピッチ", (al + kl) / 2, (ae + ke) / 2, 1.6)):
        wav = np.asarray(model.inference(
            LINE, "ja", torch.tensor(g).to(dev), torch.tensor(s).to(dev),
            temperature=0.75, speed=1.05)["wav"])
        if pitch:
            wav = shift(wav, pitch)
        sf.write(str(OUT / (tag + ".wav")), wav, SR)
        say("%s" % tag)

    say("")
    say("AとBの差がいちばん大きければ、原因はXTTSの日本語。")
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
