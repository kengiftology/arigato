# -*- coding: utf-8 -*-
"""預かっている人の声を、均一に混ぜる（2026-09-06）。

前に同じことをして失敗している。4人を平均したのに「全部姉の声に聞こえる」
と言われた。平均したのに1人に寄る、というのは起こりうる。

声の特徴は512個の数字の並びで表されている。この並びには「長さ」があり、
長い人ほど平均に強く出る。声が大きい人の意見が通るのと同じことが起きる。
足して割るだけでは均一にならない。

そこでここでは、まず全員の長さを揃えてから足す。
そのうえで「平均が誰にどれだけ似ているか」を必ず出す。
似ている度合いが人によってばらついていたら、それは均一ではないので、
数字で分かるようにする。

  python blend_people.py                    … いまある人全員で
  python blend_people.py --keep p_a,p_b     … 挙げた人だけ残して他は消す
  python blend_people.py --push 0.4         … 一番近い人から離す

抜けたい人が出たら people/その人 のフォルダを消して、走らせ直すだけ。
"""
import argparse
import io
import shutil
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
SPIRIT = HERE / "spirit"
OUT = HERE / "work" / "blend_people"

LINES = [
    ("1_なのり",   "あのね、わたし、きっちんちゃん。"),
    ("2_あいさつ", "あ、きた。"),
    ("3_しらせ",   "あのね、さっきね、だれかがね、きれいにしてくれたのかなあ。"),
    ("4_そわそわ", "なんだかね、そわそわするなあ。"),
]


def cos(x, y):
    a, b = x.flatten(), y.flatten()
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def even_mean(vs):
    """均一に混ぜる。まず長さを揃えてから足す。

    揃えずに足すと、長い並びを持つ人が平均を引っぱる。
    元の長さの平均に戻してから返す（音の大きさを変えないため）。"""
    ns = [np.linalg.norm(v) for v in vs]
    unit = [v / (n + 1e-9) for v, n in zip(vs, ns)]
    m = np.mean(unit, axis=0)
    return m / (np.linalg.norm(m) + 1e-9) * float(np.mean(ns))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", default="")
    ap.add_argument("--push", type=float, default=0.0,
                    help="一番近い人から離す量。0で離さない")
    ap.add_argument("--speed", type=float, default=1.05)
    a = ap.parse_args()

    rep = io.open(HERE / "blend_people_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    keep = [k.strip() for k in a.keep.split(",") if k.strip()]
    if keep:
        for d in sorted(PEOPLE.glob("p_*")):
            if d.name not in keep:
                shutil.rmtree(d)
                say("使わないので消しました: %s" % d.name)

    dirs = sorted(d for d in PEOPLE.glob("p_*") if (d / "clips").exists())
    if len(dirs) < 3:
        say("3人未満です。平均しても誰かの声に寄ります")
    say("混ぜる人: %s" % ", ".join(d.name for d in dirs))

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

    say("")
    say("1人ずつ特徴を出しています…")
    lat, emb = {}, {}
    for d in dirs:
        clips = [str(p) for p in sorted((d / "clips").glob("*.wav"))]
        g, s = model.get_conditioning_latents(audio_path=clips)
        lat[d.name] = g.cpu().numpy()
        emb[d.name] = s.cpu().numpy()
        np.save(d / "latent.npy", lat[d.name])
        np.save(d / "embedding.npy", emb[d.name])
        say("  %-8s %2d本  特徴の長さ %.2f" % (d.name, len(clips),
                                            float(np.linalg.norm(emb[d.name]))))

    keys = list(emb)
    say("")
    say("特徴の長さがばらついていると、長い人が平均を引っぱります")
    say("（前に「全部姉の声に聞こえる」と言われたときの、疑っている原因）")

    # 単純に足して割った場合と、長さを揃えてから足した場合を並べる
    plain_e = np.mean([emb[k] for k in keys], axis=0)
    plain_l = np.mean([lat[k] for k in keys], axis=0)
    even_e = even_mean([emb[k] for k in keys])
    even_l = even_mean([lat[k] for k in keys])

    def report(tag, e):
        sims = [cos(e, emb[k]) for k in keys]
        say("  %-14s " % tag + "  ".join("%s %.3f" % (k, c)
                                         for k, c in zip(keys, sims)))
        say("  %-14s 寄り %.3f（0に近いほど均一）" % ("", max(sims) - min(sims)))
        return sims

    say("")
    say("平均が誰にどれだけ似ているか（1.0で同じ）")
    report("足して割る", plain_e)
    sims = report("長さを揃える", even_e)

    out_e, out_l = even_e, even_l
    if a.push > 0:
        near = keys[int(np.argmax(sims))]
        out_e = even_e + a.push * (even_e - emb[near])
        out_l = even_l + a.push * (even_l - lat[near])
        say("")
        say("一番近い %s から %.2f だけ離しました" % (near, a.push))
        report("離したあと", out_e)

    SPIRIT.mkdir(exist_ok=True)
    np.save(SPIRIT / "latent.npy", out_l)
    np.save(SPIRIT / "embedding.npy", out_e)
    (SPIRIT / "made_from.txt").write_text(
        "地霊の声は、いま次の%d人から作られています:\n" % len(keys)
        + "\n".join("  - " + k for k in keys)
        + "\n\n抜けたい人がいたら people/その人 のフォルダを消して、"
          "blend_people.py を走らせ直してください。\n", encoding="utf-8")

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.wav"):
        f.unlink()
    say("")
    say("喋らせています…")
    for tag, text in LINES:
        wav = np.asarray(model.inference(
            text, "ja", torch.tensor(out_l).to(dev), torch.tensor(out_e).to(dev),
            temperature=0.75, speed=a.speed)["wav"])
        sf.write(str(OUT / (tag + ".wav")), wav, 24000)
        say("  %s" % tag)
    say("")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
