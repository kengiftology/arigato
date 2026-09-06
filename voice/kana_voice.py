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
  3) 共鳴の位置を4ミリ秒ごとにまとめて切り替えていた

耳はこの段差を「プチッ」と聞く。雑音ではないので、ノイズ除去では消えない。
文をまるごと1本の音として作る作りに変えた。1秒あたり16.4→0.5箇所。

--- ぎこちなさを取った経緯（2026-09-06）---
音は綺麗になったが、喋り方が機械のままだった。
良いと言われた声（ずんだもん・四国めたん・後鬼）の読み方を測って
自分の作りと突き合わせたら、4つずれていた。

  拍の長さ  実際は 0.057〜0.336 秒（ばらつき0.055）／私は全部 0.150 秒
  抑揚      実際は句のなかで1.5倍動く／私は文全体でひとつの山（±10%）
  間        実際は 0.33〜0.62 秒／私は 0.16 秒
  無声化    し・っ は息だけになる／私は全部を声にしていた

とくに拍の長さ。全部同じ長さで並べると、耳はそれを言葉ではなく
拍子として聞く。人は句の終わりの拍を1.7倍にのばし、詰まる音を半分に縮める。

もうひとつ、子音を拍の外に足していた。子音つきの拍だけ4割長くなり、
拍の並びががたついていた。子音の時間は拍の中から取るように直した。
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
# さらに上。これが無いと6kHzより上が空になり、息の音が本物と別物になる
# （実測：私の し は6kHz以上が−60dB、VOICEVOXは−10dB）
F6, F7 = 6800, 8300

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
# 子音の息の音そのものの形（中心Hz, 幅Hz）。
# 以前は母音と同じ共鳴に通していたので、し が 2〜4kHz に落ちていた。
# 本物の し は 5〜7kHz に山が立つ（VOICEVOX実測）。まったく別の音だった。
NOISE = {
    "s": (6500, 3500), "z": (6000, 3500),
    "sh": (5000, 3500), "ch": (5000, 3500), "j": (5000, 3500),
    "ts": (6000, 3000),
    "t": (4000, 3000), "d": (4000, 3000),
    "k": (3000, 2500), "g": (3000, 2500),
    "p": (1200, 1800), "b": (1200, 1800),
    "h": (1600, 2500), "f": (2500, 2500),
}

# 声帯を使わない子音。これにはさまれた い・う は息だけになる（無声化）
VOICELESS = {"k", "t", "p", "s", "h", "f", "sh", "ch", "ts"}

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

# 実測にもとづく拍の長さ（秒）。3人の平均は 0.156・ばらつき 0.055
BASE = 0.135          # ふつうの拍
LAST = 1.70           # 句の終わりの拍はのびる（実測 0.254 対 0.139）
SOKUON = 0.075        # 詰まる音（実測 0.058〜0.111）
HATSUON = 0.100       # ん（実測 0.082〜0.110）
PAUSE_C, PAUSE_P = 0.36, 0.30   # 、 と 。 の間（実測 0.33〜0.62）


def parse(text):
    """かなを、子音と母音の組に分ける。ー は伸ばし、っ は詰まり。"""
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch in "、。 　":
            out.append(("pause", None, PAUSE_C if ch == "、" else PAUSE_P))
            i += 1
            continue
        if ch == "っ":
            out.append(("sokuon", None, SOKUON))
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
        out.append((con, vow, HATSUON if vow == "n" else BASE))
        i += 1
    return out


def prosody(items, rng):
    """どう読むかを決める。長さ・高さ・無声化。

    句（、で区切られたまとまり）ごとに形をつける。
      終わりでない句 … 上がって終わる（まだ続くよ、の合図）
      終わりの句     … 二拍目で高くなり、そこから落ちる

    実測では句のなかで高さが1.5倍動いていた。私は±10%しか動かしておらず、
    それが棒読みの正体だった。"""
    phrases, cur = [], []
    for it in items:
        if it[0] == "pause":
            phrases.append((cur, it[2]))
            cur = []
        else:
            cur.append(it)
    phrases.append((cur, 0.0))
    phrases = [(p, g) for p, g in phrases if p or g]

    out = []
    nph = sum(1 for p, _ in phrases if p)
    ip = 0
    for p, gap in phrases:
        voiced_idx = [j for j, it in enumerate(p) if it[0] != "sokuon"]
        k = len(voiced_idx)
        last_phrase = (ip == nph - 1)
        drift = 0.97 ** ip                       # 句を追うごとに少しずつ下がる
        for j, it in enumerate(p):
            con, vow, dur = it
            if con == "sokuon":
                out.append(("sokuon", None, dur, None, False, 0.0))
                continue
            r = voiced_idx.index(j)
            d = dur * (LAST if r == k - 1 else 1.0)   # 句の終わりはのばす
            d *= 1.0 + rng.normal(0, 0.06)            # 毎回きっかり同じにはならない
            a = r / max(1, k - 1)
            if last_phrase:
                if k >= 3:
                    pit = float(np.interp(r, [0, 1, k - 1], [1.00, 1.28, 0.72]))
                elif k == 2:
                    pit = [1.10, 0.78][r]
                else:
                    pit = 0.95
            else:
                pit = 0.88 + 0.44 * a                 # 上がって終わる
            pit *= drift * (1.0 + rng.normal(0, 0.015))
            nxt = p[j + 1] if j + 1 < len(p) else None
            # 無声化。句の頭では起きない（実測：「きた」のキは声のままだった）
            dev = (r > 0 and vow in ("i", "u") and con in VOICELESS
                   and (nxt is None or nxt[0] in VOICELESS))
            # 強さ。ひと息のなかで少しずつ弱くなる。全部同じ強さで並べると、
            # 高さと長さを直しても、まだ打鍵のように聞こえる
            amp = (1.0 - 0.22 * a) if last_phrase else (1.0 - 0.08 * a)
            amp *= 1.0 + rng.normal(0, 0.04)
            out.append((con, vow, float(d), float(pit), bool(dev), float(amp)))
        if gap:
            out.append(("pause", None, gap, None, False, 0.0))
        if p:
            ip += 1
    return out


# 共鳴の幅（Hz）。狭いほど母音がはっきりする。広すぎると谷が埋まってこもる
BW = (60, 90, 120, 150, 200, 260, 320)


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


# 子音にかかる時間。拍の長さのうち、これを引いた残りが母音になる
CTIME = {"stop": 0.052, "vstop": 0.052, "aff": 0.085, "fric": 0.070,
         "nasal": 0.050, "flap": 0.018, "glide": 0.0, "none": 0.0}


def plan(items, size):
    """読み方を、時間の流れに沿った区間の並びに直す。

    区間 = (長さ, 種類, 共鳴の位置, 声の強さ, 息の強さ, 高さ, 息の音の形, 鼻)
    種類は 声 / 雑音 / 沈黙。

    息の音（息の音の形）は母音の共鳴とは別に持つ。息はすき間の前の
    短い空洞で鳴っていて、口ぜんたいの共鳴を通らないため。"""
    segs = []
    for con, vow, dur, pit, dev, amp in items:
        if con in ("pause", "sokuon"):
            segs.append((dur, "sil", None, 0.0, 0.0, None, None, 0.0))
            continue
        kind, locus = C.get(con, ("none", None))
        vt = V.get(vow, V["u"]) if vow != "n" else (300, 1000, 2200)
        vt = tuple(f * size for f in vt)
        lo = tuple(f * size for f in locus) if locus else vt
        ns = NOISE.get(con)
        ns = (ns[0] * size, ns[1]) if ns else (3000 * size, 3000)
        vowel = max(0.045, dur - CTIME.get(kind, 0.0))
        if kind in ("stop", "vstop", "aff"):
            segs.append((0.040, "sil", lo, 0.0, 0.0, None, ns, 0.0))  # 口を閉じる
        if kind in ("stop", "vstop"):
            segs.append((0.012, "noise", lo, 0.0,
                         0.50 if kind == "stop" else 0.30, None, ns, 0.0))
            if kind == "stop":
                # 破裂のあと、声が出るまでの息だけの時間。
                # ここを飛ばして破裂の直後から声を出すと、機械のように
                # 切り替わって聞こえる。人は 20〜40ミリ秒かけて声に移る
                segs.append((0.028, "noise", lo, 0.0, 0.16, None, ns, 0.0))
                vowel = max(0.045, vowel - 0.028)
        if kind in ("fric", "aff"):
            segs.append((0.070 if kind == "fric" else 0.045, "noise",
                         lo, 0.0, 0.35, None, ns, 0.0))            # 摩擦
        if kind == "nasal":
            segs.append((0.050, "voice", lo, 0.50 * amp, 0.0, pit, ns, 1.0))
        if kind == "flap":
            segs.append((0.018, "sil", lo, 0.0, 0.0, None, ns, 0.0))  # はじき
        if dev:
            # 無声化。声帯を使わず、子音の息がそのまま続く
            segs.append((vowel * 0.7, "noise", lo, 0.0, 0.14 * amp, None, ns, 0.0))
        else:
            segs.append((vowel, "voice", vt, amp, 0.0, pit, ns,
                     1.0 if vow == "n" else 0.0))
    if not segs:
        segs = [(0.2, "sil", None, 0.0, 0.0, None, None, 0.0)]
    return segs


def lowpass(x, cutoff):
    """高い成分を落とす。切る高さを1標本ずつ動かせる。"""
    n = len(x)
    c = np.exp(-2.0 * np.pi * np.clip(np.asarray(cutoff, dtype=float),
                                      300.0, SR / 2 - 200.0) / SR)
    out = np.empty(n)
    xin = np.asarray(x, dtype=float)
    y1 = 0.0
    for i in range(n):
        y1 = (1.0 - c[i]) * xin[i] + c[i] * y1
        out[i] = y1
    return out


def bandpass(x, cf, bw):
    """息の音を、その音の形に整える。中心と幅は1標本ずつ動かす。"""
    n = len(x)
    f = np.clip(np.asarray(cf, dtype=float), 200.0, SR / 2 - 300.0)
    b = np.clip(np.asarray(bw, dtype=float), 300.0, 6000.0)
    r = np.exp(-np.pi * b / SR)
    c = 2.0 * r * np.cos(2 * np.pi * f / SR)
    g = (1.0 - r * r)                          # 山の高さをそろえる
    out = np.empty(n)
    y1 = y2 = 0.0
    xin = np.asarray(x, dtype=float)
    for i in range(n):
        v = g[i] * xin[i] + c[i] * y1 - r[i] * r[i] * y2
        out[i] = v
        y2 = y1
        y1 = v
    return out


def speak(text, f0=300, size=1.35, breath=0.20, jitter=0.010, seed=0,
          contour=None):
    """文をまるごと1本の音として作る。継ぎ目がないので段差が出ない。"""
    rng = np.random.default_rng(seed)
    items = prosody(parse(text), rng)
    segs = plan(items, size)
    lens = [max(1, int(d * SR)) for d, *_ in segs]
    n = sum(lens)

    # --- 共鳴の位置・声の強さ・息の強さを、時間の帯として並べる ---
    tr = np.zeros((7, n))
    vg = np.zeros(n)
    ng = np.zeros(n)
    ncf = np.zeros(n)
    nbw = np.zeros(n)
    dark = np.zeros(n)
    marks, vals = [], []
    last = None
    pos = 0
    # 口の形は、区間ごとに置いて塗るのではなく、点を打って線でつなぐ。
    #
    # 塗っていた頃は、母音のあいだ形がぴたりと止まっていた。実測すると
    # 「まったく動かない時間」が全体の23%（肉声は2%、VOICEVOXも2%）。
    # 人の口は喋っているあいだ止まらない。この止まりがロボット感だった。
    #
    # 点は、子音の場所と母音の中心に打つ。あいだは滑らかにつなぐので、
    # 短い拍では母音の形に届ききらない。これは人でも起きること（言い崩し）。
    fa, fv = [], []
    lastn = (3000.0 * size, 3000.0)
    for (d, kind, ff, v, b, pit, ns, dk), L in zip(segs, lens):
        ff = ff or last or tuple(f * size for f in V["u"])
        last = ff
        ns = ns or lastn
        lastn = ns
        fa.append(pos + L // 2)
        fv.append(ff)
        vg[pos:pos + L] = v
        ng[pos:pos + L] = b
        ncf[pos:pos + L] = ns[0]
        nbw[pos:pos + L] = ns[1]
        dark[pos:pos + L] = dk
        if pit is not None:
            marks.append(pos + L // 2)
            vals.append(pit)
        pos += L
    if len(fa) < 2:
        fa, fv = [0, n - 1], [fv[0] if fv else tuple(f * size for f in V["u"])] * 2
    idx = np.arange(n)
    for i in range(3):
        tr[i] = np.interp(idx, fa, [f[i] for f in fv])
    tr[3, :] = F4 * size
    tr[4, :] = F5 * size
    tr[5, :] = F6 * size
    tr[6, :] = F7 * size
    for i in range(3):
        tr[i] = _smooth(tr[i], 8.0)
    # 揺らぎ。人の口は狙った形にぴたりと静止しない。
    # ゆっくりのものだけだと、20〜50ミリ秒の「止まり」が残った（実測）。
    # 速い揺らぎを重ねて、止まる時間そのものをなくす。
    for i in range(7):
        slow = _smooth(rng.normal(0, 0.012, n), 90.0) * 6.0
        fast = _smooth(rng.normal(0, 0.010, n), 30.0) * 3.5
        tr[i] = tr[i] * (1.0 + slow + fast)
    vg = _smooth(vg, 6.0)
    ng = _smooth(ng, 4.0)
    # 息の強さも一定ではない。1拍のなかでもわずかに上下する
    vg = vg * (1.0 + _smooth(rng.normal(0, 0.05, n), 70.0) * 5.0)
    ncf = _smooth(ncf, 8.0)
    nbw = _smooth(nbw, 8.0)
    dark = _smooth(dark, 15.0)

    # --- 声の高さ。文の頭から終わりまで1本の線でつなぐ ---
    if len(marks) < 2:
        marks, vals = [0, n - 1], [1.0, 0.95]
    if contour:                                  # 呼び出し側が形を指定したとき
        vals = [contour(x) for x in np.linspace(0.0, 1.0, len(marks))]
    f0line = np.interp(np.arange(n), marks, np.asarray(vals, dtype=float) * f0)
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
    white = rng.normal(0, 1, n)                  # 息の音のもと（そのまま）
    nz = lfilter([1.0, -0.97], [1.0], white)     # 声にまぜる息（高い方だけ）

    # 声は口ぜんたいの共鳴を通る。息の音は通らない（すき間の前の空洞で鳴る）。
    # 以前は両方を同じ共鳴に通していた。そのせいで し が2〜4kHzに落ち、
    # 6kHzより上が空になっていた（実測−60dB、本物は−10dB）。別の道にする。
    v = (src * (1.0 - breath * 0.35) + nz * breath * 0.12) * vg
    y = cascade(v, tr)
    # 鼻に抜ける音（ん・な・ま）は暗い。鼻の中で高い成分が吸われるため。
    # 実測：本物の ん は4kHzで−56dB、私は−21dBだった（35dBも明るかった）
    # 1段だけでは足りなかった。1段は1オクターブで6dBしか落ちず、
    # 4kHzがまだ−6dBしか下がらない（実測でも ん が明るいままだった）。
    # 3段重ねて、1オクターブ18dB落とす
    cut = 9000.0 - 7000.0 * np.clip(dark, 0, 1)
    for _ in range(3):
        y = lowpass(y, cut)
    # 息の音は、白い雑音を2段の帯域で削る。1段だと上が落ちきらず、
    # 8〜9kHzまで同じ強さで残って砂を撒いたように聞こえた（実測）
    fr = bandpass(bandpass(white * ng, ncf, nbw), ncf, nbw)
    y = y + fr * 0.32                      # VOICEVOX と同じ明るさになる大きさ
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
        it = prosody(parse(text), np.random.default_rng(i))
        ds = [t[2] for t in it if t[0] not in ("pause", "sokuon")]
        dev = sum(1 for t in it if t[4])
        say("%-12s 段差%4d  拍のばらつき%.3f  無声化%d  「%s」"
            % (name, jumps, float(np.std(ds)), dev, text))
    say("")
    say("実測（ずんだもん・四国めたん・後鬼）の拍のばらつきは 0.055")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
