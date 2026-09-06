# -*- coding: utf-8 -*-
"""声の憲法（弱いロボット8条）に沿った鳴き声（2026-09-06）。

前の版からの変更:
  第2条 ピッチは1.1〜1.2倍。前は+5半音（1.33倍）で上げすぎだった
  第3条 抑揚の型を増やす。音色より抑揚の方が効く
  第5条 間を置く。頭に沈黙を入れ、粒の間隔もばらす
  第6条 ためらいを音にする。迷ってから答える型を足す
  やらないこと 毎回同じ音を返さない（プルンのゆらぎ）→ 粒の選び方と長さを毎回ずらす

声の材料は、大人4人と甥（本人が指した6区間・16.4秒）。
"""
import io
import subprocess
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
OUT = HERE / "work" / "babble2"
SR = 24000
VOWELS = "あいうえおあえいおう"

PITCH = 2.4          # 半音。2^(2.4/12)=1.15倍。憲法第2条の1.1〜1.2倍

# 抑揚の型。(半音, その粒の長さの倍率)。間は別に持つ
SHAPES = {
    "きづいた":   [(2, 1.0), (5, 0.8), (3, 1.2)],
    "うれしい":   [(3, 0.9), (6, 0.8), (8, 0.7), (5, 1.3)],
    "ごきげん":   [(0, 1.2), (2, 1.0), (1, 1.1), (3, 0.9), (0, 1.4)],
    "ふん":       [(-1, 0.8), (-4, 1.1)],
    "きになる":   [(1, 1.0), (4, 0.9), (7, 1.2)],
    "しょんぼり": [(0, 1.1), (-2, 1.2), (-5, 1.5)],
    "ねむい":     [(0, 1.6), (-1, 1.8), (-2, 2.0)],
    "こまった":   [(2, 0.9), (0, 1.0), (2, 0.9), (-1, 1.3)],
}

# 第6条 ためらってから答える。前半は迷い、間を置いて、後半で答える
HESITATE = {
    "えーと_うれしい": ([(0, 1.4), (-1, 1.2), (0, 1.6)], 0.9,
                        [(4, 0.8), (8, 0.7), (6, 1.2)]),
    "えーと_きになる": ([(0, 1.3), (1, 1.5)], 1.1,
                        [(2, 0.9), (6, 1.3)]),
}


def blips(wav, n, seed):
    """母音の連なりから粒を切り出す。毎回すこし違う粒を選ぶ（ゆらぎ）。"""
    rng = np.random.default_rng(seed)
    win = int(0.02 * SR)
    f = wav[: len(wav) // win * win].reshape(-1, win)
    rms = np.sqrt((f ** 2).mean(axis=1) + 1e-12)
    thr = rms.mean() * 0.8
    on, out, start = False, [], 0
    for i, v in enumerate(rms > thr):
        if v and not on:
            start, on = i, True
        elif not v and on:
            on = False
            a, b = start * win, i * win
            if b - a > int(0.07 * SR):
                out.append(wav[a:min(b, a + int(0.16 * SR))])
    if not out:
        return []
    pick = rng.permutation(len(out))
    return [out[pick[i % len(out)]] for i in range(n)]


def shift(x, semitones):
    r = 2 ** (semitones / 12.0)
    idx = np.arange(0, len(x), r)
    idx = idx[idx < len(x) - 1].astype(np.float32)
    lo = idx.astype(np.int32)
    fr = idx - lo
    return x[lo] * (1 - fr) + x[lo + 1] * fr


def stretch(x, k):
    """粒の長さを変える。長いほどゆったり、短いほど跳ねる。"""
    n = max(8, int(len(x) * k))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)


def env(x):
    n = max(8, int(0.008 * SR))
    y = x.copy()
    y[:n] *= np.linspace(0, 1, n)
    y[-n:] *= np.linspace(1, 0, n)
    return y


def utter(raw, shape, seed, lead=0.0, gap_base=0.05):
    """抑揚の型から、ひと続きの鳴き声を組む。"""
    rng = np.random.default_rng(seed)
    parts = blips(raw, len(shape), seed)
    if not parts:
        return np.zeros(1, dtype=np.float32)
    out = [np.zeros(int(lead * SR), dtype=np.float32)] if lead else []
    for p, (st, dur) in zip(parts, shape):
        g = env(stretch(shift(p, st + PITCH), dur))
        out.append(g)
        out.append(np.zeros(int((gap_base + rng.uniform(0, 0.03)) * SR),
                            dtype=np.float32))
    return np.concatenate(out)


def main():
    rep = io.open(HERE / "babble2_result.txt", "w", encoding="utf-8")

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

    voices = {"kid": (kl, ke), "half": ((al + kl) / 2, (ae + ke) / 2)}
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    for vname, (g, s) in voices.items():
        say("")
        say("■ %s の声で" % ("甥っ子さんだけ" if vname == "kid" else "大人と半々"))
        raw = np.asarray(model.inference(VOWELS, "ja",
                                         torch.tensor(g).to(dev),
                                         torch.tensor(s).to(dev),
                                         temperature=0.7, speed=1.0)["wav"])
        for i, (name, shape) in enumerate(SHAPES.items()):
            y = utter(raw, shape, seed=i * 7 + len(vname), lead=0.35)
            y = y / (np.abs(y).max() + 1e-9) * 0.75      # 第7条 音量を上げない
            sf.write(str(OUT / ("%s_%s.wav" % (vname, name))), y, SR)
            say("  %s" % name)
        for j, (name, (h, gapsec, ans)) in enumerate(HESITATE.items()):
            a = utter(raw, h, seed=90 + j, lead=0.5, gap_base=0.12)
            b = utter(raw, ans, seed=95 + j, gap_base=0.05)
            y = np.concatenate([a, np.zeros(int(gapsec * SR), dtype=np.float32), b])
            y = y / (np.abs(y).max() + 1e-9) * 0.75
            sf.write(str(OUT / ("%s_%s.wav" % (vname, name))), y, SR)
            say("  %s（ためらってから答える）" % name)

    # 実機で鳴らしたときの音も1つ作る
    src = OUT / "kid_うれしい.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                    "-af", "highpass=f=280,lowpass=f=6200,aecho=0.8:0.85:22:0.18",
                    "-ac", "1", "-ar", "16000", str(OUT / "kid_うれしい_実機.wav")],
                   check=False)
    say("")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
