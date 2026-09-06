# -*- coding: utf-8 -*-
"""私の作った声と、本物の肉声は何が違うのか（2026-09-06）。

「ロボットっぽい」の中身を、数字にして並べる。
比べる相手は、同意をもらって預かっている実際の録音（people/）。

見るところ:

  高さ          … 声そのものの高さ
  細かい揺れ    … 1周期ごとの、ごくわずかな高さのふらつき（jitter）
  強さの揺れ    … 1周期ごとの、ごくわずかな大きさのふらつき（shimmer）
  帯域ごとの周期性
      声は「規則正しい振動」と「息の雑音」が混ざったもの。
      本物は、低いところは規則正しく、高くなるほど雑音が増える。
      作り物は上まで規則正しいままになりやすく、それが「ブザーっぽさ」になる。
  スペクトルの傾き … 高い成分がどれだけ残っているか

すべて16kHzに揃えてから測る（録音が16kHzのため）。
"""
import io
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt, resample_poly

HERE = Path(__file__).parent
SR = 16000
BANDS = [(60, 1000), (1000, 3000), (3000, 7000)]
WIN, HOP = int(0.04 * SR), int(0.01 * SR)


def load(p):
    x, sr = sf.read(str(p))
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != SR:
        from math import gcd
        g = gcd(int(sr), SR)
        x = resample_poly(x, SR // g, int(sr) // g)
    x = x - x.mean()
    m = np.abs(x).max()
    return (x / m if m > 0 else x).astype(np.float64)


def periods(x):
    """1周期ずつの、長さと大きさを取り出す。

    声帯が閉じる瞬間は波形が急に落ち込む。そこを1周期の境目として拾う。"""
    import librosa
    f0 = librosa.yin(x, fmin=70, fmax=700, sr=SR, frame_length=1024, hop_length=256)
    rms = librosa.feature.rms(y=x, frame_length=1024, hop_length=256)[0]
    ok = rms > rms.max() * 0.25
    if ok.sum() < 10:
        return None, None, None
    f = f0[ok]
    f = f[(f > 70) & (f < 700)]
    if len(f) < 10:
        return None, None, None
    # 周期の長さ（frame ごと）と、その差
    T = SR / f
    jit = float(np.mean(np.abs(np.diff(T))) / np.mean(T))
    # 大きさは、1周期ぶんの窓ごとの山の高さで測る
    n = int(SR / np.median(f))
    peaks = [np.abs(x[i:i + n]).max() for i in range(0, len(x) - n, n)]
    peaks = np.array([p for p in peaks if p > np.max(peaks) * 0.2])
    shim = float(np.mean(np.abs(np.diff(peaks))) / np.mean(peaks)) if len(peaks) > 5 else float("nan")
    return float(np.median(f)), jit, shim


def band_periodicity(x, f0):
    """帯域ごとに、どれだけ規則正しいか（1.0で完全に規則正しい）。"""
    out = []
    lag = int(SR / f0)
    for lo, hi in BANDS:
        sos = butter(4, [lo / (SR / 2), min(hi, SR / 2 - 100) / (SR / 2)],
                     btype="band", output="sos")
        b = sosfiltfilt(sos, x)
        vals = []
        for i in range(0, len(b) - WIN - lag, HOP):
            w = b[i:i + WIN]
            if np.abs(w).max() < np.abs(b).max() * 0.2:
                continue
            e = np.dot(w, w)
            if e <= 0:
                continue
            best = 0.0
            for L in range(int(lag * 0.9), int(lag * 1.1) + 1):
                w2 = b[i + L:i + L + WIN]
                if len(w2) < WIN:
                    break
                e2 = np.dot(w2, w2)
                if e2 <= 0:
                    continue
                best = max(best, float(np.dot(w, w2) / np.sqrt(e * e2)))
            vals.append(best)
        out.append(float(np.mean(vals)) if vals else float("nan"))
    return out


def tilt(x):
    """高いところが、低いところに比べてどれだけ残っているか（dB）。"""
    X = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / SR)
    lo = X[(f > 200) & (f < 1000)].mean()
    hi = X[(f > 3000) & (f < 6000)].mean()
    return float(10 * np.log10((hi + 1e-20) / (lo + 1e-20)))


def movement(x):
    """口の形が、どれだけ動き続けているか。

    人の口は喋っているあいだ止まらない。合成は、母音のあいだ形が
    ぴたりと止まりやすい。その「止まり」がロボット感になりうる。
    10ミリ秒ごとに音色の変化量を測り、平均と、ほとんど動かない時間の割合を返す。"""
    import librosa
    m = librosa.feature.mfcc(y=x.astype(np.float32), sr=SR, n_mfcc=13,
                             hop_length=HOP, n_fft=1024)
    rms = librosa.feature.rms(y=x.astype(np.float32), frame_length=1024,
                              hop_length=HOP)[0]
    ok = rms > rms.max() * 0.25
    ok = ok[:m.shape[1]]
    d = np.abs(np.diff(m, axis=1)).mean(axis=0)
    d = d[ok[1:len(d) + 1]] if ok[1:len(d) + 1].sum() > 5 else d
    if len(d) < 5:
        return float("nan"), float("nan")
    return float(np.mean(d)), float(np.mean(d < np.mean(d) * 0.35) * 100)


def measure(paths, label, say):
    f0s, jits, shims, bands, tilts, moves, stills = [], [], [], [], [], [], []
    for p in paths:
        x = load(p)
        if len(x) < SR * 0.3:
            continue
        f0, jit, shim = periods(x)
        if f0 is None:
            continue
        f0s.append(f0)
        jits.append(jit)
        if shim == shim:
            shims.append(shim)
        bands.append(band_periodicity(x, f0))
        tilts.append(tilt(x))
        mv, st = movement(x)
        moves.append(mv)
        stills.append(st)
    if not f0s:
        say("%s … 測れませんでした" % label)
        return None
    b = np.nanmean(np.array(bands), axis=0)
    r = dict(name=label, n=len(f0s), f0=np.mean(f0s), jit=np.mean(jits) * 100,
             shim=np.mean(shims) * 100 if shims else float("nan"),
             b0=b[0], b1=b[1], b2=b[2], tilt=np.mean(tilts),
             move=np.nanmean(moves), still=np.nanmean(stills))
    return r


def main():
    rep = io.open(HERE / "human_result.txt", "w", encoding="utf-8")

    def say(s=""):
        rep.write(s + chr(10))

    rows = []
    for d in sorted((HERE / "people").glob("p_*")):
        clips = sorted((d / "clips").glob("*.wav"))[:6]
        if clips:
            r = measure(clips, "肉声 " + d.name, say)
            if r:
                rows.append(r)
    # 良いと言われた声（合成）も並べる。これが無いと、私の数字が
    # 「肉声から遠い」のか「合成の声なら普通なのか」が区別できない
    vv = sorted((HERE / "work" / "vv").glob("*.wav"))[:6]
    if vv:
        r = measure(vv, "VOICEVOX（合成）", say)
        if r:
            rows.append(r)
    mine = sorted((HERE / "work" / "kana").glob("*.wav"))
    r = measure(mine, "私の声（ゼロから）", say)
    if r:
        rows.append(r)

    say("%-16s %6s %7s %7s %8s %8s %8s %8s %7s %8s"
        % ("", "高さHz", "細揺れ%", "強揺れ%", "周期0-1k", "1-3k", "3-7k",
           "傾きdB", "口の動き", "止まり%"))
    for r in rows:
        say("%-16s %6.0f %7.2f %7.2f %8.2f %8.2f %8.2f %8.1f %7.1f %8.1f"
            % (r["name"], r["f0"], r["jit"], r["shim"],
               r["b0"], r["b1"], r["b2"], r["tilt"], r["move"], r["still"]))

    hum = [r for r in rows if r["name"].startswith("肉声")]
    me = [r for r in rows if r["name"].startswith("私")]
    if hum and me:
        h = {k: np.mean([r[k] for r in hum])
             for k in ("jit", "shim", "b0", "b1", "b2", "tilt", "move", "still")}
        m = me[0]
        say("")
        say("=" * 62)
        say("%-16s %7s %7s %8s %8s %8s %8s %7s %8s"
            % ("", "細揺れ%", "強揺れ%", "周期0-1k", "1-3k", "3-7k", "傾きdB",
               "口の動き", "止まり%"))
        for lbl, v in (("肉声の平均", h), ("私の声", m)):
            say("%-16s %7.2f %7.2f %8.2f %8.2f %8.2f %8.1f %7.1f %8.1f"
                % (lbl, v["jit"], v["shim"], v["b0"], v["b1"], v["b2"],
                   v["tilt"], v["move"], v["still"]))
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
