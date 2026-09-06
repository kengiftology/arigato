# -*- coding: utf-8 -*-
"""集めた顔の切り抜きを取ってきて、角度と大きさの分布を出す（2026-09-06）。

    python scripts/face_angles.py            # 分布だけ見る（取得はする）
    python scripts/face_angles.py --sheet    # 一覧の画像も作る

置き場は GCS の spirit/faces_raw/。名前に測った値が入っている:
    <時刻>_<幅>px_r<起き具合×100>_<up|dn>[_edge]_<結びついたID>.jpg

見るところ:
  ・「ふつうに歩いて入る」ときの起き具合が、帯（100〜130）に入るか
  ・入らないなら、顔だけでは誰かを決められない。見上げる理由が要る
  ・弾いた顔の一覧を目で見て、弾きすぎていないか確かめる
"""
import collections
import json
import os
import re
import sys
import time
import urllib.request

SERVER = "https://arigato-3ipecjbnha-an.a.run.app"
BUCKET = "https://storage.googleapis.com/arigato-photos/"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "faces_raw")
NAME = re.compile(r"(\d+)_(\d+)px_r(\d{3}|___)_(up|dn)(_edge)?_(.+)\.jpg$")


def fetch_list():
    """置き場の一覧をサーバー経由で取る（鍵なしで見えるのは件数だけなので直接聞く）。"""
    with urllib.request.urlopen(SERVER + "/spirit/shots", timeout=60) as r:
        d = json.load(r)
    print("全画面の写真 %d枚 / 顔の切り抜き %d枚 / 記録は%s"
          % (d.get("count", 0), d.get("faces", 0),
             ("あと%.1f時間" % (d["minutes_left"] / 60)) if d.get("recording") else "止まっている"))
    return d


def parse(name):
    m = NAME.search(name)
    if not m:
        return None
    t, px, r, up, edge, pid = m.groups()
    return {"t": int(t), "px": int(px),
            "ratio": None if r == "___" else int(r) / 100.0,
            "up": up == "up", "edge": bool(edge), "pid": pid, "name": name}


def download(names):
    os.makedirs(OUT, exist_ok=True)
    got = []
    for n in names:
        local = os.path.join(OUT, os.path.basename(n))
        if not os.path.exists(local):
            try:
                urllib.request.urlretrieve(BUCKET + n, local)
            except Exception as e:
                print("取得できず", n, e)
                continue
        got.append(local)
    return got


def band(vals, lo, hi, step):
    """簡単な棒グラフ。"""
    if not vals:
        return
    hist = collections.Counter(min(int((v - lo) / step), int((hi - lo) / step)) for v in vals)
    top = max(hist.values())
    for i in range(int((hi - lo) / step) + 1):
        v = lo + i * step
        n = hist.get(i, 0)
        mark = " ←帯" if 1.00 <= v <= 1.30 else ""
        print("   %5.2f  %-30s %3d%s" % (v, "#" * int(28.0 * n / top), n, mark))


def main():
    fetch_list()
    # 一覧は GCS の公開リストから取る（バケットは公開設定）
    url = "https://storage.googleapis.com/storage/v1/b/arigato-photos/o?prefix=spirit/faces_raw/&maxResults=2000"
    items, tok = [], None
    while True:
        u = url + ("&pageToken=" + tok if tok else "")
        with urllib.request.urlopen(u, timeout=60) as r:
            d = json.load(r)
        items += [x["name"] for x in d.get("items", [])]
        tok = d.get("nextPageToken")
        if not tok:
            break
    recs = [x for x in (parse(n) for n in items) if x]
    print("読めた切り抜き: %d件" % len(recs))
    if not recs:
        print("まだ集まっていません。人が通ってから、もう一度。")
        return
    recs.sort(key=lambda x: x["t"])
    print("期間: %s 〜 %s"
          % (time.strftime("%m/%d %H:%M", time.localtime(recs[0]["t"])),
             time.strftime("%m/%d %H:%M", time.localtime(recs[-1]["t"]))))
    print()

    ups = [x for x in recs if x["up"]]
    print("[1] 帯（起き具合 1.00〜1.30）を通った顔: %d / %d (%.0f%%)"
          % (len(ups), len(recs), 100.0 * len(ups) / len(recs)))
    print("    端で切れていた: %d" % sum(1 for x in recs if x["edge"]))
    print()

    rs = [x["ratio"] for x in recs if x["ratio"]]
    print("[2] 起き具合の分布（%d件）" % len(rs))
    band(rs, 0.6, 2.0, 0.1)
    print()

    print("[3] 大きさの分布")
    for lo, hi, label in ((0, 70, "70px未満（照合できない）"),
                          (70, 120, "70〜119px（照合はできる）"),
                          (120, 9999, "120px以上（新しいIDも出せる）")):
        n = sum(1 for x in recs if lo <= x["px"] < hi)
        print("   %-28s %3d" % (label, n))
    print()

    print("[4] 時間帯ごとの「帯を通った顔」")
    byh = collections.defaultdict(lambda: [0, 0])
    for x in recs:
        h = time.strftime("%m/%d %H", time.localtime(x["t"]))
        byh[h][0] += 1
        byh[h][1] += x["up"]
    for h in sorted(byh)[-16:]:
        tot, up = byh[h]
        print("   %s時  %3d件中 %3d件が通った" % (h, tot, up))

    if "--sheet" in sys.argv:
        print()
        print("画像を取ってきます…")
        files = download([x["name"] for x in recs])
        print("%d枚を %s に置きました" % (len(files), os.path.normpath(OUT)))


if __name__ == "__main__":
    main()
