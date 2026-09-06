# -*- coding: utf-8 -*-
"""良いと言われた声から、特徴を数値で取り出す（2026-09-06）。

ゼロから組んだ声が粗かったのは、方式ではなく数値を当てずっぽうで
置いていたから（体の大きさ1.35倍・息0.2などに根拠がない）。
実際の声から測れば、根拠のある値になる。

測るもの:
  声の高さ      … 話す声の中心
  共鳴の位置    … 喉と口の大きさ。母音ごとに測って平均する
  高い方の落ち  … 声の明るさ。息っぽさにも効く
  ゆれ          … 生きもの感
"""
import glob
import io
import os
from pathlib import Path

import numpy as np
import soundfile as sf
import librosa
from scipy.signal import lfilter

HERE = Path(__file__).parent
SRC = HERE / "work" / "vvmix"


def formants(y, sr, n=5):
    """共鳴の位置を推定する。声の中の響きの山を探す。"""
    y = lfilter([1, -0.97], [1], y)                 # 高い方を持ち上げてから見る
    w = y * np.hamming(len(y))
    order = int(2 + sr / 1000)
    a = librosa.lpc(w.astype(np.float64), order=order)
    r = np.roots(a)
    r = r[np.imag(r) > 0]
    f = np.sort(np.arctan2(np.imag(r), np.real(r)) * sr / (2 * np.pi))
    bw = -0.5 * (sr / (2 * np.pi)) * np.log(np.abs(r))
    keep = [(fi, bi) for fi, bi in zip(f, bw[np.argsort(
        np.arctan2(np.imag(r), np.real(r)))]) if 200 < fi < sr / 2 - 500 and bi < 500]
    return [k[0] for k in keep[:n]], [k[1] for k in keep[:n]]


def tilt(y, sr):
    S = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    fr = np.fft.rfftfreq(len(y), 1 / sr)
    lo = S[(fr > 200) & (fr < 1000)].mean()
    hi = S[(fr > 3000) & (fr < 6000)].mean()
    return 20 * np.log10((hi + 1e-9) / (lo + 1e-9))


def jitter(y, sr):
    f = librosa.yin(y, fmin=80, fmax=600, sr=sr)
    f = f[np.isfinite(f)]
    if len(f) < 8:
        return 0.0
    p = 1.0 / f
    return float(np.mean(np.abs(np.diff(p))) / np.mean(p))


def main():
    o = io.open(HERE / "extract_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        o.write(s + chr(10))
        o.flush()

    files = sorted(glob.glob(str(SRC / "style_*.wav")))
    say("%-28s %6s %6s %6s %6s %6s %7s %6s"
        % ("声", "高さ", "共鳴1", "共鳴2", "共鳴3", "共鳴4", "明るさ", "ゆれ"))
    rows = []
    for p in files:
        y, sr = sf.read(p)
        if y.ndim > 1:
            y = y.mean(axis=1)
        y = y.astype(np.float32)
        y = y[np.abs(y) > 0.01]                     # 無音を落とす
        if len(y) < sr // 2:
            continue
        f0 = librosa.yin(y, fmin=80, fmax=600, sr=sr)
        f0 = float(np.median(f0[np.isfinite(f0)]))
        # 母音が続いている強い部分から共鳴を測る
        win = int(0.04 * sr)
        best, bf = None, None
        for i in range(0, len(y) - win, win // 2):
            seg = y[i:i + win]
            if np.abs(seg).mean() < np.abs(y).mean():
                continue
            try:
                ff, _bw = formants(seg, sr)
            except Exception:
                continue
            if len(ff) >= 4 and (best is None or np.abs(seg).mean() > best):
                best, bf = np.abs(seg).mean(), ff
        if bf is None:
            continue
        name = os.path.basename(p)[6:-4]
        rows.append((name, f0, bf, tilt(y, sr), jitter(y, sr)))
        say("%-28s %6.0f %6.0f %6.0f %6.0f %6.0f %7.1f %6.3f"
            % (name, f0, bf[0], bf[1], bf[2], bf[3] if len(bf) > 3 else 0,
               tilt(y, sr), jitter(y, sr)))

    good = [r for r in rows if r[0].split("_")[0] in ("ずんだもん", "後鬼", "四国めたん")]
    if good:
        say("")
        say("良いと言われた3人の平均")
        say("  高さ   %5.0f Hz" % np.mean([r[1] for r in good]))
        for k in range(4):
            vals = [r[2][k] for r in good if len(r[2]) > k]
            if vals:
                say("  共鳴%d  %5.0f Hz" % (k + 1, np.mean(vals)))
        say("  明るさ %5.1f dB" % np.mean([r[3] for r in good]))
        say("  ゆれ   %5.3f" % np.mean([r[4] for r in good]))
        say("")
        say("参考 いま自分で置いていた値: 高さ300 / 共鳴 1040・1560・3640（大きさ1.30倍）")
    o.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
