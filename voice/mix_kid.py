# -*- coding: utf-8 -*-
"""大人と子どもの混ぜ方を変えて試す（2026-09-05）。

5人を素直に平均すると、大人4対子ども1の多数決になり、子どもの声は
ほとんど残らない（平均から大人へ0.73〜0.81、子どもへ0.50）。

そこで人数ではなく、大人側と子ども側を半々にする。
大人4人をまず1つに均してから、子どもと半分ずつ混ぜる。
こうすると子どもの取り分が4分の1から2分の1になり、
それでいて大人の誰か1人に寄ることもない。

割合を変えたものを何通りか作って、耳で決める。
"""
import io
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
OUT = HERE / "work" / "mix"
LINE = "あら、お鍋がこんなに出てるのね。なんだかそわそわするなあ"

rep = io.open(HERE / "mix_result.txt", "w", encoding="utf-8")


def say(s=""):
    print(s)
    rep.write(s + chr(10))
    rep.flush()


def main():
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
    kid = PEOPLE / "p_kid"
    al = np.mean([np.load(d / "latent.npy") for d in adults], axis=0)
    ae = np.mean([np.load(d / "embedding.npy") for d in adults], axis=0)
    kl = np.load(kid / "latent.npy")
    ke = np.load(kid / "embedding.npy")

    cos = lambda x, y: float(np.dot(x.flatten(), y.flatten()) /
                             (np.linalg.norm(x) * np.linalg.norm(y) + 1e-9))
    say("大人4人の平均 ↔ 子ども の近さ %.3f" % cos(ae, ke))
    say("")

    OUT.mkdir(parents=True, exist_ok=True)
    for w in (0.3, 0.5, 0.7):
        g = al * (1 - w) + kl * w
        s = ae * (1 - w) + ke * w
        wav = np.asarray(model.inference(
            LINE, "ja", torch.tensor(g).to(dev), torch.tensor(s).to(dev),
            temperature=0.75, speed=1.18)["wav"])
        tag = "kid%02d" % int(w * 100)
        sf.write(str(OUT / (tag + ".wav")), wav, 24000)
        say("%s … 子ども %d%% / 大人 %d%%   大人平均との近さ %.3f・子どもとの近さ %.3f"
            % (tag, int(w * 100), int((1 - w) * 100), cos(s, ae), cos(s, ke)))

    # 半々のものを、地霊の声として保存する
    g = (al + kl) / 2
    s = (ae + ke) / 2
    sp = HERE / "spirit"
    sp.mkdir(exist_ok=True)
    np.save(sp / "latent.npy", g)
    np.save(sp / "embedding.npy", s)
    names = [d.name for d in adults] + ["p_kid"]
    (sp / "made_from.txt").write_text(
        "地霊の声は、いま次の人たちから作られています:" + chr(10)
        + chr(10).join("  - " + n for n in names)
        + chr(10) + chr(10)
        + "大人側と子ども側を半々で混ぜています。人数で平均すると"
        + "大人に呑まれてしまうためです。" + chr(10)
        + "抜けたい人がいたら people/その人 のフォルダを消して、"
        + "mix_kid.py を走らせ直してください。" + chr(10), encoding="utf-8")
    say("")
    say("半々のものを spirit/ に保存しました")
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
