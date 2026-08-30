"""顔を「誰か」ではなく「どのID」として見分ける。

設計の原則（本人指定・2026-08-31）:
  ・実名は一切扱わない。初めての顔には匿名IDを自動発行するだけ
  ・保存するのは特徴量（数値の並び）のみ。数値から顔画像は復元できない
  ・分からない時は「分からない」と答える（誤って別人のキャラを出さない）

処理は3段:
  1) 顔を見つける      … OpenCVの顔検出（軽い・コンパイル不要）
  2) 特徴量に変換する  … ONNXの顔認識モデル（起動時にダウンロード）
  3) 照合する          … 登録済みの特徴量との距離を測り、近ければ同一人物
"""
import logging
import os
import threading

import numpy as np

logger = logging.getLogger("face")

# 顔認識モデル（ArcFace系・軽量）。初回起動時に取得してローカルに置く。
_MODEL_URL = "https://github.com/onnx/models/raw/main/validated/vision/body_analysis/arcface/model/arcfaceresnet100-8.onnx"
_MODEL_PATH = "/tmp/arcface.onnx"
_SIM_THRESHOLD = 0.42        # これ以上似ていたら同一人物とみなす（低いほど緩い）
_MIN_FACE_PX = 60            # これより小さく写った顔は「見えなかった」扱い

_session = None
_detector = None
_lock = threading.Lock()


def _get_detector():
    """OpenCV同梱の顔検出器。追加ダウンロード不要で動く。"""
    global _detector
    if _detector is None:
        import cv2
        path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        _detector = cv2.CascadeClassifier(path)
    return _detector


def _get_session():
    """特徴量モデル。無ければ取得してから読み込む（初回だけ時間がかかる）。"""
    global _session
    if _session is not None:
        return _session
    with _lock:
        if _session is not None:
            return _session
        if not os.path.exists(_MODEL_PATH):
            import urllib.request
            logger.info("downloading face model...")
            urllib.request.urlretrieve(_MODEL_URL, _MODEL_PATH)
        import onnxruntime
        _session = onnxruntime.InferenceSession(
            _MODEL_PATH, providers=["CPUExecutionProvider"])
    return _session


def detect_face(image_bytes: bytes, rotate: int = 0):
    """写真から一番大きい顔を切り出す。見つからなければ None。
    rotate＝カメラの取り付け向きの補正（度・90/180/270）。"""
    import cv2
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    if rotate:
        k = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
             270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(rotate)
        if k is not None:
            img = cv2.rotate(img, k)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faces = _get_detector().detectMultiScale(gray, 1.1, 5, minSize=(_MIN_FACE_PX, _MIN_FACE_PX))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])     # 一番大きい顔＝一番近い人
    m = int(w * 0.2)                                        # 少し広めに切る（髪や輪郭も入れる）
    x0, y0 = max(0, x - m), max(0, y - m)
    x1, y1 = min(img.shape[1], x + w + m), min(img.shape[0], y + h + m)
    return img[y0:y1, x0:x1]


def embed(face_img) -> list | None:
    """顔の切り抜き → 特徴量（512個の数値）。この数値から顔は復元できない。"""
    try:
        import cv2
        blob = cv2.resize(face_img, (112, 112)).astype(np.float32)
        blob = (blob - 127.5) / 128.0
        blob = np.transpose(blob, (2, 0, 1))[np.newaxis, :]
        sess = _get_session()
        out = sess.run(None, {sess.get_inputs()[0].name: blob})[0][0]
        v = out / (np.linalg.norm(out) + 1e-9)              # 長さを1に揃える（距離を比べやすく）
        return [float(x) for x in v]
    except Exception as e:
        logger.warning("embed failed: %s", e)
        return None


def match(vec: list, known: dict) -> tuple:
    """既知の特徴量たちと比べて (ID, 似ている度) を返す。
    知らない顔なら (None, 最大類似度)。knownは {id: [特徴量, ...]}。"""
    if not vec or not known:
        return None, 0.0
    v = np.asarray(vec, dtype=np.float32)
    best_id, best = None, 0.0
    for pid, vecs in known.items():
        for kv in vecs:
            s = float(np.dot(v, np.asarray(kv, dtype=np.float32)))   # 内積＝似ている度
            if s > best:
                best, best_id = s, pid
    return (best_id, best) if best >= _SIM_THRESHOLD else (None, best)
