# -*- coding: utf-8 -*-
"""声は4人のまま、読み方だけをずんだもんから写す（2026-09-07）。

「ただの大人の声をしたキャラは、さすがにおかしい」
声そのものは4人（Cは半分）でいく。かわいらしさは読み方で作る。

声を3つの部品に分けると、
  高さの動き・拍の長さ … 読み方（ここをずんだもんから）
  響きの形             … 声そのもの（ここは4人のまま）
  かすれ               … 4人のまま

手順:
  1) 同じ台詞を、4人の声（XTTS）とずんだもん（VOICEVOX）で作る
  2) 2本の時間を揃える（音色の流れを突き合わせて、どの瞬間がどの瞬間かを対応させる）
  3) ずんだもんの時間軸に、4人の響きを並べ直す   → 拍の長さが写る
  4) ずんだもんの高さの動きを、4人の高さの位置に平行移動して載せる → 抑揚が写る
  5) 響きの目盛りを縮める → 体が小さくなる

ずんだもんの音そのものは1サンプルも残らない（高さの線と拍の長さだけを使う）。
ただし読み方はずんだもんの音声ライブラリから得ているので、クレジットは付ける。
"""
import io
import json
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pyworld as pw
import soundfile as sf
from scipy.signal import resample_poly

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
OUT = HERE / "work" / "manner"
API = "http://127.0.0.1:50021"
SR = 24000
FP = 5.0                                    # 部品の刻み（ミリ秒）
HOP = int(SR * FP / 1000)

WEIGHTS = {"p_a": 1.0, "p_b": 1.0, "p_c": 0.5, "p_d": 1.0}   # 「重み_C半分」
LINES = [
    ("1_なのり",   "あのね、わたし、きっちんちゃん。"),
    ("4_そわそわ", "なんだかね、そわそわするなあ。"),
]


def wmean(vs, ws):
    ns = [np.linalg.norm(v) for v in vs]
    m = sum(w * v / (n + 1e-9) for v, n, w in zip(vs, ns, ws)) / sum(ws)
    return m / (np.linalg.norm(m) + 1e-9) * float(np.mean(ns))


def vv(text, sid=3):
    u = API + "/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": sid})
    with urllib.request.urlopen(urllib.request.Request(u, method="POST"), timeout=30) as r:
        q = json.load(r)
    u2 = API + "/synthesis?" + urllib.parse.urlencode({"speaker": sid})
    req = urllib.request.Request(u2, method="POST", data=json.dumps(q).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        p = OUT / "_vv.wav"
        p.write_bytes(r.read())
    x, sr = sf.read(str(p))
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != SR:
        from math import gcd
        g = gcd(int(sr), SR)
        x = resample_poly(x, SR // g, int(sr) // g)
    return np.asarray(x, dtype=np.float64)


def parts(x):
    f0, t = pw.harvest(x, SR, f0_floor=60.0, f0_ceil=800.0, frame_period=FP)
    f0 = pw.stonemask(x, f0, t, SR)
    return f0, pw.cheaptrick(x, f0, t, SR), pw.d4c(x, f0, t, SR)


def align(a, b):
    """a の各瞬間が b のどの瞬間にあたるかを返す（音色の流れを突き合わせる）。"""
    import librosa
    ma = librosa.feature.mfcc(y=a.astype(np.float32), sr=SR, n_mfcc=20, hop_length=HOP, n_fft=1024)
    mb = librosa.feature.mfcc(y=b.astype(np.float32), sr=SR, n_mfcc=20, hop_length=HOP, n_fft=1024)
    _, wp = librosa.sequence.dtw(X=ma, Y=mb, metric="cosine")
    wp = wp[::-1]
    mp = {}
    for i, j in wp:
        mp.setdefault(i, []).append(j)
    idx = np.array([int(np.median(mp[i])) if i in mp else 0 for i in range(ma.shape[1])])
    # 行ったり来たりしないよう、単調に直してなめらかにする
    idx = np.maximum.accumulate(idx)
    return idx


def warp(sp, ratio):
    n = sp.shape[1]
    src = np.arange(n)
    dst = np.clip(src / ratio, 0, n - 1)
    return np.stack([np.interp(dst, src, row) for row in sp])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.wav"):
        f.unlink()
    rep = io.open(HERE / "manner_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    import torch
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager
    path, _, _ = ModelManager().download_model("tts_models/multilingual/multi-dataset/xtts_v2")
    cfg = XttsConfig()
    cfg.load_json(str(Path(path) / "config.json"))
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)

    ks = [k for k in WEIGHTS if (PEOPLE / k / "latent.npy").exists()]
    ws = [WEIGHTS[k] for k in ks]
    g = wmean([np.load(PEOPLE / k / "latent.npy") for k in ks], ws)
    s = wmean([np.load(PEOPLE / k / "embedding.npy") for k in ks], ws)
    say("声: " + ", ".join("%s×%.1f" % (k, WEIGHTS[k]) for k in ks))

    for tag, text in LINES:
        mine = np.asarray(model.inference(text, "ja", torch.tensor(g).to(dev),
                                          torch.tensor(s).to(dev),
                                          temperature=0.75, speed=1.05)["wav"], dtype=np.float64)
        sf.write(str(OUT / ("%s_0_もと.wav" % tag)), mine, SR)
        ref = vv(text)

        f0m, spm, apm = parts(mine)
        f0r, spr, apr = parts(ref)
        idx = align(ref, mine)                       # ずんだもんの各瞬間 → 4人のどの瞬間か
        n = min(len(f0r), len(idx))
        idx = np.clip(idx[:n], 0, len(f0m) - 1)

        # 拍の長さ：ずんだもんの時間軸に、4人の響きを並べ直す
        sp = spm[idx]
        ap = apm[idx]
        # 抑揚：ずんだもんの高さの動きを、4人の高さの位置へ平行移動
        vm = f0m > 0
        vr = f0r[:n] > 0
        base_m = np.log(f0m[vm]).mean()
        base_r = np.log(f0r[:n][vr]).mean()
        f0 = np.zeros(n)
        f0[vr] = np.exp(np.log(f0r[:n][vr]) - base_r + base_m)

        say("  %s 長さ %.2f秒 → %.2f秒 / 高さ %.0fHz（4人）・抑揚の幅 ×%.2f（ずんだもん）"
            % (tag, len(mine) / SR, n * FP / 1000, np.exp(base_m),
               np.exp(np.log(f0r[:n][vr]).std() * 2)))

        for label, ratio, lift in (("1_読み方だけ", 1.00, 1.00),
                                   ("2_読み方＋小さめ", 0.92, 1.00),
                                   ("3_読み方＋小さめ＋高め", 0.92, 1.18),
                                   ("4_読み方＋もっと小さめ＋高め", 0.86, 1.25)):
            y = pw.synthesize(f0 * lift, warp(sp, ratio), ap, SR, FP)
            y = y / (np.abs(y).max() + 1e-9) * 0.85
            sf.write(str(OUT / ("%s_%s.wav" % (tag, label))), y, SR)
        say("    4通り")

    say("")
    say("読み方の出どころ: VOICEVOX:ずんだもん（音は使っていない・線と長さだけ）")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
