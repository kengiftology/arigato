# -*- coding: utf-8 -*-
"""「誰の声でもない声」を作る（2026-09-06）。

平均は数値上どの人にも寄っていない（0.77〜0.85・差0.077）のに、
耳では誰かの声に聞こえる。矛盾ではない――平均しても出てくるのは
「1人ぶんの声」だから。人の声の空間の中では、どの点も誰かの声になる。
平均顔がやはり1つの顔であるのと同じ。

そこで空間の外へ出す。2つの筋がある。

  重ねる … 4人が同時に喋る。ひとりに帰属できないのは、実際に複数だから。
           「場所の声が使う人たちからできている」を、字義どおり鳴らす形。
  響かせる … 口からではなく部屋から聞こえるようにする。
           身体を持たない声になる。

組み合わせも作って比べる。
"""
import io
import subprocess
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
OUT = HERE / "work" / "nobody"
SR = 24000
LINE = "あのね、さっきね……だれかがね、きれいにしてくれたのかなあ。"


def pad(xs):
    n = max(len(x) for x in xs)
    return [np.pad(x, (0, n - len(x))) for x in xs]


def shift(x, st):
    r = 2 ** (st / 12.0)
    i = np.arange(0, len(x), r)
    i = i[i < len(x) - 1].astype(np.float32)
    lo = i.astype(np.int32)
    fr = i - lo
    return x[lo] * (1 - fr) + x[lo + 1] * fr


def main():
    rep = io.open(HERE / "nobody_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    import torch
    import soundfile as sf
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager

    path, _, _ = ModelManager().download_model(
        "tts_models/multilingual/multi-dataset/xtts_v2")
    cfg = XttsConfig()
    cfg.load_json(str(Path(path) / "config.json"))
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)

    adults = [d for d in sorted(PEOPLE.glob("p_*")) if d.name != "p_kid"]
    lats = [np.load(d / "latent.npy") for d in adults]
    embs = [np.load(d / "embedding.npy") for d in adults]

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    def gen(g, e, temp=0.75):
        return np.asarray(model.inference(
            LINE, "ja", torch.tensor(g).to(dev), torch.tensor(e).to(dev),
            temperature=temp, speed=1.0)["wav"])

    # 0 平均ひとつ（いまのもの・比較用）
    avg = gen(np.mean(lats, axis=0), np.mean(embs, axis=0))
    sf.write(str(OUT / "0_平均ひとつ.wav"), avg / (np.abs(avg).max() + 1e-9) * 0.8, SR)
    say("0 平均ひとつ（いまのもの）")

    # 1 重ねる。4人が同時に喋る。わずかにずらして、揃いすぎないようにする
    say("4人それぞれに喋らせています...")
    solos = [gen(l, e, temp=0.8) for l, e in zip(lats, embs)]
    solos = pad(solos)
    rng = np.random.default_rng(3)
    mixed = np.zeros(len(solos[0]) + int(0.2 * SR), dtype=np.float32)
    for k, y in enumerate(solos):
        off = int(rng.uniform(0.0, 0.09) * SR)          # ずれ
        z = shift(y, rng.uniform(-0.6, 0.6))            # わずかな音程差
        n = min(len(z), len(mixed) - off)
        mixed[off:off + n] += z[:n] * (0.75 if k == 0 else 0.55)
    mixed = mixed / (np.abs(mixed).max() + 1e-9) * 0.8
    sf.write(str(OUT / "1_4人が同時に.wav"), mixed, SR)
    say("1 4人が同時に")

    # 2 平均を自分自身と少しずらして重ねる（薄い重ね）
    # 音程を変えると長さも変わるので、置ける範囲だけ足す
    d = np.zeros(len(avg) + int(0.2 * SR), dtype=np.float32)
    d[:len(avg)] += avg
    for off_s, st, vol in ((0.022, 0.35, 0.6), (0.041, -0.3, 0.5)):
        off = int(off_s * SR)
        z = shift(avg, st)
        n = min(len(z), len(d) - off)
        d[off:off + n] += z[:n] * vol
    d = d / (np.abs(d).max() + 1e-9) * 0.8
    sf.write(str(OUT / "2_平均を薄く重ねる.wav"), d, SR)
    say("2 平均を薄く重ねる")

    say("")
    say("部屋の響きを足したものも作ります...")
    for src, tag in (("0_平均ひとつ", "3_平均＋部屋"),
                     ("1_4人が同時に", "4_4人同時＋部屋"),
                     ("2_平均を薄く重ねる", "5_薄い重ね＋部屋")):
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(OUT / (src + ".wav")),
             "-af", "aecho=0.85:0.7:35|58|91:0.28|0.2|0.13,"
                    "highpass=f=190,lowpass=f=7000",
             str(OUT / (tag + ".wav"))], check=False)
        say(tag)

    say("")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
