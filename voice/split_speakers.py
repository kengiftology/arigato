# -*- coding: utf-8 -*-
"""ゼミの録音を、話している人ごとに切り分ける。

手順:
  1) 音声を16kHzのモノラルに直す
  2) 声が出ている区間を拾う（音量で判定。会議録音なので十分）
  3) 区間ごとに「声の特徴」を出す
  4) 似ている特徴どうしをまとめる → 人ごとの束になる
  5) people/<仮の名前>/clips/ に分けて置く

人ごとにフォルダを分けるのは、あとで「やっぱりなしで」と言われたときに
そのフォルダを消すだけで済むようにするため（README参照）。

使い方:
    python split_speakers.py <音声ファイル> [--people 6] [--minutes 90]
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
WORK = HERE / "work"
PEOPLE = HERE / "people"

SR = 16000
WIN = 0.03                 # 音量を見る窓（秒）
MIN_SEG = 1.2              # これより短い声は使わない（特徴が出ない）
MAX_SEG = 6.0              # 長すぎる区間は切る（途中で話者が変わりうる）
GAP = 0.4                  # これだけ黙ったら区間の切れ目


def to_wav(src: Path, dst: Path, minutes: int) -> None:
    """16kHzモノラルに直す。minutes>0ならその長さだけ。"""
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    if minutes > 0:
        cmd += ["-t", str(minutes * 60)]
    cmd += ["-ac", "1", "-ar", str(SR), "-sample_fmt", "s16", str(dst)]
    subprocess.run(cmd, check=True)


def read_wav(p: Path) -> np.ndarray:
    with wave.open(str(p), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return a.astype(np.float32) / 32768.0


def segments(x: np.ndarray) -> list:
    """声が出ている区間を拾う。返すのは (開始秒, 終了秒) の並び。

    静かな部分の音量を基準にして、そこから持ち上がったところを声とみなす。
    部屋の暗騒音は録音ごとに違うので、決め打ちにせず毎回測る。"""
    n = int(WIN * SR)
    frames = x[: len(x) // n * n].reshape(-1, n)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    floor = np.percentile(rms, 20)                  # 静かな側の代表値
    thr = max(floor * 3.0, rms.mean() * 0.35)
    voiced = rms > thr
    out, start, gap = [], None, 0
    for i, v in enumerate(voiced):
        t = i * WIN
        if v:
            if start is None:
                start = t
            gap = 0
        elif start is not None:
            gap += WIN
            if gap >= GAP:
                end = t - gap
                while end - start > MAX_SEG:        # 長すぎる区間は割る
                    out.append((start, start + MAX_SEG))
                    start += MAX_SEG
                if end - start >= MIN_SEG:
                    out.append((start, end))
                start = None
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--people", type=int, default=0, help="人数（0なら自動で決める）")
    ap.add_argument("--minutes", type=int, default=90, help="先頭から何分ぶん使うか（0で全部）")
    ap.add_argument("--per-person", type=int, default=12, help="1人あたり残す区間の数")
    a = ap.parse_args()

    WORK.mkdir(exist_ok=True)
    rep = io.open(HERE / "split_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + "\n")
        rep.flush()

    wav = WORK / "mono.wav"
    say("音声を16kHzモノラルに直しています…")
    to_wav(Path(a.src), wav, a.minutes)
    x = read_wav(wav)
    say("長さ %.1f 分" % (len(x) / SR / 60))

    segs = segments(x)
    say("声が出ている区間: %d本（合計 %.1f 分）"
        % (len(segs), sum(e - s for s, e in segs) / 60))
    if len(segs) < 20:
        say("区間が少なすぎます。音量の基準か、録音そのものを確かめてください")
        rep.close()
        return 1

    say("\n声の特徴を出しています（GPUを使います）…")
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

    tmp = WORK / "seg.wav"
    embs, keep = [], []
    for i, (s, e) in enumerate(segs):
        sf.write(str(tmp), x[int(s * SR):int(e * SR)], SR)
        try:
            _g, sp = model.get_conditioning_latents(audio_path=[str(tmp)])
        except Exception:
            continue
        embs.append(sp.cpu().numpy().flatten())
        keep.append((s, e))
        if (i + 1) % 50 == 0:
            say("  %d / %d" % (i + 1, len(segs)))
    E = np.stack(embs)
    E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    say("特徴を出せた区間: %d本" % len(E))

    say("\n似ている声どうしをまとめています…")
    from sklearn.cluster import AgglomerativeClustering
    if a.people > 0:
        cl = AgglomerativeClustering(n_clusters=a.people, metric="cosine",
                                     linkage="average")
    else:
        cl = AgglomerativeClustering(n_clusters=None, distance_threshold=0.35,
                                     metric="cosine", linkage="average")
    lab = cl.fit_predict(E)
    uniq, cnt = np.unique(lab, return_counts=True)
    order = uniq[np.argsort(-cnt)]
    say("まとまり: %d個（区間数 %s）" % (len(uniq), ", ".join(str(c) for c in sorted(cnt)[::-1])))

    say("\n人ごとに書き出しています…")
    PEOPLE.mkdir(exist_ok=True)
    for rank, k in enumerate(order):
        idx = [i for i, l in enumerate(lab) if l == k]
        if len(idx) < 4:
            continue                                # ほとんど喋っていない＝使わない
        name = "p_%s" % chr(ord("a") + rank)
        d = PEOPLE / name
        if d.exists():
            shutil.rmtree(d)
        (d / "clips").mkdir(parents=True)
        # 長い区間から順に、決めた本数だけ残す
        idx.sort(key=lambda i: -(keep[i][1] - keep[i][0]))
        total = 0.0
        for j, i in enumerate(idx[: a.per_person]):
            s, e = keep[i]
            sf.write(str(d / "clips" / ("%02d.wav" % j)), x[int(s * SR):int(e * SR)], SR)
            total += e - s
        shutil.copy2(HERE / "consent_template.md", d / "consent.md")
        say("  %s … %d本 / 合計 %.1f 秒" % (name, min(len(idx), a.per_person), total))

    say("\nできました。people/ の下に人ごとのフォルダがあります。")
    say("中の clips/*.wav を聴いて、同じ人の声だけが入っているか確かめてください。")
    say("混ざっていたら --people で人数を指定してやり直せます。")
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
