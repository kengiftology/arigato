# -*- coding: utf-8 -*-
"""子音を残した鳴き声（2026-09-06）。

本人の評価：「何言ってるかさっぱりわからない泣き声」
憲法の表でいう「削りすぎ ＝ どう解釈していいか分からない」に落ちていた。

原因は子音を捨てたこと。母音だけを粒に刻んだので音の立ち上がりが消え、
喋ろうとしている感じが無くなった。「む〜」には m があり、ピングーの
「ピーピー」には p がある。子音があるから発話に聞こえる。

今回は刻まない。子音つきの意味のない語をそのまま喋らせ、
抑揚は語の書き方（伸ばし・促音・？！）と、全体のピッチで作る。
"""
import io
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
OUT = HERE / "work" / "babble3"
SR = 24000
PITCH = 2.4          # 半音。1.15倍。憲法第2条

# 意味のない語。子音を残し、抑揚は書き方で作る。
# 「む」を軸にするのは『弱いロボット』のむ〜に倣ったもの。
LINES = {
    "きづいた":   "む? むー",
    "うれしい":   "むー! むむっ、むー!",
    "ごきげん":   "むー、むむ、むーん",
    "ふん":       "むん。",
    "きになる":   "む、むー?",
    "しょんぼり": "むー……むぅ",
    "ねむい":     "むぅ……むー……",
    "こまった":   "む、むむ、むー……",
    "よびかけ":   "むっ、むー!",
    "ためらい":   "むー……えと……むむ……んー……むっ!",
}


def shift(x, semitones):
    """全体の音の高さを上げる。声の太さごと動くので体が小さく聞こえる。"""
    r = 2 ** (semitones / 12.0)
    idx = np.arange(0, len(x), r)
    idx = idx[idx < len(x) - 1].astype(np.float32)
    lo = idx.astype(np.int32)
    fr = idx - lo
    return x[lo] * (1 - fr) + x[lo + 1] * fr


def main():
    rep = io.open(HERE / "babble3_result.txt", "w", encoding="utf-8")

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

    kid = PEOPLE / "p_kid"
    adults = [d for d in sorted(PEOPLE.glob("p_*")) if d.name != "p_kid"]
    al = np.mean([np.load(d / "latent.npy") for d in adults], axis=0)
    ae = np.mean([np.load(d / "embedding.npy") for d in adults], axis=0)
    kl, ke = np.load(kid / "latent.npy"), np.load(kid / "embedding.npy")
    voices = {"kid": (kl, ke), "half": ((al + kl) / 2, (ae + ke) / 2)}

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    for vname, (g, s) in voices.items():
        say("")
        say("■ %s" % ("甥っ子さんの声" if vname == "kid" else "大人と半々"))
        for name, text in LINES.items():
            wav = np.asarray(model.inference(
                text, "ja", torch.tensor(g).to(dev), torch.tensor(s).to(dev),
                temperature=0.8, speed=1.05)["wav"])
            y = shift(wav, PITCH)
            # 第5条 頭に間を置く。すぐ鳴り出さない
            y = np.concatenate([np.zeros(int(0.4 * SR), dtype=np.float32), y])
            y = y / (np.abs(y).max() + 1e-9) * 0.75      # 第7条
            sf.write(str(OUT / ("%s_%s.wav" % (vname, name))), y, SR)
            say("  %-10s 「%s」" % (name, text))

    say("")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
