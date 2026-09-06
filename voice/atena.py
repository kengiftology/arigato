# -*- coding: utf-8 -*-
"""宛名のある喋り方を試す（2026-09-06・弱いロボット第4章）。

岡田の診断：声を良くしても届かない。足りないのは宛名。
「コンド、トウキョウデ、オリンピックガ、カイサイガ、キマッタンデスッテ」は冷たく、
「あのね、こんどね、っていうかね」は相手に配慮している（p.119）。

宛名の作り方は4つ：助詞「ね」／フィラー／発語片に分解／相手に合わせる。
名指しは使わないので、台帳#12とも両立する。

同じ中身を、流暢さの違う4通りで喋らせて比べる。
"""
import io
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
OUT = HERE / "work" / "atena"
SR = 24000
PITCH = 1.6          # 半音。1.1倍。憲法第2条の下限側（喋らせるので控えめに）

# 同じ中身。流暢さだけが違う。
LINES = [
    ("1_流暢",       "お鍋がたくさん出ています。片付いていません。", 1.15),
    ("2_ねを足す",   "お鍋がこんなに出てるね。なんだかそわそわするなあ。", 1.10),
    ("3_発語片",     "あのね、お鍋がね、こんなに出ててね。なんだか、そわそわするなあ。", 1.05),
    ("4_ためらい",   "あのね、えーとね……お鍋がね、こんなに……なんだろうね、そわそわするなあ。", 1.00),
]

# 装置の役割（埋もれたありがとうを届ける）を、宛名つきで言うとどうなるか
NEWS = [
    ("5_知らせ_流暢",   "シンクが片付けられました。", 1.15),
    ("6_知らせ_宛名",   "あのね、シンクがね、きれいになっててね……だれかがやってくれたのかなあ。", 1.02),
]


def shift(x, semitones):
    r = 2 ** (semitones / 12.0)
    idx = np.arange(0, len(x), r)
    idx = idx[idx < len(x) - 1].astype(np.float32)
    lo = idx.astype(np.int32)
    fr = idx - lo
    return x[lo] * (1 - fr) + x[lo + 1] * fr


def main():
    rep = io.open(HERE / "atena_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

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

    kid = PEOPLE / "p_kid"
    adults = [d for d in sorted(PEOPLE.glob("p_*")) if d.name != "p_kid"]
    al = np.mean([np.load(d / "latent.npy") for d in adults], axis=0)
    ae = np.mean([np.load(d / "embedding.npy") for d in adults], axis=0)
    kl, ke = np.load(kid / "latent.npy"), np.load(kid / "embedding.npy")
    g = torch.tensor((al + kl) / 2).to(dev)      # 大人と子ども半々
    s = torch.tensor((ae + ke) / 2).to(dev)

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    for name, text, sp in LINES + NEWS:
        wav = np.asarray(model.inference(text, "ja", g, s,
                                         temperature=0.75, speed=sp)["wav"])
        y = shift(wav, PITCH)
        y = np.concatenate([np.zeros(int(0.5 * SR), dtype=np.float32), y])
        y = y / (np.abs(y).max() + 1e-9) * 0.75
        sf.write(str(OUT / (name + ".wav")), y, SR)
        say("%-14s 速さ%.2f 「%s」" % (name, sp, text))

    say("")
    say("1と3を聴き比べるのが本題。中身は同じで、流暢さだけが違う。")
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
