# -*- coding: utf-8 -*-
"""同じ文を、私の声と VOICEVOX で作り、音の部品ごとに突き合わせる（2026-09-06）。

全体の数字（高さ・揺れ・明るさ）はもう合っている。それでもぎこちない。
なら、ずれているのは全体ではなく部品ではないか。

とくに疑っているのは3つ。

  さ・し … 私は「口の中の共鳴」で作っている。本物の し は 2〜4kHz、
           す は 5kHz より上に山が立つ、まったく別の形をしている。
  ん・な … 鼻に抜ける音には「谷」がある（反共鳴）。私は山しか作っていない。
  か・た … 破裂のあと、声が出るまでに息だけの時間がある（30〜40ミリ秒）。
           私は破裂の直後から声を出している。

「あのね、わたし、きっちんちゃん。」で、し・ん・き のところの
スペクトルを並べて、どこに山が立っているかを比べる。
"""
import io
import json
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

HERE = Path(__file__).parent
OUT = HERE / "work" / "parts"
API = "http://127.0.0.1:50021"
TEXT = "あのね、わたし、きっちんちゃん。"
SR = 24000


def vv(text, sid=3):
    u = API + "/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": sid})
    with urllib.request.urlopen(urllib.request.Request(u, method="POST"), timeout=30) as r:
        q = json.load(r)
    u2 = API + "/synthesis?" + urllib.parse.urlencode({"speaker": sid})
    req = urllib.request.Request(u2, method="POST",
                                 data=json.dumps(q).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        wav = r.read()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "vv.wav").write_bytes(wav)
    x, sr = sf.read(str(OUT / "vv.wav"))
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != SR:
        from math import gcd
        g = gcd(int(sr), SR)
        x = resample_poly(x, SR // g, int(sr) // g)
    # 拍の切れ目（秒）
    t = q.get("prePhonemeLength", 0.1)
    marks = []
    for ph in q["accent_phrases"]:
        for m in ph["moras"]:
            d = (m.get("consonant_length") or 0.0) + m["vowel_length"]
            marks.append((m["text"], t, t + d))
            t += d
        if ph.get("pause_mora"):
            t += ph["pause_mora"]["vowel_length"]
    return np.asarray(x, dtype=float), marks


def mine(text):
    import kana_voice as K
    y = K.speak(text, f0=300, size=1.30, seed=0)
    rng = np.random.default_rng(0)
    items = K.prosody(K.parse(text), rng)
    segs = K.plan(items, 1.30)
    marks, t = [], 0.0
    for seg in segs:
        d, kind = seg[0], seg[1]
        # 区間の役割で拾う。長さで拾うと、短い母音を鼻の音と取り違える
        role = kind
        if kind == "voice" and len(seg) > 7 and seg[7] >= 1.0:
            role = "nasal"
        elif kind == "noise" and d <= 0.02:
            role = "burst"
        elif kind == "noise" and d >= 0.04:
            role = "fric"
        marks.append((role, t, t + d))
        t += d
    return np.asarray(y, dtype=float), marks


def spec(x, a, b):
    i, j = int(a * SR), int(b * SR)
    w = x[i:j]
    if len(w) < 256:
        return None, None
    w = w * np.hanning(len(w))
    X = np.abs(np.fft.rfft(w, 4096)) ** 2
    f = np.fft.rfftfreq(4096, 1 / SR)
    X = X / (X.max() + 1e-20)
    return f, X


def peaks(f, X, top=3):
    """どこに山が立っているか。1kHz幅ごとの強さで見る。"""
    out = []
    for lo in range(0, 10000, 1000):
        m = (f >= lo) & (f < lo + 1000)
        out.append(float(10 * np.log10(X[m].mean() + 1e-20)))
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rep = io.open(HERE / "parts_result.txt", "w", encoding="utf-8")

    def say(s=""):
        rep.write(s + chr(10))

    xv, mv = vv(TEXT)
    xm, mm = mine(TEXT)
    sf.write(str(OUT / "mine.wav"), xm, SR)

    def band_row(label, f, X):
        v = peaks(f, X)
        say("  %-14s " % label + " ".join("%6.0f" % b for b in v))

    say("帯域ごとの強さ（dB・その部分の最大を0とする）")
    say("  %-14s " % "" + " ".join("%6s" % ("%dk" % k) for k in range(10)))
    say("")

    # VOICEVOX 側：シ・ン・キ を拍から拾う
    want = {"シ": "し（息の音）", "ン": "ん（鼻）", "キ": "き（破裂）"}
    say("■ VOICEVOX")
    for t, a, b in mv:
        if t in want:
            f, X = spec(xv, a, b)
            if f is not None:
                band_row(want[t], f, X)
                sf.write(str(OUT / ("vv_%s.wav" % t)), xv[int(a * SR):int(b * SR)], SR)
                del want[t]

    # 私の側：区間の種類から拾う。し＝無声化の雑音、ん＝鼻の声、き＝破裂の雑音
    say("")
    say("■ 私の声")
    got = set()
    for i, (kind, a, b) in enumerate(mm):
        if kind == "fric" and "し" not in got:
            f, X = spec(xm, a, b)
            if f is not None:
                band_row("し（息の音）", f, X)
                sf.write(str(OUT / "mine_shi.wav"), xm[int(a * SR):int(b * SR)], SR)
                got.add("し")
        if kind == "nasal" and "ん" not in got:
            f, X = spec(xm, a, b)
            if f is not None:
                band_row("ん（鼻）", f, X)
                got.add("ん")
        if kind == "burst" and "き" not in got:
            f, X = spec(xm, a, b)
            if f is not None:
                band_row("き（破裂）", f, X)
                got.add("き")
    say("")
    say("音の部品: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
