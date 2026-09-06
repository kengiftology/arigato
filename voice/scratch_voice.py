# -*- coding: utf-8 -*-
"""声をゼロから組み立てる（2026-09-06）。

これまでは人の声を真似て作っていた。元が人なので、出てくるのは人の声になる。
平均しても混ぜても、人の声の空間から出られない。

ここでは録音を一切使わない。声を部品から組み立てる。

  音源  … 声帯の振動（パルス列）＋息（雑音）
  共鳴  … 喉と口の空洞（共鳴を3つ）。この位置で母音が決まる
  大きさ… 共鳴の位置を上下させると、体の大きさが変わる

一度も誰のものでもなかった音なので、誰の声でもない。
そして数値で指定するので、あとから動かせる。

つまみ:
  f0     声の高さ（Hz）
  size   体の大きさ（1.0が成人。小さいほど共鳴が上がる）
  breath 息の混ざり具合（0〜1）
  jitter 声の揺れ（生きもの感。0で機械、0.03くらいで自然）
"""
import io
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).parent
OUT = HERE / "work" / "scratch"
SR = 24000

# 日本語の母音の共鳴の位置（Hz）。成人のおおよその値
VOWELS = {
    "a": (800, 1200, 2800),
    "i": (300, 2700, 3300),
    "u": (350, 1200, 2300),
    "e": (500, 2100, 2900),
    "o": (500, 900, 2600),
    "n": (300, 1000, 2200),
}


def source(n, f0, jitter, breath, rng):
    """声のもと。声帯のパルスに、息の雑音を混ぜる。"""
    x = np.zeros(n, dtype=np.float32)
    t, i = 0.0, 0
    while i < n:
        p = SR / (f0 * (1.0 + rng.normal(0, jitter)))   # 揺れ
        i = int(t)
        if i < n:
            x[i] = 1.0
        t += max(8.0, p)
    # パルスを少しなまらせる（尖ったままだと金属的になる）
    k = np.exp(-np.arange(40) / 6.0).astype(np.float32)
    x = np.convolve(x, k)[:n]
    if breath > 0:
        x = x * (1 - breath * 0.5) + rng.normal(0, 1, n).astype(np.float32) * breath * 0.35
    return x


def resonate(x, freq, bw=90.0):
    """共鳴をひとつ通す。口の中の空洞に相当する。"""
    r = np.exp(-np.pi * bw / SR)
    th = 2 * np.pi * freq / SR
    a1, a2 = -2 * r * np.cos(th), r * r
    y = np.zeros_like(x)
    y1 = y2 = 0.0
    g = (1 - r) * np.sqrt(1 - 2 * r * np.cos(2 * th) + r * r)
    for i in range(len(x)):
        v = g * x[i] - a1 * y1 - a2 * y2
        y[i] = v
        y2, y1 = y1, v
    return y


def envelope(n, attack=0.02, release=0.06):
    e = np.ones(n, dtype=np.float32)
    a, r = int(attack * SR), int(release * SR)
    e[:a] = np.linspace(0, 1, a)
    e[-r:] = np.linspace(1, 0, r)
    return e


def syllable(v, dur, f0, size, breath, jitter, rng, closed=False):
    """ひと音。closed=Trueなら頭に短い閉じ（子音らしさ）をつける。"""
    n = int(dur * SR)
    s = source(n, f0, jitter, breath, rng)
    f1, f2, f3 = (f * size for f in VOWELS[v])
    y = (resonate(s, f1, 80) * 1.0
         + resonate(s, f2, 100) * 0.5
         + resonate(s, f3, 140) * 0.25)
    y *= envelope(n)
    if closed:
        gap = int(0.035 * SR)
        y = np.concatenate([np.zeros(gap, dtype=np.float32), y])
    return y


def utter(seq, f0s, durs, size, breath, jitter, seed):
    """ひと続きの発話。seq＝母音の並び、f0s＝各音の高さ。"""
    rng = np.random.default_rng(seed)
    out = []
    for k, (v, f0, d) in enumerate(zip(seq, f0s, durs)):
        out.append(syllable(v, d, f0, size, breath, jitter, rng, closed=(k > 0)))
        out.append(np.zeros(int(0.02 * SR), dtype=np.float32))
    y = np.concatenate(out)
    return y / (np.abs(y).max() + 1e-9) * 0.8


def main():
    rep = io.open(HERE / "scratch_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    # 「むー、むむ、むー?」に相当する形。母音と高さで気もちを作る
    seq = ["u", "u", "u", "o"]
    durs = [0.22, 0.13, 0.13, 0.30]

    settings = [
        ("1_小さい生きもの", dict(f0=300, size=1.35, breath=0.20, jitter=0.030)),
        ("2_もっと小さい",   dict(f0=380, size=1.55, breath=0.25, jitter=0.035)),
        ("3_おおきめ",       dict(f0=210, size=1.10, breath=0.15, jitter=0.020)),
        ("4_息おおめ",       dict(f0=300, size=1.35, breath=0.45, jitter=0.030)),
        ("5_ゆれすくなめ",   dict(f0=300, size=1.35, breath=0.20, jitter=0.008)),
        ("6_ゆれおおめ",     dict(f0=300, size=1.35, breath=0.20, jitter=0.060)),
    ]
    for i, (name, kw) in enumerate(settings):
        f0 = kw.pop("f0")
        f0s = [f0, f0 * 1.10, f0 * 1.05, f0 * 1.22]      # 語尾が上がる＝問いかけ
        y = utter(seq, f0s, durs, seed=i, **kw)
        sf.write(str(OUT / (name + ".wav")), y, SR)
        say("%-16s 高さ%3dHz 大きさ%.2f 息%.2f ゆれ%.3f"
            % (name, f0, kw["size"], kw["breath"], kw["jitter"]))

    # 同じつまみで、気もちを書き分ける
    base = dict(size=1.35, breath=0.20, jitter=0.030)
    moods = {
        "うれしい":   (["a", "a", "i"], [280, 340, 400], [0.16, 0.14, 0.26]),
        "きづいた":   (["a", "o"],      [300, 360],      [0.12, 0.22]),
        "ふん":       (["u", "n"],      [280, 230],      [0.14, 0.22]),
        "きになる":   (["u", "u", "o"], [290, 310, 380], [0.18, 0.12, 0.30]),
        "しょんぼり": (["o", "u", "u"], [280, 250, 215], [0.20, 0.18, 0.34]),
    }
    say("")
    for j, (name, (sq, fs, ds)) in enumerate(moods.items()):
        y = utter(sq, fs, ds, seed=20 + j, **base)
        sf.write(str(OUT / ("mood_" + name + ".wav")), y, SR)
        say("mood_%s" % name)

    say("")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
