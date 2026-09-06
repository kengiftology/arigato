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

--- プチプチを消した経緯（2026-09-06）---
かなを1文字ずつ別々に作って、あとから貼り合わせていた。
貼り目で3つのことが同時に起きていた。

  1) 声帯の波が、文字ごとに頭からやり直しになる（波形が飛ぶ）
  2) 共鳴を通す計算の「余韻」が、文字の終わりで切り捨てられる
  3) 破裂や摩擦の雑音に、頭と尻の始末がついていない

耳はこの段差を「プチッ」と聞く。雑音ではないので、ノイズ除去では消えない
（段差は残り、まわりの声のほうが削られる）。実測 1秒あたり3000箇所。

そこで、文をまるごと1本の音として作る作りに変えた。
声帯の波は文の頭から終わりまで途切れず進み、共鳴は余韻を持ったまま
少しずつ位置を変えていく。人の口も、そうやって動いている。
"""
import io
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import lfilter, lfilter_zi

HERE = Path(__file__).parent
OUT = HERE / "work" / "kana"
SR = 24000

# 良いと言われた声（ずんだもん・後鬼・四国めたん）から測った値に寄せる。
# 3人の平均は 高さ337Hz / 共鳴 650・1702・2816・3569Hz。
# 以前は共鳴1を1040Hz相当に置いていて60%高すぎ、それがこもりの正体だった。
V = {"a": (700, 1300, 2700), "i": (330, 2400, 3100), "u": (380, 1250, 2500),
     "e": (520, 1900, 2750), "o": (500, 1000, 2650)}
F4, F5 = 3569, 4600                    # 上の共鳴。明るさを支える（実測より）

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
            i += 1
        out.append((con, vow, 0.15))
        i += 1
    return out


# 共鳴の幅（Hz）。狭いほど母音がはっきりする。広すぎると谷が埋まってこもる
BW = (60, 90, 120, 150, 200)
BLOCK = 96                             # 共鳴の位置を測り直す間隔（4ミリ秒）


def _smooth(x, ms):
    """角を取る。段差をそのまま渡すと、そこがプチッと鳴る。

    出だしは最初の値から始める。0から始めると、文の頭で声の高さが
    下から滑り上がってしまう。"""
    c = float(np.exp(-1.0 / (ms * 0.001 * SR)))
    b, a = [1.0 - c], [1.0, -c]
    zi = lfilter_zi(b, a) * float(x[0])
    return lfilter(b, a, x, zi=zi)[0]


def cascade(x, tracks):
    """喉と口の共鳴を、余韻を保ったまま通す。

    以前は、かな1文字ぶんを通しては次の文字で最初からやり直していた。
    計算の途中の値（＝余韻）が文字の変わり目で毎回捨てられ、そこで
    音が途切れていた。ここでは文の頭から終わりまで値を持ち越す。

    共鳴の位置も、1標本ずつ動かす。4ミリ秒ごとにまとめて切り替えると、
    切り替えのたびに小さな衝撃が出て、それが毎秒250回のプチプチになる
    （実測でそうなった）。少しずつ動かせば衝撃は出ない。

    直列（1本の管）に通す。並べて足すと山のあいだの谷が埋まり、
    こもったのにざらつく音になる。"""
    y = np.asarray(x, dtype=np.float64)
    n = len(y)
    for track, bwv in zip(tracks, BW):
        f = np.clip(np.asarray(track, dtype=np.float64), 90.0, SR / 2 - 200.0)
        r = float(np.exp(-np.pi * bwv / SR))
        c = 2.0 * r * np.cos(2 * np.pi * f / SR)
        a1 = -c
        a2 = r * r
        k = 1.0 - c + r * r                    # 直流で利得1になるようにする
        out = np.empty(n)
        y1 = y2 = 0.0
        for i in range(n):
            v = k[i] * y[i] - a1[i] * y1 - a2 * y2
            out[i] = v
            y2 = y1
            y1 = v
        y = out
    return y


def plan(text, size):
    """文を、時間の流れに沿った区間の並びに直す。

    区間 = (長さ, 種類, 共鳴の位置, 声の強さ, 息の強さ)
    種類は 声 / 雑音 / 沈黙。"""
    segs = []
    for con, vow, dur in parse(text):
        if con in ("pause", "sokuon"):
            segs.append((dur, "sil", None, 0.0, 0.0))
            continue
        kind, locus = C.get(con, ("none", None))
        vt = V.get(vow, V["u"]) if vow != "n" else (300, 1000, 2200)
        vt = tuple(f * size for f in vt)
        lo = tuple(f * size for f in locus) if locus else vt
        if kind in ("stop", "vstop", "aff"):
            segs.append((0.045, "sil", lo, 0.0, 0.0))          # 口を閉じる
        if kind in ("stop", "vstop"):
            segs.append((0.012, "noise", lo, 0.0,
                         0.50 if kind == "stop" else 0.30))    # 破裂
        if kind in ("fric", "aff"):
            segs.append((0.070, "noise", lo, 0.0, 0.35))       # 摩擦
        if kind == "nasal":
            segs.append((0.050, "voice", lo, 0.50, 0.0))       # 鼻の共鳴
        if kind == "flap":
            segs.append((0.018, "sil", lo, 0.0, 0.0))          # はじき
        segs.append((dur, "voice", vt, 1.0, 0.0))
    if not segs:
        segs = [(0.2, "sil", None, 0.0, 0.0)]
    return segs


def speak(text, f0=300, size=1.35, breath=0.20, jitter=0.010, seed=0,
          contour=None):
    """文をまるごと1本の音として作る。継ぎ目がないので段差が出ない。"""
    rng = np.random.default_rng(seed)
    segs = plan(text, size)
    lens = [max(1, int(d * SR)) for d, *_ in segs]
    n = sum(lens)

    # --- 共鳴の位置・声の強さ・息の強さを、時間の帯として並べる ---
    tr = np.zeros((5, n))
    vg = np.zeros(n)
    ng = np.zeros(n)
    last = None
    pos = 0
    for (d, kind, ff, v, b), L in zip(segs, lens):
        ff = ff or last or tuple(f * size for f in V["u"])
        last = ff
        tr[0, pos:pos + L] = ff[0]
        tr[1, pos:pos + L] = ff[1]
        tr[2, pos:pos + L] = ff[2]
        vg[pos:pos + L] = v
        ng[pos:pos + L] = b
        pos += L
    tr[3, :] = F4 * size
    tr[4, :] = F5 * size
    # 12ミリ秒でなめらかに移す。これが子音から母音への滑り（言葉らしさ）になる
    for i in range(3):
        tr[i] = _smooth(tr[i], 12.0)
    vg = _smooth(vg, 6.0)
    ng = _smooth(ng, 4.0)

    # --- 声の高さ。文の頭から終わりまで1本の線でつなぐ ---
    marks, pos = [], 0
    for (d, kind, ff, v, b), L in zip(segs, lens):
        if kind == "voice" and v >= 1.0:
            marks.append(pos + L // 2)
        pos += L
    if len(marks) < 2:
        marks = [0, n - 1]
    a = np.linspace(0.0, 1.0, len(marks))
    vals = np.array([contour(x) if contour else (1.0 + 0.10 * np.sin(np.pi * x))
                     for x in a])
    f0line = np.interp(np.arange(n), marks, vals * f0)
    f0line = _smooth(f0line, 25.0)
    # 声の揺れ。ゆっくり揺らす。1標本ごとに散らすとザラつきになる
    wob = _smooth(rng.normal(0, jitter, n), 18.0)
    f0line = f0line * (1.0 + wob * 8.0)
    f0line = np.clip(f0line, 60.0, 900.0)

    # --- 声帯。位相を積み上げるので、文の途中で頭から始まり直さない ---
    ph = np.cumsum(f0line / SR)
    ph = ph - np.floor(ph)
    src = np.zeros(n)
    op, cl = 0.44, 0.18                    # 開く長さ / 閉じる長さ
    m1 = ph < op
    src[m1] = 0.5 - 0.5 * np.cos(np.pi * ph[m1] / op)
    m2 = (ph >= op) & (ph < op + cl)
    src[m2] = np.cos(0.5 * np.pi * (ph[m2] - op) / cl)

    # --- 息と雑音。高い成分だけ（低い方は口の中では作られない） ---
    nz = lfilter([1.0, -0.97], [1.0], rng.normal(0, 1, n))

    x = (src * (1.0 - breath * 0.35) + nz * breath * 0.12) * vg + nz * ng
    y = cascade(x, tr)
    y = lfilter([1.0, -0.94], [1.0], y)    # 唇から外へ出るときの変化

    e = np.ones(n)                         # 文の頭と尻だけ、そっと始めて終わる
    k = min(int(0.015 * SR), n // 2)
    e[:k] = np.linspace(0, 1, k)
    e[-k:] = np.linspace(1, 0, k)
    y = y * e
    return (y / (np.abs(y).max() + 1e-9) * 0.8).astype(np.float32)


LINES = [
    ("1_なのり",   "あのね、わたし、きっちんちゃん。", dict(f0=300, size=1.30)),
    ("2_小さめ",   "あのね、わたし、きっちんちゃん。", dict(f0=360, size=1.45)),
    ("3_息おおめ", "あのね、わたし、きっちんちゃん。", dict(f0=300, size=1.30, breath=0.30)),
    ("4_あいさつ", "あ、きた。", dict(f0=310, size=1.30)),
    ("5_しらせ",   "あのね、さっきね、だれかがね、きれいにしてくれたのかなあ。",
                   dict(f0=295, size=1.30)),
    ("6_そわそわ", "なんだかね、そわそわするなあ。", dict(f0=290, size=1.30)),
]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()
    rep = io.open(HERE / "kana_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    for i, (name, text, kw) in enumerate(LINES):
        y = speak(text, seed=i, **kw)
        sf.write(str(OUT / (name + ".wav")), y, SR)
        d = np.abs(np.diff(y.astype(np.float64)))
        # 音が出ているところの差を基準に、そこから飛び抜けた分だけ数える
        jumps = int((d > (np.percentile(d, 90) + 1e-12) * 6).sum())
        say("%-12s 段差 %5d 箇所（%.0f/秒）  「%s」"
            % (name, jumps, jumps / (len(y) / SR), text))
    say("")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
