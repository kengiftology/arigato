# -*- coding: utf-8 -*-
"""ゼロから組んだ声で、日本語を喋らせる（2026-09-06）。

母音だけでは言葉にならない。子音を足す。
子音は種類ごとに作り方が違う。

  破裂（か・た・ぱ）  … 口を閉じる沈黙 → 短い破裂の雑音 → 母音
  鼻音（な・ま）      … 鼻の共鳴（低くこもった音）→ 母音
  摩擦（さ・は）      … 息の雑音を通す → 母音
  破擦（ちゃ・つ）    … 沈黙 → 摩擦 → 母音
  はじき（ら）        … ごく短い接触
  わたり（や・わ）    … 隣の位置から母音へ滑る
  っ                  … 沈黙だけ

共鳴の位置を子音の場所から母音へ滑らせるのが、言葉らしさの正体。
録音は使っていないので、誰の声でもないまま。
"""
import io
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import lfilter

HERE = Path(__file__).parent
OUT = HERE / "work" / "kana"
SR = 24000

# 良いと言われた声（ずんだもん・後鬼・四国めたん）から測った値に寄せる。
# 3人の平均は 高さ337Hz / 共鳴 650・1702・2816・3569Hz。
# 以前は共鳴1を1040Hz相当に置いていて60%高すぎ、それがこもりの正体だった。
V = {"a": (700, 1300, 2700), "i": (330, 2400, 3100), "u": (380, 1250, 2500),
     "e": (520, 1900, 2750), "o": (500, 1000, 2650)}

# 子音の種類と、口を閉じる場所（そこから母音へ滑る）
C = {
    "":   ("none", None),
    "k":  ("stop", (300, 1900, 2600)), "g": ("vstop", (300, 1900, 2600)),
    "t":  ("stop", (350, 1700, 2600)), "d": ("vstop", (350, 1700, 2600)),
    "p":  ("stop", (300, 900, 2200)),  "b": ("vstop", (300, 900, 2200)),
    "s":  ("fric", (350, 1700, 2600)), "z": ("fric", (350, 1700, 2600)),
    "h":  ("fric", (500, 1500, 2500)), "f": ("fric", (350, 1100, 2200)),
    "sh": ("fric", (300, 2000, 2700)), "j": ("aff", (300, 2000, 2700)),
    "ch": ("aff", (300, 2000, 2700)),  "ts": ("aff", (350, 1700, 2600)),
    "n":  ("nasal", (300, 1000, 2200)), "m": ("nasal", (280, 900, 2100)),
    "r":  ("flap", (350, 1600, 2600)),
    "y":  ("glide", (300, 2400, 3000)), "w": ("glide", (350, 800, 2200)),
}

KANA = {}
for row, con in (("あいうえお", ""), ("かきくけこ", "k"), ("さしすせそ", "s"),
                 ("たちつてと", "t"), ("なにぬねの", "n"), ("はひふへほ", "h"),
                 ("まみむめも", "m"), ("らりるれろ", "r"), ("がぎぐげご", "g"),
                 ("ざじずぜぞ", "z"), ("だぢづでど", "d"), ("ばびぶべぼ", "b"),
                 ("ぱぴぷぺぽ", "p")):
    for k, v in zip(row, "aiueo"):
        KANA[k] = (con, v)
KANA.update({"し": ("sh", "i"), "ち": ("ch", "i"), "つ": ("ts", "u"),
             "ふ": ("f", "u"), "じ": ("j", "i"), "や": ("y", "a"),
             "ゆ": ("y", "u"), "よ": ("y", "o"), "わ": ("w", "a"),
             "を": ("", "o"), "ん": ("n", "n")})
SMALL = {"ゃ": "a", "ゅ": "u", "ょ": "o"}


def parse(text):
    """かなを、子音と母音の組に分ける。ー は伸ばし、っ は詰まり。"""
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch in "、。 　":
            out.append(("pause", None, 0.16 if ch == "、" else 0.3))
            i += 1
            continue
        if ch == "っ":
            out.append(("sokuon", None, 0.09))
            i += 1
            continue
        if ch in "ーぁぃぅぇぉ":
            if out:
                out[-1] = (out[-1][0], out[-1][1], out[-1][2] + 0.10)
            i += 1
            continue
        if ch not in KANA:
            i += 1
            continue
        con, vow = KANA[ch]
        if i + 1 < len(text) and text[i + 1] in SMALL:
            vow = SMALL[text[i + 1]]
            con = {"k": "k", "ch": "ch", "sh": "sh", "n": "n", "h": "h",
                   "m": "m", "r": "r", "g": "g", "j": "j", "b": "b",
                   "p": "p"}.get(con, con)
            i += 1
        out.append((con, vow, 0.15))
        i += 1
    return out


# 共鳴の幅（Hz）。狭いほど母音がはっきりする。広すぎると谷が埋まってこもる
BW = (60, 90, 120, 150, 200)


def reson(x, freqs, bw=None):
    """喉と口の共鳴を通す。

    以前は共鳴を並べて足していた。これだと山と山のあいだの谷が埋まり、
    母音が母音として立たない（こもる）のに、余計な成分は残る（ざらつく）。
    実際の喉と口は直列につながった1本の管なので、順に通していく。
    こうすると谷が深くなり、母音がはっきりする。

    上の方の共鳴（4番目・5番目）も足す。無いと 高い成分が死んでこもる。"""
    f = list(freqs)
    while len(f) < 5:                     # 上の共鳴。声の明るさを支える
        f.append(f[-1] + 1000)
    b = bw or BW
    y = x
    for fi, bi in zip(f, b):
        if fi >= SR / 2 - 100:
            continue
        r = np.exp(-np.pi * bi / SR)
        th = 2 * np.pi * fi / SR
        a = [1.0, -2 * r * np.cos(th), r * r]
        k = 1 - 2 * r * np.cos(th) + r * r      # 直流で利得1になるようにする
        y = lfilter([k], a, y)
    return y.astype(np.float32)


def glottal(n, f0, jitter, breath, rng):
    """声帯の動き。

    点（インパルス）を並べると、全ての周波数が同じ強さで入り、
    ザラついた音になる。本物の声帯はなめらかに開いて閉じるので、
    高い方が自然に落ちる。その形（開く山と、閉じる下り坂）を作る。

    息は、口の中で作られる高い方の雑音なので、低い方は混ぜない。"""
    x = np.zeros(n + 512, dtype=np.float32)
    t = 0.0
    while t < n:
        T = SR / (f0 * (1.0 + rng.normal(0, jitter)))
        T = max(10.0, T)
        op = int(T * 0.44)                    # 開いていく長さ
        cl = int(T * 0.18)                    # 閉じる長さ（ここが急なほど明るい）
        i0 = int(t)
        if op > 1:
            a = np.arange(op) / op
            x[i0:i0 + op] += (0.5 - 0.5 * np.cos(np.pi * a)).astype(np.float32)
        if cl > 1:
            b = np.arange(cl) / cl
            x[i0 + op:i0 + op + cl] += np.cos(0.5 * np.pi * b).astype(np.float32)
        t += T
    x = x[:n]
    if breath:
        nz = rng.normal(0, 1, n).astype(np.float32)
        nz = lfilter([1.0, -0.97], [1.0], nz)  # 低い方を落とす（息は高い成分）
        x = x * (1 - breath * 0.35) + nz * breath * 0.12
    return x


def mora(con, vow, dur, f0, size, breath, jitter, rng):
    kind, locus = C.get(con, ("none", None))
    vt = tuple(f * size for f in V.get(vow, V["u"]))
    if vow == "n":
        vt = tuple(f * size for f in (300, 1000, 2200))
    pre = []
    if kind in ("stop", "vstop", "aff"):
        pre.append(np.zeros(int(0.045 * SR), dtype=np.float32))
    if kind in ("stop", "vstop"):
        b = rng.normal(0, 1, int(0.012 * SR)).astype(np.float32)
        pre.append(reson(b, tuple(f * size for f in locus)) * (0.5 if kind == "stop" else 0.3))
    if kind in ("fric", "aff"):
        nz = rng.normal(0, 1, int(0.07 * SR)).astype(np.float32)
        pre.append(reson(nz, tuple(f * size for f in locus)) * 0.35)
    if kind == "nasal":
        n0 = int(0.05 * SR)
        s0 = glottal(n0, f0, jitter, breath, rng)
        pre.append(reson(s0, tuple(f * size for f in locus)) * 0.5)
    if kind == "flap":
        pre.append(np.zeros(int(0.018 * SR), dtype=np.float32))

    n = int(dur * SR)
    s = glottal(n, f0, jitter, breath, rng)
    if kind in ("glide", "flap", "vstop", "nasal") and locus:
        # 子音の場所から母音へ滑らせる。ここが言葉らしさの正体
        g = int(min(n, 0.045 * SR))
        head = np.zeros(g, dtype=np.float32)
        for j in range(g):
            a = j / max(1, g - 1)
            f = tuple(lo * size * (1 - a) + vv * a for lo, vv in zip(locus, vt))
            head[j] = reson(s[j:j + 1], f)[0]
        body = reson(s[g:], vt)
        y = np.concatenate([head, body])
    else:
        y = reson(s, vt)
    e = np.ones(len(y), dtype=np.float32)
    a, r = int(0.012 * SR), int(0.03 * SR)
    e[:a] = np.linspace(0, 1, a)
    e[-r:] = np.linspace(1, 0, r)
    y = y * e
    # 唇から外へ出るときの変化（高い方が少し持ち上がる）
    y = lfilter([1.0, -0.94], [1.0], y).astype(np.float32)
    return np.concatenate(pre + [y]) if pre else y


def speak(text, f0=300, size=1.35, breath=0.20, jitter=0.010, seed=0,
          contour=None):
    rng = np.random.default_rng(seed)
    items = parse(text)
    voiced = [x for x in items if x[0] not in ("pause", "sokuon")]
    m = len(voiced) or 1
    out, k = [], 0
    for con, vow, dur in items:
        if con == "pause":
            out.append(np.zeros(int(dur * SR), dtype=np.float32))
            continue
        if con == "sokuon":
            out.append(np.zeros(int(dur * SR), dtype=np.float32))
            continue
        a = k / max(1, m - 1)
        f = f0 * (contour(a) if contour else (1.0 + 0.10 * np.sin(np.pi * a)))
        out.append(mora(con, vow, dur, f, size, breath, jitter, rng))
        k += 1
    y = np.concatenate(out)
    return y / (np.abs(y).max() + 1e-9) * 0.8


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()
    rep = io.open(HERE / "kana_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    lines = [
        ("1_なのり",   "あのね、わたし、きっちんちゃん。", dict(f0=300, size=1.30)),
        ("2_小さめ",   "あのね、わたし、きっちんちゃん。", dict(f0=360, size=1.45)),
        ("3_息おおめ", "あのね、わたし、きっちんちゃん。", dict(f0=300, size=1.30, breath=0.30)),
        ("4_あいさつ", "あ、きた。", dict(f0=310, size=1.30)),
        ("5_しらせ",   "あのね、さっきね、だれかがね、きれいにしてくれたのかなあ。",
                       dict(f0=295, size=1.30)),
        ("6_そわそわ", "なんだかね、そわそわするなあ。", dict(f0=290, size=1.30)),
    ]
    for i, (name, text, kw) in enumerate(lines):
        y = speak(text, seed=i, **kw)
        sf.write(str(OUT / (name + ".wav")), y, SR)
        say("%-12s 「%s」" % (name, text))
    say("")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
