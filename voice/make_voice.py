# -*- coding: utf-8 -*-
"""人ごとの声から特徴を出し、平均して地霊の声を作る。

  python make_voice.py --keep p_a,p_b,p_d,p_e

--keep に挙げた人だけを残し、他は消す。
判断していない人の声を持ち続けないため（README・同意の考え方）。

作るのは3通り:
  平均      … そのまま足して割る。誰でもない代わりに、特徴も薄い
  押し出し  … 平均から少し離す。匿名のまま、癖を戻す
  実機      … 16kHz・小さなスピーカー・台所の反響を通した音

「平均はつまらない」という評価が前に出ているので、押し出しも一緒に作る。
"""
import argparse
import io
import shutil
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
SPIRIT = HERE / "spirit"
OUT = HERE / "work" / "voice"

LINE = "あら、お鍋がこんなに出てるのね。なんだかそわそわするなあ"
PUSH = 0.6                     # 中心からどれだけ押し出すか


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", default="", help="残す人（コンマ区切り）")
    ap.add_argument("--speed", type=float, default=1.18)
    a = ap.parse_args()

    rep = io.open(HERE / "voice_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + "\n")
        rep.flush()

    keep = [k.strip() for k in a.keep.split(",") if k.strip()]
    if keep:
        for d in sorted(PEOPLE.glob("p_*")):
            if d.name not in keep:
                shutil.rmtree(d)
                say("使わないので消しました: %s" % d.name)
    dirs = sorted(d for d in PEOPLE.glob("p_*") if (d / "clips").exists())
    say("\n地霊の声を作る人: %s" % ", ".join(d.name for d in dirs))
    if len(dirs) < 3:
        say("3人以上ないと、誰かの声に寄って聞こえます")

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

    say("\n1人ずつ特徴を出しています…")
    lat, emb = {}, {}
    for d in dirs:
        clips = [str(p) for p in sorted((d / "clips").glob("*.wav"))]
        g, s = model.get_conditioning_latents(audio_path=clips)
        lat[d.name], emb[d.name] = g.cpu().numpy(), s.cpu().numpy()
        np.save(d / "latent.npy", lat[d.name])
        np.save(d / "embedding.npy", emb[d.name])
        say("  %s … %d本から" % (d.name, len(clips)))

    keys = list(emb)
    cos = lambda x, y: float(np.dot(x.flatten(), y.flatten()) /
                             (np.linalg.norm(x) * np.linalg.norm(y) + 1e-9))
    avg_l = np.mean([lat[k] for k in keys], axis=0)
    avg_e = np.mean([emb[k] for k in keys], axis=0)

    say("\n平均は誰に似ているか（1.0で同じ）")
    sims = []
    for k in keys:
        c = cos(avg_e, emb[k])
        sims.append(c)
        say("  平均 ↔ %-5s %.3f" % (k, c))
    pairs = [cos(emb[x], emb[y]) for i, x in enumerate(keys) for y in keys[i + 1:]]
    say("  もとの人どうしの近さ: %.3f 〜 %.3f" % (min(pairs), max(pairs)))
    say("  特定の誰かへの寄り: %.3f（小さいほど公平）" % (max(sims) - min(sims)))

    near = max(keys, key=lambda k: cos(avg_e, emb[k]))
    out_l = avg_l + PUSH * (avg_l - lat[near])
    out_e = avg_e + PUSH * (avg_e - emb[near])
    say("\n押し出す先: %s の逆向き" % near)

    SPIRIT.mkdir(exist_ok=True)
    np.save(SPIRIT / "latent.npy", out_l)
    np.save(SPIRIT / "embedding.npy", out_e)
    (SPIRIT / "made_from.txt").write_text(
        "地霊の声は、いま次の%d人から作られています:\n" % len(keys)
        + "\n".join("  - " + k for k in keys)
        + "\n\n抜けたい人がいたら people/その人 のフォルダを消して、"
          "make_voice.py を走らせ直してください。\n", encoding="utf-8")

    OUT.mkdir(parents=True, exist_ok=True)
    say("\n喋らせています…")
    for tag, g, s in (("blend", avg_l, avg_e), ("pushed", out_l, out_e)):
        wav = np.asarray(model.inference(
            LINE, "ja", torch.tensor(g).to(dev), torch.tensor(s).to(dev),
            temperature=0.75, speed=a.speed)["wav"])
        sf.write(str(OUT / (tag + ".wav")), wav, 24000)
        say("  %s" % tag)
    say("\nできました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
