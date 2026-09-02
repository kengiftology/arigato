# -*- coding: utf-8 -*-
"""Tapoの映像をクラウドの地霊へ橋渡しする（2026-09-01 / 2026-09-02改訂）。

Tapoは家のネットワークの中にいるので、クラウドから直接は取りに行けない。
ラズパイが中に立って、静止画を1枚ずつ取り出して送る。

送り先は今までと同じ /spirit/frame。クラウド側は写真1枚で
「人が居るか・誰か・散らかり具合」をすべて判断する（2026-08-31の統合）ので、
橋渡しは撮って送るだけでよい。

■ 2026-09-02の改訂：見張りと判断を分ける
  旧方式は30秒おきに1枚ずつクラウドへ送っていた。これだと人が入ってきても
  最悪30秒気づかない。かといって短くすると、誰も居ない台所を何百回も
  クラウドに見せることになる（費用も上限も無駄になる）。

  そこで、ラズパイ自身がずっと映像を見張るようにした。
  映像をごく小さな白黒（80x45）で流しっぱなしにして、前のコマとの差を測る。
  差が出た＝何かが動いた時だけ、大きな写真を撮ってクラウドへ送る。

  見張りは足し算と引き算だけなので、外部の部品も要らず、CPUもほぼ食わない。
  クラウドへ送るのは「動いた時」と「たまの定時報告」だけになる。
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

# 見張りは副ストリーム（1280x720）を使う。主ストリーム（2304x1296）を
# 流しっぱなしにするとラズパイのCPUを1コア食い切ってしまうため。
# 写真としてクラウドへ送るのは、今までどおり主ストリームの1枚。
WATCH_URL = os.environ.get("TAPO_WATCH_URL", CAM_URL.replace("/stream1", "/stream2"))

# 見張り用の小さな映像（人かどうかまでは分からないが、動いたことは分かる）
WATCH_W, WATCH_H, WATCH_FPS = 80, 45, 2
FRAME_BYTES = WATCH_W * WATCH_H

GAP_BUSY = 3.0        # 動きがある間、クラウドへ送る最短間隔
GAP_HEARTBEAT = 300.0 # 何も起きなくても、これだけ経ったら1枚送る（定時報告）
GAP_ERROR = 15.0      # 失敗した時
STILL_HOLD = 20.0     # 最後に動いてからこの秒数は「まだ居る」とみなす
CALIB_FRAMES = 20     # 最初のこの枚数で、その部屋の「静かさ」を測る


def watch_stream():
    """小さな白黒映像を流しっぱなしにする。読むのは生のバイト列。"""
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-i", WATCH_URL,
         "-vf", "fps=%d,scale=%d:%d,format=gray" % (WATCH_FPS, WATCH_W, WATCH_H),
         "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def diff(a: bytes, b: bytes) -> float:
    """2コマの違い。0なら全く同じ、大きいほど何かが動いた。"""
    total = 0
    for i in range(0, FRAME_BYTES, 3):        # 3画素に1つ見れば十分（軽くする）
        d = a[i] - b[i]
        total += d if d >= 0 else -d
    return total / (FRAME_BYTES / 3.0)


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


def report(jpg: bytes, why: str) -> None:
    """1枚送って、意味のある返事だけ記録する。"""
    try:
        res = send(jpg)
    except Exception as e:
        print("send failed:", e, flush=True)
        return
    if res.get("person") or res.get("judged"):
        print(time.strftime("%H:%M:%S"), why, res, flush=True)


def main():
    print("tapo bridge start ->", SERVER, flush=True)
    while True:
        proc = watch_stream()
        prev = None
        quiet, seen = 0.0, 0             # その部屋の「静かな時の揺らぎ」を測る
        last_sent = 0.0
        last_move = -1e9
        try:
            while True:
                buf = proc.stdout.read(FRAME_BYTES)
                if not buf or len(buf) < FRAME_BYTES:
                    print("watch stream ended", flush=True)
                    break
                now = time.time()
                if prev is not None:
                    d = diff(prev, buf)
                    if seen < CALIB_FRAMES:          # 最初は黙って基準を測る
                        quiet = max(quiet, d)
                        seen += 1
                        if seen == CALIB_FRAMES:
                            print("静かな時の揺らぎ = %.2f / しきい値 = %.2f"
                                  % (quiet, max(quiet * 2.5, 1.5)), flush=True)
                    elif d > max(quiet * 2.5, 1.5):
                        if now - last_move >= STILL_HOLD:    # 静けさが破られた瞬間
                            print(time.strftime("%H:%M:%S"),
                                  "動きあり %.2f" % d, flush=True)
                        last_move = now
                prev = buf

                busy = (now - last_move) < STILL_HOLD
                gap = GAP_BUSY if busy else GAP_HEARTBEAT
                if seen >= CALIB_FRAMES and now - last_sent >= gap:
                    last_sent = now
                    jpg = grab()
                    if jpg is None:
                        time.sleep(GAP_ERROR)
                        continue
                    report(jpg, "動き" if busy else "定時")
        except Exception as e:
            print("watch failed:", e, flush=True)
        finally:
            try:
                proc.kill()
            except Exception:
                pass
        time.sleep(GAP_ERROR)            # 映像が切れたら少し待って繋ぎ直す


if __name__ == "__main__":
    main()
