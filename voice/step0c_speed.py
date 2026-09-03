# -*- coding: utf-8 -*-
"""速さと言葉づかいを切り分ける（2026-09-03）。

本人の評価：「bとcが面白かった。ゆっくり喋ってるからかね。速度上げたらよい？
             あと言葉。普通の日本語じゃないからピンときてないのかも」

前回の文は「あら…… そわそわ するなあ…… おなべが、あちこちに…… いるみたい……」で、
間を作るために点々と分かち書きを入れた自作の文だった。ひらがなだけを空けて並べると
読み上げ側が語の切れ目を取り違えるので、不自然さの一因はここかもしれない。

2つを別々に動かして、どちらが効いているかを見る:
  速さ  … 1.0（前回）と 1.18
  言葉 … 前回の自作文と、ふつうに書いた日本語（漢字あり・点々なし）
声は前回よかった b（端どうしの中点）と c（中心から押し出した）だけを使う。
"""
import io
import itertools
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

HERE = Path(__file__).parent
SAMPLES = HERE / "samples"
OUT = SAMPLES / "speed"

OLD = "あら…… そわそわ するなあ…… おなべが、あちこちに…… いるみたい……"
NEW = "あら、お鍋がこんなに出てるのね。なんだかそわそわするなあ"


def cos(a, b):
    a, b = a.flatten(), b.flatten()
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def main():
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager

    o = io.open(HERE / "step0c_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        o.write(s + "\n")

    mm = ModelManager()
    path, _, _ = mm.download_model("tts_models/multilingual/multi-dataset/xtts_v2")
    cfg = XttsConfig()
    cfg.load_json(str(Path(path) / "config.json"))
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)

    lat, emb = {}, {}
    for w in sorted(SAMPLES.glob("s?_*.wav")):
        g, s = model.get_conditioning_latents(audio_path=[str(w)])
        lat[w.stem], emb[w.stem] = g.cpu().numpy(), s.cpu().numpy()
    keys = list(emb)

    far = min(itertools.combinations(keys, 2), key=lambda p: cos(emb[p[0]], emb[p[1]]))
    voices = {"b": ((lat[far[0]] + lat[far[1]]) / 2, (emb[far[0]] + emb[far[1]]) / 2)}

    avg_l = np.mean([lat[k] for k in keys], axis=0)
    avg_e = np.mean([emb[k] for k in keys], axis=0)
    near = max(keys, key=lambda k: cos(avg_e, emb[k]))
    voices["c"] = (avg_l + 0.6 * (avg_l - lat[near]), avg_e + 0.6 * (avg_e - emb[near]))

    OUT.mkdir(exist_ok=True)
    plan = [
        ("b_新しい言葉_ふつう", "b", NEW, 1.00),
        ("b_新しい言葉_速め", "b", NEW, 1.18),
        ("c_新しい言葉_ふつう", "c", NEW, 1.00),
        ("c_新しい言葉_速め", "c", NEW, 1.18),
        ("b_前の言葉_速め", "b", OLD, 1.18),      # 速さだけの効果を見る
    ]
    for tag, vk, text, sp in plan:
        g, s = voices[vk]
        wav = np.asarray(model.inference(
            text, "ja", torch.tensor(g).to(dev), torch.tensor(s).to(dev),
            temperature=0.75, speed=sp)["wav"])
        sf.write(str(OUT / (tag + ".wav")), wav, 24000)
        say("%-22s 声=%s 速さ=%.2f  「%s」" % (tag, vk, sp, text[:24]))
    o.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
