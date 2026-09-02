# -*- coding: utf-8 -*-
"""Tapoの映像をクラウドの地霊へ橋渡しする（2026-09-01 / 2026-09-02改訂）。

Tapoは家のネットワークの中にいるので、クラウドから直接は取りに行けない。
ラズパイが中に立って、静止画を1枚ずつ取り出して送る。

送り先は今までと同じ /spirit/frame。クラウド側は写真1枚で
「人が居るか・誰か・散らかり具合」をすべて判断する（2026-08-31の統合）ので、
橋渡しは撮って送るだけでよい。

■ 見張りと判断を分ける
  30秒おきに1枚ずつ送っていた頃は、人が入ってきても最悪30秒気づかなかった。
  かといって短くすると、誰も居ない台所を何百回もクラウドに見せることになる。

  そこでラズパイ自身がずっと映像を見張る。ごく小さな白黒（80x45）で流し、
  前のコマとの差を測る。差が出た＝何かが動いた時だけクラウドへ送る。
  足し算と引き算だけなので外部の部品も要らず、CPUもほとんど食わない。

■ 映像は開いたままにする（2026-09-02の改訂）
  写真が要るたびにffmpegを起こしていたが、1枚あたり2.8秒かかっていた。
  中身は接続の手続きで、解像度を落としても縮まらない（主2.87秒／副2.78秒）。
  そこで同じ1本の接続から、見張り用の小さな白黒と、送る用のJPEGを
  同時に出しつづける。写真が要るときはできあがったものを読むだけになる。
"""
import json
import os
import subprocess
import time
import urllib.request

CAM_URL = os.environ.get("TAPO_URL", "rtsp://thankU:39Kitchen@192.168.0.230:554/stream1")
SERVER = os.environ.get("SPIRIT_SERVER", "https://arigato-3ipecjbnha-an.a.run.app")
KEY = os.environ.get("SPIRIT_KEY", "06dc964a3cdd2c4f4c5c1d8592dff543")
SHOT = "/tmp/tapo.jpg"          # 常に最新の1枚が置かれる（ffmpegが書き替えつづける）

# 副ストリーム（1280x720）から取る。主ストリーム（2304x1296）を流しっぱなしに
# するとラズパイのCPUを1コア食い切ってしまう。顔は720pでも十分な大きさで写る。
WATCH_URL = os.environ.get("TAPO_WATCH_URL", CAM_URL.replace("/stream1", "/stream2"))

WATCH_W, WATCH_H, WATCH_FPS = 80, 45, 2      # 見張り用の小さな白黒
FRAME_BYTES = WATCH_W * WATCH_H
SHOT_FPS = 1                                 # 送る用のJPEGを作り替える速さ
SHOT_MAX_AGE = 4.0                           # これより古い1枚は使わない

GAP_BUSY = 3.0        # 動きがある間、クラウドへ送る最短間隔
GAP_HEARTBEAT = 300.0 # 何も起きなくても、これだけ経ったら1枚送る（定時報告）
GAP_ERROR = 15.0      # 失敗した時
STILL_HOLD = 20.0     # 最後に動いてからこの秒数は「まだ居る」とみなす
CALIB_FRAMES = 20     # 最初のこの枚数で、その部屋の「静かさ」を測る


def watch_stream():
    """1本の接続から2つを同時に出す。

    ひとつは見張り用の小さな白黒（標準出力へ流しっぱなし）。
    もうひとつは送る用のJPEG（同じ名前を上書きしつづける）。
    カメラは梁に逆さに吊ってあるので、JPEGはここで180度回しておく。
    見張り用は回さない（差を測るだけなので向きは関係ない）。"""
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-i", WATCH_URL,
         "-an", "-vf", "fps=%d,scale=%d:%d,format=gray" % (WATCH_FPS, WATCH_W, WATCH_H),
         "-f", "rawvideo", "pipe:1",
         "-an", "-vf", "fps=%d,hflip,vflip" % SHOT_FPS, "-q:v", "3",
         "-update", "1", "-y", SHOT],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def diff(a: bytes, b: bytes) -> float:
    """2コマの違い。0なら全く同じ、大きいほど何かが動いた。"""
    total = 0
    for i in range(0, FRAME_BYTES, 3):        # 3画素に1つ見れば十分（軽くする）
        d = a[i] - b[i]
        total += d if d >= 0 else -d
    return total / (FRAME_BYTES / 3.0)


def grab() -> bytes | None:
    """できあがっている最新の1枚を読む。新しくなければNone。"""
    try:
        if time.time() - os.path.getmtime(SHOT) > SHOT_MAX_AGE:
            return None                       # 映像が止まっている
        with open(SHOT, "rb") as f:
            data = f.read()
        return data if data[-2:] == b"\xff\xd9" else None   # 書きかけは捨てる
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


def report(jpg: bytes, why: str) -> bool:
    """1枚送って、意味のある返事だけ記録する。人が写っていたらTrue。"""
    try:
        res = send(jpg)
    except Exception as e:
        print("send failed:", e, flush=True)
        return False
    if res.get("person") or res.get("judged"):
        print(time.strftime("%H:%M:%S"), why, res, flush=True)
    return bool(res.get("person"))


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
                        continue                     # 次のコマで撮り直せばよい
                    if report(jpg, "動き" if busy else "定時"):
                        # 座って動かない人を見失わないため、クラウドが人を
                        # 見たと言う間は「まだ居る」として見張りを続ける。
                        # 動きだけを頼りにすると、じっとしている人が消える。
                        last_move = now
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
