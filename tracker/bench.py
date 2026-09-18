# -*- coding: utf-8 -*-
"""検出・追跡・顔の処理が M1 で何fps回るかを測る（2026-09-17）。

入力は2通り：
  --src rtsp      カメラ（URL は ~/.arigato/tapo.env の TAPO_URL。副ストリームなら TAPO_URL2）
  --src 画像/動画  カメラなしで測る（画像は同じ1枚を繰り返す）

段ごとの時間を測る：
  読む   カメラから最新の1コマを取る（別の流れで読み続け、古いコマは捨てる）
  追跡   YOLO11n で人を見つけ、ByteTrack で前のコマの人とつなぐ
  顔     人の枠の中だけで SCRFD（顔の検出）→ buffalo_l（顔の数値化）
"""
import argparse
import os
import resource
import threading
import time
from pathlib import Path

import cv2
import numpy as np

SECRETS = Path.home() / ".arigato" / "tapo.env"


def rtsp_url(sub: bool) -> str:
    """鍵はコードに書かない。~/.arigato/tapo.env から読む（本人が書く）。"""
    env = {}
    for line in SECRETS.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    url = env["TAPO_URL"]
    return url.replace("/stream1", "/stream2") if sub else url


class LatestReader(threading.Thread):
    """映像を読み続け、最新の1コマだけを持つ。処理が遅れても古いコマが溜まらない。"""

    # 電波が弱いと TCP は届かなかった分を送り直し、そのあいだ映像が止まる。
    # まとめて届いた数コマは同じ瞬間なので、コマ数のわりに見える場面が増えない。
    # UDP は送り直さない代わりに、途切れずに新しい瞬間が来る（9/18 実測：
    # 同じ20秒で「別々の瞬間」が 4.2/秒 → 8.0/秒）。
    UDP = "rtsp_transport;udp|fflags;nobuffer|flags;low_delay|reorder_queue_size;0|max_delay;100000"
    TCP = "rtsp_transport;tcp"

    def __init__(self, url: str, transport: str = "udp"):
        super().__init__(daemon=True)
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = self.UDP if transport == "udp" else self.TCP
        self.cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        self.frame, self.seq, self.got = None, 0, 0
        self.lock = threading.Lock()
        self.alive = self.cap.isOpened()

    def run(self):
        while self.alive:
            ok, f = self.cap.read()
            if not ok:
                self.alive = False
                break
            with self.lock:
                self.frame, self.seq = f, self.seq + 1
                self.got += 1

    def latest(self, after: int):
        with self.lock:
            return (self.frame, self.seq) if self.seq > after else (None, after)


def load_face(providers):
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name="buffalo_l", providers=providers,
                       allowed_modules=["detection", "recognition", "landmark_3d_68"])
    app.prepare(ctx_id=0, det_size=(320, 320))
    return app


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024   # macOS はバイト


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="rtsp")
    ap.add_argument("--sub", action="store_true", help="副ストリーム（1280x720）を使う")
    ap.add_argument("--fps", type=float, default=10.0, help="処理する目標fps")
    ap.add_argument("--secs", type=float, default=60.0)
    ap.add_argument("--model", default="yolo11n.pt", help=".pt / .mlpackage / .onnx")
    ap.add_argument("--device", default="cpu", help="cpu / mps（.pt のときだけ効く）")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--face", default="coreml", choices=["off", "cpu", "coreml"])
    ap.add_argument("--resize", default="", help="画像入力の大きさ 例 2304x1296")
    ap.add_argument("--transport", default="udp", choices=["udp", "tcp"])
    a = ap.parse_args()

    from ultralytics import YOLO
    model = YOLO(a.model, task="detect")
    face = None
    if a.face != "off":
        prov = (["CoreMLExecutionProvider", "CPUExecutionProvider"] if a.face == "coreml"
                else ["CPUExecutionProvider"])
        face = load_face(prov)

    reader, still = None, None
    if a.src == "rtsp":
        reader = LatestReader(rtsp_url(a.sub), a.transport)
        if not reader.alive:
            raise SystemExit("カメラに繋がらない")
        reader.start()
    elif Path(a.src).suffix.lower() in (".jpg", ".jpeg", ".png"):
        still = cv2.imread(a.src)
        if a.resize:
            w, h = (int(v) for v in a.resize.split("x"))
            still = cv2.resize(still, (w, h))
    else:
        reader = LatestReader(a.src)
        reader.start()

    kw = dict(persist=True, classes=[0], tracker="bytetrack.yaml", imgsz=a.imgsz, verbose=False)
    if a.model.endswith(".pt"):
        kw["device"] = a.device

    period = 1.0 / a.fps
    t_track, t_face, n_person, n_face, ids = [], [], 0, 0, set()
    seq, n, warm = 0, 0, 5
    t_start = time.time()
    next_at = t_start
    while time.time() - t_start < a.secs:
        now = time.time()
        if now < next_at:
            time.sleep(next_at - now)
        next_at = max(next_at + period, time.time())
        if still is not None:
            frame = still
        else:
            frame, seq = reader.latest(seq)
            if frame is None:
                if not reader.alive:
                    break
                continue
        n += 1

        t0 = time.perf_counter()
        r = model.track(frame, **kw)[0]
        t1 = time.perf_counter()
        boxes = r.boxes
        if boxes.id is not None:
            ids.update(int(i) for i in boxes.id.tolist())
        nf = 0
        if face is not None:
            H, W = frame.shape[:2]
            for x1, y1, x2, y2 in boxes.xyxy.cpu().numpy().astype(int):
                crop = frame[max(0, y1):min(H, y2), max(0, x1):min(W, x2)]
                if crop.size:
                    nf += len(face.get(crop))
        t2 = time.perf_counter()
        n_person += len(boxes)
        n_face += nf
        if n > warm:                      # 最初の数コマは準備で遅いので数えない
            t_track.append(t1 - t0)
            t_face.append(t2 - t1)

    el = time.time() - t_start
    p = lambda xs, q: 1000 * float(np.percentile(xs, q)) if xs else float("nan")
    tot = [x + y for x, y in zip(t_track, t_face)]
    print("入力=%s 大きさ=%s モデル=%s(%s) imgsz=%d 顔=%s 目標=%.0ffps %s"
          % (a.src + (" 副" if a.sub else ""), None if frame is None else frame.shape[1::-1],
             a.model, a.device, a.imgsz, a.face, a.fps, a.transport))
    if reader is not None:
        print("  カメラから届いた=%.1ffps" % (reader.got / el))
    print("  処理できた=%.1ffps（%dコマ/%.0f秒）" % (n / el, n, el))
    print("  追跡 中央=%.0fms 9割=%.0fms / 顔 中央=%.0fms 9割=%.0fms / 合計 中央=%.0fms"
          % (p(t_track, 50), p(t_track, 90), p(t_face, 50), p(t_face, 90), p(tot, 50)))
    print("  1コマあたり 人=%.2f 顔=%.2f トラックID数=%d 最大メモリ=%.0fMB"
          % (n_person / max(n, 1), n_face / max(n, 1), len(ids), rss_mb()))


if __name__ == "__main__":
    main()
