# -*- coding: utf-8 -*-
"""ひとりずつの声と、かわいらしさの作り方を試す（2026-09-05）。

かわいらしさは3つの層でできている。ここで試せるのは上2つ。

  声そのもの … 高さ・軽さ・息の混ざり方
  喋り方     … 速さ・語尾の上がり・言い切らなさ
  ふるまい   … こちらに関心があること（なつき度の仕組み側の話）

3つ目がいちばん効くはずだが、それは声では作れない。
ここでは声と喋り方を動かして、どこが効くかを切り分ける。
"""
import io
import subprocess
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
OUT = HERE / "work" / "cute"

PLAIN = "あら、お鍋がこんなに出てるのね。なんだかそわそわするなあ"
SOFT = "あ、お鍋…こんなに出てるのねえ。なんだか、そわそわしちゃうなあ"


def pitch(src: Path, dst: Path, semitones: float) -> None:
    """音の高さを上げる。声の太さも一緒に上がるので、体が小さく聞こえる。"""
    r = 2 ** (semitones / 12.0)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src),
         "-af", "asetrate=24000*%.5f,aresample=24000,atempo=%.5f" % (r, 1 / r),
         str(dst)], check=True)


def main() -> int:
    rep = io.open(HERE / "cute_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + "\n")
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
        lat[d.name] = np.load(d / "latent.npy")
        emb[d.name] = np.load(d / "embedding.npy")
    keys = list(emb)
    avg_l = np.mean([lat[k] for k in keys], axis=0)
    avg_e = np.mean([emb[k] for k in keys], axis=0)

    OUT.mkdir(parents=True, exist_ok=True)

    def make(tag, g, s, text, speed):
        wav = np.asarray(model.inference(
            text, "ja", torch.tensor(g).to(dev), torch.tensor(s).to(dev),
            temperature=0.75, speed=speed)["wav"])
        p = OUT / (tag + ".wav")
        sf.write(str(p), wav, 24000)
        say("  " + tag)
        return p

    say("■ ひとりずつ（本人の声そのまま）")
    for k in keys:
        make("solo_" + k, lat[k], emb[k], PLAIN, 1.18)

    say("\n■ かわいらしさ：声の高さを上げる（4人の平均から）")
    base = make("cute0_もと", avg_l, avg_e, PLAIN, 1.18)
    for st in (2, 4):
        pitch(base, OUT / ("cute1_高さ+%d.wav" % st), st)
        say("  cute1_高さ+%d" % st)

    say("\n■ かわいらしさ：喋り方をやわらかく")
    soft = make("cute2_やわらかい言葉", avg_l, avg_e, SOFT, 1.05)

    say("\n■ 両方（高さ＋やわらかい言葉）")
    pitch(soft, OUT / "cute3_両方.wav", 3)
    say("  cute3_両方")

    say("\nできました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
