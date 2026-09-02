# -*- coding: utf-8 -*-
"""部屋を見渡して人を探す（2026-09-02）。

固定の画角では、そこに居ない人を見逃す。実際に「椅子は空・本人は別のPCの前」
という状況で顔を捉えられなかった。Tapoは首を振れるので、
何点かを順に見て回り、顔が取れたところで止める。
"""
import sys
import time

import cv2
from onvif import ONVIFCamera

HOST, PORT = "192.168.0.230", 2020
USER, PWD = "thankU", "39Kitchen"
RTSP = "rtsp://thankU:39Kitchen@192.168.0.230:554/stream1"

# 見て回る向き（横, 縦）。-1〜1の範囲。部屋の左・正面・右を見る
SCAN_POINTS = [(-0.6, -0.3), (0.0, -0.3), (0.6, -0.3), (0.0, 0.2)]


def connect():
    cam = ONVIFCamera(HOST, PORT, USER, PWD)
    media = cam.create_media_service()
    token = media.GetProfiles()[0].token
    return cam.create_ptz_service(), token


def look(ptz, token, x, y):
    """指定の向きへ首を向ける。"""
    ptz.AbsoluteMove({"ProfileToken": token,
                      "Position": {"PanTilt": {"x": x, "y": y}}})
    time.sleep(2.5)                       # 首が動き終わるのを待つ


def grab():
    cap = cv2.VideoCapture(RTSP, cv2.CAP_FFMPEG)
    ok, fr = cap.read()
    cap.release()
    return fr if ok else None


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, ".")
    from server import face
    ptz, token = connect()
    for x, y in SCAN_POINTS:
        look(ptz, token, x, y)
        fr = grab()
        if fr is None:
            print("  (%.1f, %.1f) 映像取得できず" % (x, y))
            continue
        ok, buf = cv2.imencode(".jpg", fr)
        crop = face.detect_face(buf.tobytes(), rotate=0)
        if crop is not None:
            print("  (%.1f, %.1f) 顔あり %s" % (x, y, crop.shape))
            return (x, y)
        print("  (%.1f, %.1f) 顔なし" % (x, y))
    print("部屋のどこにも顔は見つからず")
    return None


if __name__ == "__main__":
    main()
