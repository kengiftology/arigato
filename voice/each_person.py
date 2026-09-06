# -*- coding: utf-8 -*-
"""1人ずつ聴く（2026-09-06）。

4人が別人だと分かった。次は「誰が何を持ち込んでいるか」。
  単独 … その人の特徴だけで喋らせる
  抜き … その人を抜いた3人の平均で喋らせる（いなくなると何が変わるか）

people/<人>/latent.npy, embedding.npy（blend_people.py が保存したもの）を使う。
"""
import io
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
OUT = HERE / "work" / "each"
TEXT = "あのね、わたし、きっちんちゃん。"


def even_mean(vs):
    ns = [np.linalg.norm(v) for v in vs]
    m = np.mean([v / (n + 1e-9) for v, n in zip(vs, ns)], axis=0)
    return m / (np.linalg.norm(m) + 1e-9) * float(np.mean(ns))


def main():
    rep = io.open(HERE / "each_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    import torch
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager
    import soundfile as sf

    path, _, _ = ModelManager().download_model(
        "tts_models/multilingual/multi-dataset/xtts_v2")
    cfg = XttsConfig()
    cfg.load_json(str(Path(path) / "config.json"))
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)

    lat, emb = {}, {}
    for d in sorted(PEOPLE.glob("p_*")):
        if (d / "latent.npy").exists():
            lat[d.name] = np.load(d / "latent.npy")
            emb[d.name] = np.load(d / "embedding.npy")
    keys = list(lat)
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.wav"):
        f.unlink()

    def make(tag, g, s):
        wav = np.asarray(model.inference(
            TEXT, "ja", torch.tensor(g).to(dev), torch.tensor(s).to(dev),
            temperature=0.75, speed=1.05)["wav"])
        sf.write(str(OUT / (tag + ".wav")), wav, 24000)
        say("  " + tag)

    say("■ 単独")
    for k in keys:
        make("単独_" + k, lat[k], emb[k])
    say("■ 1人抜き")
    for k in keys:
        rest = [x for x in keys if x != k]
        make("抜き_" + k + "なし", even_mean([lat[x] for x in rest]),
             even_mean([emb[x] for x in rest]))
    say("")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
