# -*- coding: utf-8 -*-
"""かわいい声を材料に入れる（2026-09-07）。

一晩かけて分かったこと：
  大人の声を、高くしても、縮めても、抑揚を写しても、かわいくはならない。
  出てくるのは「加工された大人」。かわいらしさは声優が小さい体の声を
  演じているもので、加工で後から足せるものではなかった。

だから、かわいい声そのものを材料に入れる。
ずんだもん・四国めたん・後鬼（VOICEVOX）に何文か喋らせ、その音から
「声の特徴」を取り、4人（Cは半分）の特徴と混ぜて、1つの声にする。

  キャラ 100% … 人は入っていない（比較用）
  キャラ  70% / 人 30%
  キャラ  50% / 人 50%

音を重ねるのではなく、特徴を混ぜてから1本の声として喋らせる。
VOICEVOX の音を材料にしているのでクレジットが要る。
"""
import io
import json
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
OUT = HERE / "work" / "chars_people"
CHARS = HERE / "work" / "chars"
API = "http://127.0.0.1:50021"

SPEAKERS = {"ずんだもん": 3, "四国めたん": 2, "後鬼": 27}
PEOPLE_W = {"p_a": 1.0, "p_b": 1.0, "p_c": 0.5, "p_d": 1.0}
SEED_TEXT = [
    "あのね、きょうはね、おそとがとてもあかるいの。",
    "だれかがきれいにしてくれたのかなあ。うれしいなあ。",
    "おなべ、ここにあるよ。つかったらもどしてね。",
    "なんだかね、そわそわするなあ。",
    "あ、きた。まってたんだよ。",
]
LINES = [("1_なのり", "あのね、わたし、きっちんちゃん。"),
         ("4_そわそわ", "なんだかね、そわそわするなあ。")]


def vv(text, sid, path):
    u = API + "/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": sid})
    with urllib.request.urlopen(urllib.request.Request(u, method="POST"), timeout=30) as r:
        q = json.load(r)
    u2 = API + "/synthesis?" + urllib.parse.urlencode({"speaker": sid})
    req = urllib.request.Request(u2, method="POST", data=json.dumps(q).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        path.write_bytes(r.read())


def wmean(vs, ws):
    ns = [np.linalg.norm(v) for v in vs]
    m = sum(w * v / (n + 1e-9) for v, n, w in zip(vs, ns, ws)) / sum(ws)
    return m / (np.linalg.norm(m) + 1e-9) * float(np.mean(ns))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    CHARS.mkdir(parents=True, exist_ok=True)
    rep = io.open(HERE / "chars_people_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    import torch
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager
    path, _, _ = ModelManager().download_model("tts_models/multilingual/multi-dataset/xtts_v2")
    cfg = XttsConfig()
    cfg.load_json(str(Path(path) / "config.json"))
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)

    say("キャラの声を材料にしています…")
    clat, cemb = [], []
    for name, sid in SPEAKERS.items():
        clips = []
        for i, t in enumerate(SEED_TEXT):
            p = CHARS / ("%s_%d.wav" % (name, i))
            if not p.exists():
                vv(t, sid, p)
            clips.append(str(p))
        g, s = model.get_conditioning_latents(audio_path=clips)
        clat.append(g.cpu().numpy())
        cemb.append(s.cpu().numpy())
        say("  %s … %d文" % (name, len(clips)))
    char_l, char_e = wmean(clat, [1] * 3), wmean(cemb, [1] * 3)

    ks = [k for k in PEOPLE_W if (PEOPLE / k / "latent.npy").exists()]
    ppl_l = wmean([np.load(PEOPLE / k / "latent.npy") for k in ks], [PEOPLE_W[k] for k in ks])
    ppl_e = wmean([np.load(PEOPLE / k / "embedding.npy") for k in ks], [PEOPLE_W[k] for k in ks])
    say("人の声: " + ", ".join("%s×%.1f" % (k, PEOPLE_W[k]) for k in ks))

    for f in OUT.glob("*.wav"):
        f.unlink()
    say("")
    for tag, text in LINES:
        for label, wp in (("キャラ100", 0.0), ("キャラ70_人30", 0.3), ("キャラ50_人50", 0.5),
                          ("キャラ40_人60", 0.6), ("キャラ30_人70", 0.7)):
            g = wmean([char_l, ppl_l], [1 - wp, wp])
            s = wmean([char_e, ppl_e], [1 - wp, wp])
            wav = np.asarray(model.inference(text, "ja", torch.tensor(g).to(dev),
                                             torch.tensor(s).to(dev),
                                             temperature=0.75, speed=1.05)["wav"])
            sf.write(str(OUT / ("%s_%s.wav" % (tag, label))), wav, 24000)
            say("  %s_%s" % (tag, label))
    say("")
    say("材料: VOICEVOX:ずんだもん / VOICEVOX:四国めたん / VOICEVOX:後鬼 ＋ 4人")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
