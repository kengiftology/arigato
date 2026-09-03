# -*- coding: utf-8 -*-
"""段階0の検証：声の特徴を平均すると、本当に「中間の声」になるか。

人の声を使う前に、仕組みが成立するかだけを確かめる。
見本はPC内蔵の音声を音程加工して作った4種類で、誰の声でもない。

確かめたいこと:
  1) 声から特徴（数値の並び）が取り出せる
  2) 4人ぶんを平均した特徴が、どの1人にも寄っていない
  3) その平均で日本語を喋らせられる
"""
import io
import sys
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).parent
SAMPLES = HERE / "samples"
OUT = HERE / "samples" / "out"
REPORT = HERE / "step0_result.txt"

TEXT = "あら、そわそわするなあ。おなべが あちこちに いるみたい。"


def cos(a, b) -> float:
    a, b = a.flatten(), b.flatten()
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def main() -> int:
    o = io.open(REPORT, "w", encoding="utf-8")

    def say(s=""):
        print(s)
        o.write(s + "\n")

    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager

    name = "tts_models/multilingual/multi-dataset/xtts_v2"
    say("モデルを用意しています（初回は1.8GBほど落とします）…")
    mm = ModelManager()
    path, _, _ = mm.download_model(name)
    cfg = XttsConfig()
    cfg.load_json(str(Path(path) / "config.json"))
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)
    say("読み込み完了（%s）" % dev)
    say()

    wavs = sorted(SAMPLES.glob("s?_*.wav"))
    if not wavs:
        say("見本が見つかりません"); o.close(); return 1

    # ① 1人ずつ特徴を取り出す（本番では people/<人>/embedding.npy に置くもの）
    lat, emb = {}, {}
    for w in wavs:
        g, s = model.get_conditioning_latents(audio_path=[str(w)])
        lat[w.stem] = g.cpu().numpy()
        emb[w.stem] = s.cpu().numpy()
    say("① 特徴の取り出し … %d人ぶん / 1人あたり %d個の数値"
        % (len(emb), emb[wavs[0].stem].size))
    say()

    # ② 平均が、どの1人にも寄っていないか
    keys = list(emb)
    avg_e = np.mean([emb[k] for k in keys], axis=0)
    avg_l = np.mean([lat[k] for k in keys], axis=0)
    say("② 平均した声は、誰に似ているか（1.0で同じ・小さいほど遠い）")
    sims = []
    for k in keys:
        c = cos(avg_e, emb[k])
        sims.append(c)
        say("     平均 ↔ %-10s %.3f" % (k, c))
    say("   もとの4人どうしの近さ: %.3f 〜 %.3f"
        % (min(cos(emb[a], emb[b]) for i, a in enumerate(keys) for b in keys[i + 1:]),
           max(cos(emb[a], emb[b]) for i, a in enumerate(keys) for b in keys[i + 1:])))
    say("   平均から見た遠近の差: %.3f （小さいほど、誰にも寄っていない）"
        % (max(sims) - min(sims)))
    say()

    # ③ その平均で日本語を喋らせる
    OUT.mkdir(exist_ok=True)
    say("③ 喋らせてみる …")
    import soundfile as sf
    for tag, (g, s) in [("blend", (avg_l, avg_e))] + \
            [(k, (lat[k], emb[k])) for k in keys]:
        wav = model.inference(
            TEXT, "ja",
            torch.tensor(g).to(dev), torch.tensor(s).to(dev),
            temperature=0.7)["wav"]
        p = OUT / ("%s.wav" % tag)
        sf.write(str(p), np.asarray(wav), 24000)
        say("     %s → %s" % (tag, p.name))
    say()
    say("聴き比べ: samples/out/blend.wav が「誰でもない声」になっているか")
    o.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
