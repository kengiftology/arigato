# -*- coding: utf-8 -*-
"""同じ読み方のまま、3人の声を1つに溶かす（2026-09-06）。

前に試した「3人同時」は、3人が同時に喋っているように聞こえた。
音をそのまま重ねたからで、当然そうなる。

今度は重ねない。声を3つの部品に分けてから、部品ごとに平均する。

  高さ      … 声帯が震える速さ
  響きの形  … 口と喉の形（その人らしさは、ほぼここにある）
  かすれ    … 息の混ざり具合

読み方（どの拍を何秒のばすか）は1つに固定してから3人に喋らせるので、
3つの音は同じ時刻に同じ音を出している。だから部品ごとに平均できる。
出てくるのは「3人が同時に喋る音」ではなく、「3人のどれでもない1つの声」。

さらに、響きの形の目盛りを伸び縮みさせると、体の大きさが変わる。
0.9なら小さい体、1.1なら大きい体。ここで3人からもっと離せる。

もとの音は VOICEVOX で作っている。使うならクレジットが要る:
  VOICEVOX:ずんだもん / VOICEVOX:四国めたん / VOICEVOX:後鬼
"""
import io
import json
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pyworld as pw
import soundfile as sf

HERE = Path(__file__).parent
OUT = HERE / "work" / "blend"
API = "http://127.0.0.1:50021"
SPEAKERS = {"ずんだもん": 3, "四国めたん": 2, "後鬼": 27}

LINES = [
    ("1_なのり",   "あのね、わたし、きっちんちゃん。"),
    ("2_あいさつ", "あ、きた。"),
    ("3_しらせ",   "あのね、さっきね、だれかがね、きれいにしてくれたのかなあ。"),
    ("4_そわそわ", "なんだかね、そわそわするなあ。"),
]


def query(text, sid):
    u = API + "/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": sid})
    with urllib.request.urlopen(urllib.request.Request(u, method="POST"), timeout=30) as r:
        return json.load(r)


def synth(q, sid, path):
    u = API + "/synthesis?" + urllib.parse.urlencode({"speaker": sid})
    req = urllib.request.Request(u, method="POST",
                                 data=json.dumps(q).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        path.write_bytes(r.read())
    x, sr = sf.read(str(path))
    if x.ndim > 1:
        x = x.mean(axis=1)
    return x.astype(np.float64), sr


def parts(x, sr):
    """声を3つの部品に分ける。"""
    f0, t = pw.harvest(x, sr, f0_floor=60.0, f0_ceil=800.0, frame_period=5.0)
    f0 = pw.stonemask(x, f0, t, sr)
    sp = pw.cheaptrick(x, f0, t, sr)      # 響きの形
    ap = pw.d4c(x, f0, t, sr)             # かすれ
    return f0, sp, ap


def warp(sp, ratio):
    """響きの形の目盛りを伸び縮みさせる。体の大きさが変わる。"""
    n = sp.shape[1]
    src = np.arange(n)
    dst = np.clip(src / ratio, 0, n - 1)
    out = np.empty_like(sp)
    for i in range(sp.shape[0]):
        out[i] = np.interp(dst, src, sp[i])
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.wav"):
        f.unlink()
    rep = io.open(HERE / "blend_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    work = HERE / "work" / "blend_raw"
    work.mkdir(parents=True, exist_ok=True)

    for tag, text in LINES:
        # 読み方は1つに固定する。これで3人が同じ時刻に同じ音を出す
        q = query(text, 3)
        f0s, sps, aps, sr = [], [], [], None
        for name, sid in SPEAKERS.items():
            x, sr = synth(q, sid, work / ("%s_%s.wav" % (tag, name)))
            f0, sp, ap = parts(x, sr)
            f0s.append(f0)
            sps.append(sp)
            aps.append(ap)
        m = min(len(a) for a in f0s)
        F = np.array([a[:m] for a in f0s])
        S = np.array([a[:m] for a in sps])
        A = np.array([a[:m] for a in aps])

        # 高さは、声のあるところだけを掛け合わせて平均する（対数の平均）
        voiced = (F > 0).all(axis=0)
        f0 = np.zeros(m)
        f0[voiced] = np.exp(np.log(F[:, voiced] + 1e-9).mean(axis=0))
        sp = np.exp(np.log(S + 1e-20).mean(axis=0))     # 響きも対数で平均
        ap = A.mean(axis=0)

        for label, ratio, pitch in (("そのまま", 1.00, 1.00),
                                    ("小さめ",   0.92, 1.06),
                                    ("もっと小さめ", 0.86, 1.12),
                                    ("大きめ",   1.08, 0.94)):
            y = pw.synthesize(f0 * pitch, warp(sp, ratio), ap, sr, 5.0)
            y = y / (np.abs(y).max() + 1e-9) * 0.85
            sf.write(str(OUT / ("%s_%s.wav" % (tag, label))), y, sr)
        say("%s 「%s」 … 4通り" % (tag, text))

    say("")
    say("もとの3人: " + " / ".join(SPEAKERS))
    say("使うならクレジットが要る（VOICEVOX:名前）")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
