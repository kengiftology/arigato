# -*- coding: utf-8 -*-
"""ゼミの録音を、話している人ごとに切り分ける。

手順:
  1) 音声を16kHzのモノラルに直す（録音まるごと）
  2) 声が出ている区間を拾う（音量で判定。会議録音なので十分）
  3) 録音全体から満遍なく区間を選び、「声の特徴」を出す
  4) 特徴どうしの近さの分布を測り、そこからまとめる基準を決める
  5) people/<仮の名前>/clips/ に分けて置く

■ 最初の版で外したこと（2026-09-05）
  最初の1時間だけを見て8人に分けたが、聴いてみると全部同じ2人だった。
  ゼミは教授がずっと居て学生が1時間ごとに入れ替わる構造なので、
  1時間だけ見れば2人しか出てこない。録音全体から拾わないと人は増えない。
  まとめる基準（0.35）も勘で置いていて厳しすぎ、同じ人を細かく割っていた。
  基準は毎回、特徴どうしの近さの分布から決める。

人ごとにフォルダを分けるのは、あとで「やっぱりなしで」と言われたときに
そのフォルダを消すだけで済むようにするため（README参照）。

使い方:
    python split_speakers.py <音声ファイル> [--people 6]
"""
import argparse
import io
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
WORK = HERE / "work"
PEOPLE = HERE / "people"

SR = 16000
WIN = 0.03                 # 音量を見る窓（秒）
MIN_SEG = 3.0              # これより短い声は使わない。短いと特徴がぶれる
MAX_SEG = 8.0              # 長すぎる区間は切る（途中で話者が変わりうる）
GAP = 0.5                  # これだけ黙ったら区間の切れ目
MAX_EMBED = 700            # 特徴を出す区間の上限（録音全体から満遍なく選ぶ）


def to_wav(src: Path, dst: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                    "-ac", "1", "-ar", str(SR), "-sample_fmt", "s16", str(dst)],
                   check=True)


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
    floor = np.percentile(rms, 20)
    thr = max(floor * 3.0, rms.mean() * 0.35)
    voiced = rms > thr
    out, start, gap = [], None, 0.0
    for i, v in enumerate(voiced):
        t = i * WIN
        if v:
            if start is None:
                start = t
            gap = 0.0
        elif start is not None:
            gap += WIN
            if gap >= GAP:
                end = t - gap
                while end - start > MAX_SEG:
                    out.append((start, start + MAX_SEG))
                    start += MAX_SEG
                if end - start >= MIN_SEG:
                    out.append((start, end))
                start = None
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--people", type=int, default=0, help="人数（0なら分布から決める）")
    ap.add_argument("--per-person", type=int, default=12, help="1人あたり残す区間の数")
    a = ap.parse_args()

    WORK.mkdir(exist_ok=True)
    rep = io.open(HERE / "split_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + "\n")
        rep.flush()

    wav = WORK / "mono.wav"
    if not wav.exists() or wav.stat().st_mtime < Path(a.src).stat().st_mtime:
        say("音声を16kHzモノラルに直しています（録音まるごと）…")
        to_wav(Path(a.src), wav)
    x = read_wav(wav)
    say("長さ %.1f 時間" % (len(x) / SR / 3600))

    segs = segments(x)
    say("声が出ている区間: %d本（合計 %.1f 分）"
        % (len(segs), sum(e - s for s, e in segs) / 60))
    if len(segs) < 30:
        say("区間が少なすぎます。録音そのものを確かめてください")
        rep.close()
        return 1

    # 録音全体から満遍なく選ぶ。前半だけ見ると、入れ替わった人が出てこない。
    if len(segs) > MAX_EMBED:
        step = len(segs) / MAX_EMBED
        segs = [segs[int(i * step)] for i in range(MAX_EMBED)]
        say("全体から %d本 を等間隔で選びました" % len(segs))

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
        if (i + 1) % 100 == 0:
            say("  %d / %d" % (i + 1, len(segs)))
    E = np.stack(embs)
    E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    say("特徴を出せた区間: %d本" % len(E))
    np.save(WORK / "emb.npy", E)
    np.save(WORK / "seg.npy", np.array(keep))

    # 近さの分布を見る。同じ人どうしは近く、別人どうしは遠い。
    # その二山の谷が、まとめる基準になる。
    d = 1.0 - (E @ E.T)
    iu = np.triu_indices(len(E), 1)
    dd = d[iu]
    say("\n特徴どうしの遠さ（0で同一・大きいほど別人）")
    for q in (5, 10, 25, 50, 75, 90):
        say("  下から%2d%% … %.3f" % (q, np.percentile(dd, q)))

    say("\n似ている声どうしをまとめています…")
    from sklearn.cluster import AgglomerativeClustering
    if a.people > 0:
        cl = AgglomerativeClustering(n_clusters=a.people, metric="cosine",
                                     linkage="average")
        say("  人数を %d と指定" % a.people)
    else:
        thr = float(np.percentile(dd, 25))      # 4分の1が「同じ人」に入る想定
        cl = AgglomerativeClustering(n_clusters=None, distance_threshold=thr,
                                     metric="cosine", linkage="average")
        say("  分布から基準を %.3f に決めました" % thr)
    lab = cl.fit_predict(E)
    uniq, cnt = np.unique(lab, return_counts=True)
    order = uniq[np.argsort(-cnt)]
    say("まとまり: %d個（大きい順の区間数 %s）"
        % (len(uniq), ", ".join(str(c) for c in sorted(cnt)[::-1][:12])))

    say("\n人ごとに書き出しています…")
    PEOPLE.mkdir(exist_ok=True)
    for old in PEOPLE.glob("p_*"):
        shutil.rmtree(old)
    for rank, k in enumerate(order):
        idx = [i for i, l in enumerate(lab) if l == k]
        if len(idx) < 5:
            continue
        name = "p_%s" % chr(ord("a") + rank)
        d2 = PEOPLE / name
        (d2 / "clips").mkdir(parents=True)
        idx.sort(key=lambda i: -(keep[i][1] - keep[i][0]))
        total, span = 0.0, []
        for j, i in enumerate(idx[: a.per_person]):
            s, e = keep[i]
            sf.write(str(d2 / "clips" / ("%02d.wav" % j)), x[int(s * SR):int(e * SR)], SR)
            total += e - s
            span.append(s)
        shutil.copy2(HERE / "consent_template.md", d2 / "consent.md")
        say("  %s … %d本 / %.0f秒 / 録音の %.1f〜%.1f 時間目に登場"
            % (name, min(len(idx), a.per_person), total,
               min(span) / 3600, max(span) / 3600))

    say("\nできました。people/ の下に人ごとのフォルダがあります。")
    say("登場する時間帯がばらけていれば、入れ替わった別々の人を拾えています。")
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
