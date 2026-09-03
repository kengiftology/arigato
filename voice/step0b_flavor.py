# -*- coding: utf-8 -*-
"""平均した声が「話しかけたくない声」だった問題を切り分ける。

本人の評価：「誰でもないけど、オーディブル聞いてるみたい。話したいとは思わない」

平均は centroid を取る操作なので、声を魅力的にしている尖った部分
（かすれ・癖・偏り）を真っ先に打ち消す。「誰でもない」と「特徴がない」は
同じ操作の裏表で、そこが今回いちばん効いている疑いがある。

ただし原因が声質だとは限らないので、4通り作って切り分ける:
  a 独り言 … 声はそのまま、喋り方だけ変える（朗読 → つぶやき）
  b 端どうしの中点 … いちばん違う2人の真ん中。平均より尖るはず
  c 平均から遠ざける … 中心から少し押し出して、癖を戻す
  d 実機の音 … 16kHz・モノラル・小さなスピーカー・台所の反響
      いま聴いているのは澄んだ24kHzだが、実機はこう鳴る
"""
import io
import itertools
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

HERE = Path(__file__).parent
SAMPLES = HERE / "samples"
OUT = SAMPLES / "flavor"

READ = "あら、そわそわするなあ。おなべが あちこちに いるみたい。"
MUTTER = "あら…… そわそわ するなあ…… おなべが、あちこちに…… いるみたい……"


def cos(a, b):
    a, b = a.flatten(), b.flatten()
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def main():
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager

    o = io.open(HERE / "step0b_result.txt", "w", encoding="utf-8")

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

    wavs = sorted(SAMPLES.glob("s?_*.wav"))
    lat, emb = {}, {}
    for w in wavs:
        g, s = model.get_conditioning_latents(audio_path=[str(w)])
        lat[w.stem], emb[w.stem] = g.cpu().numpy(), s.cpu().numpy()
    keys = list(emb)

    avg_l = np.mean([lat[k] for k in keys], axis=0)
    avg_e = np.mean([emb[k] for k in keys], axis=0)

    # b いちばん違う2人を探して、その中点を取る
    far = min(itertools.combinations(keys, 2), key=lambda p: cos(emb[p[0]], emb[p[1]]))
    say("いちばん違う2人: %s と %s（近さ %.3f）" % (far[0], far[1], cos(emb[far[0]], emb[far[1]])))
    mid_l = (lat[far[0]] + lat[far[1]]) / 2
    mid_e = (emb[far[0]] + emb[far[1]]) / 2

    # c 中心から押し出す。いちばん平均に近い人の逆向きへ伸ばす
    near = max(keys, key=lambda k: cos(avg_e, emb[k]))
    say("いちばん平均に近い人: %s（%.3f）→ その逆へ押し出す" % (near, cos(avg_e, emb[near])))
    a = 0.6
    out_l = avg_l + a * (avg_l - lat[near])
    out_e = avg_e + a * (avg_e - emb[near])
    say("押し出した後、誰にどれだけ近いか:")
    for k in keys:
        say("   %-10s %.3f" % (k, cos(out_e, emb[k])))
    say()

    OUT.mkdir(exist_ok=True)
    plan = [
        ("a_mutter", avg_l, avg_e, MUTTER, "平均の声で、独り言の喋り方"),
        ("b_midpoint", mid_l, mid_e, MUTTER, "いちばん違う2人の中点"),
        ("c_pushed", out_l, out_e, MUTTER, "平均から押し出して癖を戻した"),
        ("z_原型", avg_l, avg_e, READ, "前回と同じ（比較用）"),
    ]
    for tag, g, s, text, note in plan:
        wav = np.asarray(model.inference(
            text, "ja", torch.tensor(g).to(dev), torch.tensor(s).to(dev),
            temperature=0.75, length_penalty=1.0, repetition_penalty=5.0)["wav"])
        sf.write(str(OUT / (tag + ".wav")), wav, 24000)
        say("%-12s %s" % (tag, note))
    o.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
