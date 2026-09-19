# -*- coding: utf-8 -*-
"""入室1回ぶんを「トラック（滞在1回）」として記録する（2026-09-18）。

B の狙いは「判定の単位を1枚の写真から、人の滞在1回に変える」こと。
そのためにまず、実際のキッチンで次の2つを見る。

  1. 1人の滞在が、途中で切れずに1本の追跡としてつながるか
  2. その滞在のあいだに、正面の顔が何枚取れるか

残すもの（再生を本番と同じ入力にするため。引き継ぎメモ 3 の「記録」）：
  det.jsonl   顔1件ごと：時刻・コマ番号・トラックID・枠・5点・pitch/yaw/roll・
              顔幅・ボケ・検出の点数・埋め込みの置き場所
  emb.f16     埋め込み（512個の数値・float16）を順番に並べたもの
  track.jsonl 滞在1回ごと：始まり・終わり・長さ・コマ数・顔の枚数・正面の枚数
  crop/       整列済み112x112の切り抜き（顔の判定にそのまま使える形）

顔の映像は外に出さない。すべてこのマックの中だけに置く。
"""
import argparse
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from insightface.utils import face_align

from bench import LatestReader, load_face, rtsp_url

# 顔の向き。|yaw| がこれ以下なら「正面」とみなす（引き継ぎメモ 3 の出発点）
FRONT_YAW = 25.0
FRONT_PITCH = 25.0
# 追跡が切れてからこれだけ見かけなければ、その滞在は終わりとする
TRACK_GONE = 3.0


def net_now() -> str:
    """いまつながっている先を短く。ネットワークが黙って切り替わったことに、
    あとから気づけるようにする（9/19、PC側で実際に起きた）。
    SSID は場所情報の許可が無いと伏せられるので、IP と相手先で代わりにする。"""
    def sh(*cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return ""
    ip = sh("ipconfig", "getifaddr", "en0") or "なし"
    ssid = ""
    for line in sh("ipconfig", "getsummary", "en0").splitlines():
        if "SSID" in line and "BSSID" not in line:
            ssid = line.split(":", 1)[1].strip()
    cam = "届く" if sh("ping", "-c1", "-t2", "192.168.0.230") else "届かない"
    return "IP=%s SSID=%s カメラ=%s" % (ip, ssid or "不明", cam)


def blur_of(gray) -> float:
    """ボケ具合。ラプラシアン分散。小さいほどぼやけている。"""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=1800.0)
    ap.add_argument("--fps", type=float, default=10.0, help="処理する目標fps（届く分だけ処理する）")
    ap.add_argument("--sub", action="store_true", default=True)
    ap.add_argument("--main", dest="sub", action="store_false")
    ap.add_argument("--out", default="rec")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--min-person", type=int, default=60, help="この幅より小さい人は顔を探さない")
    ap.add_argument("--test-break", type=float, default=0.0,
                    help="この秒数のところで、わざと1回映像を切る（繋ぎ直しの試験）")
    a = ap.parse_args()

    out = Path(a.out) / datetime.now().strftime("%Y%m%d_%H%M")
    (out / "crop").mkdir(parents=True, exist_ok=True)
    det_f = (out / "det.jsonl").open("w", encoding="utf-8")
    trk_f = (out / "track.jsonl").open("w", encoding="utf-8")
    emb_f = (out / "emb.f16").open("wb")

    from ultralytics import YOLO
    model = YOLO("yolo11n.pt", task="detect")
    face = load_face(["CoreMLExecutionProvider", "CPUExecutionProvider"])

    reader = LatestReader(rtsp_url(a.sub), "udp")
    if not reader.alive:
        raise SystemExit("カメラに繋がらない")
    reader.start()

    tracks = {}          # tid -> 滞在の記録
    n_emb = 0
    frame_i, seq = 0, 0
    last_kick = 0.0
    period = 1.0 / a.fps
    t_start = time.time()
    next_at = t_start
    print(datetime.now().strftime("%H:%M:%S"), "記録を始める →", out,
          "/", net_now(), flush=True)
    last_net = time.time()

    def close_track(tid, now):
        t = tracks.pop(tid)
        t["end"] = t["last"]
        t["sec"] = round(t["last"] - t["start"], 1)
        trk_f.write(json.dumps(t, ensure_ascii=False) + "\n")
        trk_f.flush()
        print(datetime.now().strftime("%H:%M:%S"),
              "滞在の終わり id=%d %.1f秒 コマ=%d 顔=%d 正面=%d 最大の顔幅=%dpx"
              % (tid, t["sec"], t["frames"], t["faces"], t["front"], t["max_px"]), flush=True)

    try:
        while time.time() - t_start < a.secs:
            now = time.time()
            if now < next_at:
                time.sleep(next_at - now)
            next_at = max(next_at + period, time.time())
            frame, seq = reader.latest(seq)
            if frame is None:
                if not reader.alive:
                    print("映像が切れた（繋ぎ直せない）", flush=True)
                    break
                # 切るのは1回だけ。毎周よぶと、繋ぎ直している最中に何度も切って
                # しまう（9/19 の試験で毎秒よんでいた）。次に切ってよいのは、
                # 繋ぎ直しの待ち時間が過ぎてから。
                # 作り直す間隔も、うまくいかない間は延ばす（10秒→最大60秒）。
                # 短い間隔のまま繰り返すと、弱っている回線をこちらから叩き続ける。
                gap = min(reader.STALE * (1 + reader.reconnects // 3), 60.0)
                if reader.stalled() > reader.STALE and time.time() - last_kick > gap:
                    last_kick = time.time()
                    print(datetime.now().strftime("%H:%M:%S"),
                          "コマが %.0f 秒来ない → 読み手を作り直す" % reader.stalled(),
                          flush=True)
                    print(datetime.now().strftime("%H:%M:%S"), "  そのときの回線",
                          net_now(), flush=True)
                    reader.kick()
                    reader = reader.new_reader()
                time.sleep(0.5)
                continue
            now = time.time()
            frame_i += 1
            H, W = frame.shape[:2]

            r = model.track(frame, persist=True, classes=[0], tracker="bytetrack.yaml",
                            imgsz=a.imgsz, device="cpu", verbose=False)[0]
            b = r.boxes
            ids = [] if b.id is None else [int(i) for i in b.id.tolist()]
            xyxy = b.xyxy.cpu().numpy().astype(int)
            conf = b.conf.cpu().numpy()

            for k, tid in enumerate(ids):
                x1, y1, x2, y2 = xyxy[k]
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(W, x2), min(H, y2)
                t = tracks.get(tid)
                if t is None:
                    t = tracks[tid] = {"tid": tid, "start": now, "last": now, "frames": 0,
                                       "faces": 0, "front": 0, "max_px": 0, "best": None}
                    print(datetime.now().strftime("%H:%M:%S"), "滞在の始まり id=%d" % tid, flush=True)
                t["last"] = now
                t["frames"] += 1

                if x2 - x1 < a.min_person:
                    continue
                crop = frame[y1:y2, x1:x2]
                if crop.size == 0:
                    continue
                for f in face.get(crop):
                    fx1, fy1, fx2, fy2 = f.bbox.astype(int)
                    px = int(fx2 - fx1)
                    pitch, yaw, roll = (None, None, None) if f.pose is None \
                        else (float(f.pose[0]), float(f.pose[1]), float(f.pose[2]))
                    aligned = face_align.norm_crop(crop, f.kps, 112)
                    blur = blur_of(cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY))
                    front = (yaw is not None and abs(yaw) <= FRONT_YAW
                             and abs(pitch) <= FRONT_PITCH)
                    name = "%06d_t%03d_%d.jpg" % (frame_i, tid, px)
                    cv2.imwrite(str(out / "crop" / name), aligned)
                    emb_f.write(f.normed_embedding.astype(np.float16).tobytes())
                    det_f.write(json.dumps({
                        "t": round(now, 3), "frame": frame_i, "tid": tid,
                        "person": [int(x1), int(y1), int(x2), int(y2)],
                        "person_conf": round(float(conf[k]), 3),
                        "face": [int(fx1 + x1), int(fy1 + y1), int(fx2 + x1), int(fy2 + y1)],
                        "kps": (f.kps + [x1, y1]).round(1).tolist(),
                        "px": px, "pitch": pitch, "yaw": yaw, "roll": roll,
                        "blur": round(blur, 1), "det": round(float(f.det_score), 3),
                        "front": front, "emb": n_emb, "crop": name,
                    }, ensure_ascii=False) + "\n")
                    n_emb += 1
                    t["faces"] += 1
                    t["front"] += int(front)
                    t["max_px"] = max(t["max_px"], px)

            for tid in [i for i, t in tracks.items() if now - t["last"] > TRACK_GONE]:
                close_track(tid, now)
            if frame_i % 300 == 0:
                det_f.flush()
                print(datetime.now().strftime("%H:%M:%S"),
                      "経過 %.0f分 コマ=%d 顔=%d いま追跡中=%d"
                      % ((now - t_start) / 60, frame_i, n_emb, len(tracks)), flush=True)
            if a.test_break and now - t_start > a.test_break:
                a.test_break = 0.0             # 1回だけ
                print(datetime.now().strftime("%H:%M:%S"),
                      "【試験】わざと映像を切る（受けたコマ=%d）" % reader.got, flush=True)
                reader.kick()
                reader = reader.new_reader()
            if now - last_net > 60:        # つながっている先を1分ごとに残す
                last_net = now
                print(datetime.now().strftime("%H:%M:%S"), "回線", net_now(), flush=True)
    except KeyboardInterrupt:
        print("止めた", flush=True)
    finally:
        now = time.time()
        for tid in list(tracks):
            close_track(tid, now)
        for f in (det_f, trk_f, emb_f):
            f.close()
        el = time.time() - t_start
        reader.close()
        print(datetime.now().strftime("%H:%M:%S"),
              "記録の終わり %.0f分 処理したコマ=%d（%.1ffps） 顔=%d 繋ぎ直し=%d回 → %s"
              % (el / 60, frame_i, frame_i / el, n_emb, reader.reconnects, out), flush=True)


if __name__ == "__main__":
    main()
