# -*- coding: utf-8 -*-
"""4人の声を材料にして、意味を持たない鳴き声を作る（2026-09-05）。

本人の評価：「かわいらしさがない。ミーム的には面白いけど、それ以上はない」

人の声を音程だけ上げても、聞き手は生き物ではなく「加工」を聞く。
そしてもっと根の深い衝突がある――日本語がはっきり聞こえるほど、
それは人に聞こえる。地霊は人ではない。

あつ森がかわいいのは意味が分からないからで、聞き手が勝手に意味を補う。
分かってしまうと補う余地がなくなる。当初の設計（音はあつ森方式）は
そこを踏まえていたのに、日本語を足したときに手放していた。

そこで、声の出どころは4人のままにして、聞こえ方だけ生き物に寄せる。
  1) 平均した声に母音だけを喋らせる
  2) 短い粒に刻む
  3) 抑揚の形（上がる・下がる・弾む）を粒の並びに与える
言葉にならないので、意味は字ではなく抑揚で届く。
"""
import io
import subprocess
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
OUT = HERE / "work" / "babble"
SR = 24000

VOWELS = "あいうえおあえいおう"          # 母音だけ。子音が入ると言葉に寄る

# 抑揚の形。数字は半音。人はここから気もちを読み取る
SHAPES = {
    "きづいた": [2, 5, 3],             # あ、来た
    "うれしい": [3, 6, 8, 6],          # 弾んで上がる
    "ごきげん": [0, 2, 1, 3, 1],       # ゆるく波打つ
    "ふん": [-1, -3],                  # 短く落とす
    "きになる": [1, 4, 7],             # 語尾が上がる＝問いかけ
    "しょんぼり": [0, -2, -4],         # 落ちていく
}


def blips(wav: np.ndarray, n: int) -> list:
    """母音の連なりから、粒を切り出す。音量の山を1粒とみなす。"""
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
    return out[:n] if len(out) >= n else (out * (n // max(1, len(out)) + 1))[:n]


def shift(x: np.ndarray, semitones: float) -> np.ndarray:
    """音の高さを変える。声の太さごと動くので、体の大きさが変わって聞こえる。"""
    r = 2 ** (semitones / 12.0)
    idx = np.arange(0, len(x), r)
    idx = idx[idx < len(x) - 1].astype(np.float32)
    lo = idx.astype(np.int32)
    frac = idx - lo
    return x[lo] * (1 - frac) + x[lo + 1] * frac


def envelope(x: np.ndarray) -> np.ndarray:
    """粒の頭と尻をなめらかにする。ぶつ切りだと機械に聞こえる。"""
    n = max(8, int(0.008 * SR))
    y = x.copy()
    y[:n] *= np.linspace(0, 1, n)
    y[-n:] *= np.linspace(1, 0, n)
    return y


def main() -> int:
    rep = io.open(HERE / "babble_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + "\n")
        rep.flush()

    import torch
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager
    import soundfile as sf

    path, _, _ = ModelManager().download_model(
        "tts_models/multilingual/multi-dataset/xtts_v2")
    cfg = XttsConfig()
    cfg.load_json(str(Path(path) / "config.json"))
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)

    lat = [np.load(d / "latent.npy") for d in sorted(PEOPLE.glob("p_*"))]
    emb = [np.load(d / "embedding.npy") for d in sorted(PEOPLE.glob("p_*"))]
    g = torch.tensor(np.mean(lat, axis=0)).to(dev)
    s = torch.tensor(np.mean(emb, axis=0)).to(dev)
    say("4人の平均から、母音だけを喋らせています…")
    raw = np.asarray(model.inference(VOWELS, "ja", g, s,
                                     temperature=0.7, speed=1.0)["wav"])

    OUT.mkdir(parents=True, exist_ok=True)
    sf.write(str(OUT / "00_もとの母音.wav"), raw, SR)

    say("\n粒に刻んで、抑揚をつけています…")
    for name, shape in SHAPES.items():
        parts = blips(raw, len(shape))
        pieces, gap = [], np.zeros(int(0.045 * SR), dtype=np.float32)
        for p, st in zip(parts, shape):
            # 全体を少し高くする。小さい生き物に聞こえる高さ
            pieces.append(envelope(shift(p, st + 5)))
            pieces.append(gap)
        y = np.concatenate(pieces)
        y = y / (np.abs(y).max() + 1e-9) * 0.9
        sf.write(str(OUT / (name + ".wav")), y, SR)
        # キッチンのスピーカーで鳴らしたときの音
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(OUT / (name + ".wav")),
             "-af", "highpass=f=280,lowpass=f=6200,aecho=0.8:0.85:22:0.18",
             "-ac", "1", "-ar", "16000", str(OUT / (name + "_実機.wav"))],
            check=False)
        say("  %-10s 粒%d個" % (name, len(shape)))

    say("\nできました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
