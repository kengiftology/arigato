# -*- coding: utf-8 -*-
"""出し終えた声の特徴を、まとめ直す（2026-09-06）。

split_speakers.py は基準を「遠さの下から25%」に置いていた。
今回の録音ではそれが 0.275 で、同じ人の山（0.16〜0.44）の真ん中に落ち、
同じ人を115個に割ってしまった。

遠さの分布は二山になる。左の山が同じ人どうし、右の山が別人どうし。
基準はその谷に置く。今回は 0.48〜0.52 に谷があった。

特徴の計算（GPUで数分）はやり直さず、work/emb.npy を使う。
人ごとに、聴いて確かめるための1本（preview.wav）も作る。

    python recluster.py [--thresh 0.50]
"""
import argparse
import io
import shutil
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).parent
WORK = HERE / "work"
PEOPLE = HERE / "people"
SR = 16000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresh", type=float, default=0.0, help="0なら谷を自動で探す")
    ap.add_argument("--per-person", type=int, default=12)
    ap.add_argument("--min", type=int, default=5, help="この本数未満のまとまりは捨てる")
    a = ap.parse_args()
    rep = io.open(HERE / "recluster_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    E = np.load(WORK / "emb.npy")
    keep = np.load(WORK / "seg.npy")
    with wave.open(str(WORK / "mono.wav"), "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

    d = 1.0 - (E @ E.T)
    dd = d[np.triu_indices(len(E), 1)]
    thr = a.thresh
    if thr <= 0:
        # 二山の谷を探す。0.3〜0.7 のあいだで、いちばん少ないところ
        h, edges = np.histogram(dd, bins=np.arange(0.30, 0.72, 0.02))
        thr = float(edges[int(np.argmin(h))] + 0.01)
    say("基準: %.3f（同じ人の山と別人の山の谷）" % thr)

    from sklearn.cluster import AgglomerativeClustering
    cl = AgglomerativeClustering(n_clusters=None, distance_threshold=thr,
                                 metric="cosine", linkage="average")
    lab = cl.fit_predict(E)
    uniq, cnt = np.unique(lab, return_counts=True)
    order = uniq[np.argsort(-cnt)]
    say("まとまり: %d個（大きい順 %s）"
        % (len(uniq), ", ".join(str(c) for c in sorted(cnt)[::-1][:10])))

    PEOPLE.mkdir(exist_ok=True)
    for old in PEOPLE.glob("p_*"):
        shutil.rmtree(old)
    say("")
    rank = 0
    for k in order:
        idx = [i for i, l in enumerate(lab) if l == k]
        if len(idx) < a.min:
            continue
        name = "p_%s" % chr(ord("a") + rank)
        rank += 1
        d2 = PEOPLE / name
        (d2 / "clips").mkdir(parents=True)
        # まとまりの中心に近い順。端のものは別人が混じっている可能性がある
        c = E[idx].mean(axis=0)
        idx.sort(key=lambda i: -float(E[i] @ c))
        total, span, pieces = 0.0, [], []
        for j, i in enumerate(idx[: a.per_person]):
            s, e = keep[i]
            seg = x[int(s * SR):int(e * SR)]
            sf.write(str(d2 / "clips" / ("%02d.wav" % j)), seg, SR)
            total += e - s
            span.append(s)
            if j < 4:
                pieces.append(seg[: 4 * SR])
                pieces.append(np.zeros(int(0.5 * SR), dtype=np.float32))
        sf.write(str(d2 / "preview.wav"), np.concatenate(pieces), SR)
        shutil.copy2(HERE / "consent_template.md", d2 / "consent.md")
        say("  %s … %3d本のうち%2d本 / %3.0f秒 / 録音の %.1f〜%.1f 時間目"
            % (name, len(idx), min(len(idx), a.per_person), total,
               min(span) / 3600, max(span) / 3600))
    say("")
    say("people/<人>/preview.wav を聴いて、同じ人・別人を確かめてください")
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
