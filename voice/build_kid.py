# -*- coding: utf-8 -*-
"""確定した区間で声を作り、同じ手でさらに探す（2026-09-06）。

姉を減点する探し方が当たった（候補6本すべて甥っ子）。
確定は 33,67,68,69,70,71,72,76,79,89 の10本・約36秒。
これで声を組みつつ、広がった手がかりで次の候補を出す。
"""
import glob
import io
import re
import shutil
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
TEXTD = HERE / "work" / "kid_text"
PEOPLE = HERE / "people"
OUT = HERE / "work" / "kid2"
NEXT = HERE / "work" / "find2"
SR16 = 16000
SR = 24000

KID = [33, 67, 68, 69, 70, 71, 72, 76, 79, 89]
SISTER = [43, 92]

LINES = [
    ("1_発語片",   "あのね、お鍋がね、こんなに出ててね。なんだか、そわそわするなあ。", 1.02, 0.0),
    ("2_ためらい", "あのね、えーとね……お鍋がね、こんなに……なんだろうね、そわそわするなあ。", 0.98, 0.0),
    ("3_知らせ",   "あのね、シンクがね、きれいになっててね……だれかがやってくれたのかなあ。", 1.00, 0.0),
    ("4_短い",     "あ、きた。", 1.00, 0.0),
]


def shift(x, st):
    r = 2 ** (st / 12.0)
    i = np.arange(0, len(x), r)
    i = i[i < len(x) - 1].astype(np.float32)
    lo = i.astype(np.int32)
    fr = i - lo
    return x[lo] * (1 - fr) + x[lo + 1] * fr


def main():
    import soundfile as sf
    import torch
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager

    rep = io.open(HERE / "build_kid_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    segs = np.load(TEXTD / "segs.npy")
    x = []
    for p in sorted(glob.glob(str(HERE / "work" / "kid" / "a*.wav"))):
        with wave.open(p, "rb") as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        x.append(a.astype(np.float32) / 32768.0)
    x = np.concatenate(x)

    d = PEOPLE / "p_kid"
    if d.exists():
        shutil.rmtree(d)
    (d / "clips").mkdir(parents=True)
    tot = 0.0
    for j, i in enumerate(KID):
        s, e = segs[i]
        sf.write(str(d / "clips" / ("%02d.wav" % j)), x[int(s * SR16):int(e * SR16)], SR16)
        tot += e - s
    shutil.copy2(HERE / "consent_template.md", d / "consent.md")
    say("甥っ子さんの声 %d本 / 合計 %.1f 秒" % (len(KID), tot))

    path, _, _ = ModelManager().download_model(
        "tts_models/multilingual/multi-dataset/xtts_v2")
    cfg = XttsConfig()
    cfg.load_json(str(Path(path) / "config.json"))
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)

    clips = [str(p) for p in sorted((d / "clips").glob("*.wav"))]
    g, s = model.get_conditioning_latents(audio_path=clips)
    np.save(d / "latent.npy", g.cpu().numpy())
    np.save(d / "embedding.npy", s.cpu().numpy())

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()
    parts = []
    for c in clips:
        with wave.open(c, "rb") as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        parts.append(a.astype(np.float32) / 32768.0)
        parts.append(np.zeros(3200, dtype=np.float32))
    sf.write(str(OUT / "0_本物の録音.wav"), np.concatenate(parts), SR16)

    say("")
    for tag, text, sp, pi in LINES:
        wav = np.asarray(model.inference(text, "ja", g, s,
                                         temperature=0.8, speed=sp)["wav"])
        if pi:
            wav = shift(wav, pi)
        y = np.concatenate([np.zeros(int(0.45 * SR), dtype=np.float32), wav])
        y = y / (np.abs(y).max() + 1e-9) * 0.8
        sf.write(str(OUT / (tag + ".wav")), y, SR)
        say("%-12s 「%s」" % (tag, text))

    # 広がった手がかりで、次の候補を出す
    E = np.load(TEXTD / "all_emb.npy")
    N = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    ref = N[KID].mean(axis=0)
    ref = ref / (np.linalg.norm(ref) + 1e-9)
    sis = N[SISTER].mean(axis=0)
    sis = sis / (np.linalg.norm(sis) + 1e-9)
    score = (N @ ref) - 0.6 * (N @ sis)

    lines = io.open(HERE / "kid_text.txt", encoding="utf-8").read().splitlines()[2:]
    tbl = {}
    for L in lines:
        m = re.match(r"\s*(\d+)\s", L)
        if m:
            tbl[int(m.group(1))] = L.strip()

    NEXT.mkdir(parents=True, exist_ok=True)
    for f in NEXT.glob("*"):
        f.unlink()
    say("")
    say("次の候補:")
    n = 0
    for i in [int(v) for v in np.argsort(-score)]:
        if i in KID or i in SISTER:
            continue
        if segs[i][1] - segs[i][0] < 0.8:
            continue
        a, b = segs[i]
        sf.write(str(NEXT / ("seg%03d.wav" % i)), x[int(a * SR16):int(b * SR16)], SR16)
        say("  seg%03d 近さ%.3f %s" % (i, score[i], tbl.get(i, "")))
        n += 1
        if n >= 8:
            break
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
