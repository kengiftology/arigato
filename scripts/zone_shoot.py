# -*- coding: utf-8 -*-
"""区画の見本を撮る／候補を並べて撮る（2026-09-27）。

**見回りとまったく同じ道順で撮る。**これが唯一の要点である。
別の撮り方（手でカメラを向けて撮る・アプリの画面から撮る）で撮った見本は、
見回りの撮る1枚と**永久に合わない**。9/26〜27 に2度これをやって、
「区画の判定が1件も通らない」状態を丸一日つくった。

    python scripts/zone_shoot.py 出力先 水切り -0.80 -0.90
    python scripts/zone_shoot.py 出力先 コンロ候補1 -0.40 0 コンロ候補2 -0.50 0

撮った写真は**必ず人が目で見て**「その場所が写っているか」を確かめる。
機械は「この見本は間違っている」とは言えない。**間違った見本を登録すると、
そこから先ずっと『合っている』と出続けて、誰も気づけない。**

確かめたら、その1枚をそのまま登録する：
    python scripts/zone_shoot.py 出力先 --register 水切り 出力先/水切り.jpg
"""
import os
import sys
import urllib.parse
import urllib.request
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cam


def register(zone: str, path: str) -> None:
    d = open(path, "rb").read()
    u = "%s/spirit/zone/ref?zone=%s&key=%s" % (cam.SRV, urllib.parse.quote(zone), cam.key())
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        u, data=d, method="POST", headers={"Content-Type": "image/jpeg"}), timeout=90))
    print("%s の見本として登録しました: %s" % (zone, json.dumps(r, ensure_ascii=False)[:120]))


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    if sys.argv[2] == "--register":
        return register(sys.argv[3], sys.argv[4])
    args = sys.argv[2:]
    jobs = [(args[i], float(args[i + 1]), float(args[i + 2])) for i in range(0, len(args), 3)]
    fx, fy = cam.aim_fix()
    if fx or fy:
        print("向きの補正 %+.2f/%+.2f を足して振ります（見回りと同じ）" % (fx, fy))
    cam.pause(True)
    try:
        for name, x, y in jobs:
            jpg = cam.shoot(x + fx, y + fy)
            p = os.path.join(out, "%s.jpg" % name)
            open(p, "wb").write(jpg)
            print("撮りました %-10s %+.2f/%+.2f → %s（%dKB）"
                  % (name, x, y, p, len(jpg) // 1024), flush=True)
    finally:
        cam.go_home()
        cam.pause(False)
    print("\n**この写真を人が目で見て、その場所が写っていることを確かめてください。**")
    print("確かめたら： python scripts/zone_shoot.py %s --register <区画名> <写真>" % out)


if __name__ == "__main__":
    main()
