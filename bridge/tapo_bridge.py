# -*- coding: utf-8 -*-
"""Tapoの映像をクラウドの地霊へ橋渡しする（2026-09-01）。

Tapoは家のネットワークの中にいるので、クラウドから直接は取りに行けない。
ラズパイが中に立って、静止画を1枚ずつ取り出して送る。

送り先は今までと同じ /spirit/frame。クラウド側は写真1枚で
「人が居るか・誰か・散らかり具合」をすべて判断する（2026-08-31の統合）ので、
橋渡しは撮って送るだけでよい。

間隔は状況で変える：人が居る間はこまめに（顔を捉えたい）、
無人なら控えめに（同じ景色を何度見ても同じ答えしか返らない）。
"""
import json
import os
import subprocess
import time
import urllib.request

CAM_URL = os.environ.get("TAPO_URL", "rtsp://thankU:39Kitchen@192.168.0.230:554/stream1")
SERVER = os.environ.get("SPIRIT_SERVER", "https://arigato-3ipecjbnha-an.a.run.app")
KEY = os.environ.get("SPIRIT_KEY", "06dc964a3cdd2c4f4c5c1d8592dff543")
SHOT = "/tmp/tapo.jpg"

GAP_BUSY = 3.0        # 人が居る間（顔を捉えたいので短く）
GAP_IDLE = 30.0       # 誰も居ない間
GAP_ERROR = 15.0      # 失敗した時


def grab() -> bytes | None:
    """RTSPから静止画を1枚。失敗したらNone。"""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-rtsp_transport", "tcp",
             "-i", CAM_URL, "-frames:v", "1", "-q:v", "3", SHOT],
            check=True, timeout=25, capture_output=True)
        with open(SHOT, "rb") as f:
            return f.read()
    except Exception as e:
        print("grab failed:", e, flush=True)
        return None


def send(jpg: bytes) -> dict:
    """クラウドへ送って判断を受け取る。"""
    req = urllib.request.Request(
        SERVER + "/spirit/frame", data=jpg,
        headers={"Content-Type": "image/jpeg", "X-Upload-Key": KEY})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode())


def main():
    print("tapo bridge start ->", SERVER, flush=True)
    gap = GAP_IDLE
    while True:
        jpg = grab()
        if jpg is None:
            time.sleep(GAP_ERROR)
            continue
        try:
            res = send(jpg)
        except Exception as e:
            print("send failed:", e, flush=True)
            time.sleep(GAP_ERROR)
            continue
        # 人が居た（顔が取れた／AIが人を見た）なら間隔を詰める
        busy = bool(res.get("person")) or res.get("why") in ("person_seen", "person_in_frame")
        if res.get("person") or res.get("judged"):
            print(time.strftime("%H:%M:%S"), res, flush=True)
        gap = GAP_BUSY if busy else GAP_IDLE
        time.sleep(gap)


if __name__ == "__main__":
    main()
