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
# 大きさの線は3本ある。一本だけにしていた頃は「小さい顔」が
# その場で捨てられ、人が居たことさえ残らなかった（実測：9/3〜9/5の3日間で
# 顔が取れたのは3回）。分けると、小さい顔を「大きく撮り直せ」の合図に使える。
_MIN_FACE_PX = 45            # これ未満は顔として扱わない（見えてもいない）
_MATCH_FACE_PX = 70          # これ未満は照合しない。特徴が出ず、別人に結びつく
# 新しい匿名IDを出すのは、これ以上の大きさで写ったときだけ。
# 小さい顔は特徴が曖昧で、同じ人でも一致度が0.42前後まで落ちる。
# 実際にそれで同じ人が2つのIDに割れた（2026-09-03・0.407）。
_ENROLL_FACE_PX = 120
# 顔だと言い切る自信の下限。0.60では誰も居ない台所の棚を83x83の顔と見て
# 匿名IDを発行してしまった（2026-09-02・確信度ちょうど0.60）。
# 本物の顔は実測で0.76〜0.94に出るので、この間に線を引く。
_DET_CONF = 0.80

_session = None
_detector = None
_last_size = [0]             # 直前に切り出した顔の幅
_lock = threading.Lock()


_MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
_YUNET_PATH = os.path.join(_MODEL_DIR, "yunet.onnx")
_SFACE_PATH = os.path.join(_MODEL_DIR, "sface.onnx")


def _get_detector():
    """顔検出器（YuNet）。モデルは同梱（2026-09-02）。

    以前は起動時にダウンロードしていたが、クラウド上で取得に失敗し、
    顔が一切検出されない状態になった。手元では動くのに本番だけ落ちるため
    原因の特定に時間を要した。取りに行かず、持っていく方式に改めた。
    2026-08-31: 旧来のカスケード方式は本物0件・床の木目を10件誤検出したためYuNetへ交換。"""
    global _detector
    if _detector is None:
        import cv2
        _detector = cv2.FaceDetectorYN.create(_YUNET_PATH, "", (320, 320), _DET_CONF, 0.3, 5000)
    return _detector


def _get_session():
    """顔の特徴量を作る器（SFace）。同梱モデルを読むだけで、外部取得はしない。"""
    global _session
    if _session is None:
        import cv2
        _session = cv2.FaceRecognizerSF.create(_SFACE_PATH, "")
    return _session


MAX_FACES = 4                # 一度に見る人数の上限


def detect_faces(image_bytes: bytes, rotate: int = 0) -> list:
    """写真に写っている顔を全部切り出す。大きい順に返す。

    以前は一番大きい1つだけを返し、残りを捨てていた。2人居ても1人しか
    識別できず、「この時間帯に居たのは誰と誰か」が作れなかった。

    返すのは [{"crop": 切り抜き, "px": 顔の幅, "edge": 画面の端にかかっているか}, ...]。
    幅は、新しいIDを出してよいかの判断に使う（小さい顔からは卵を作らない）。"""
    import cv2
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return []
    if rotate:
        k = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
             270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(rotate)
        if k is not None:
            img = cv2.rotate(img, k)
    det = _get_detector()
    det.setInputSize((img.shape[1], img.shape[0]))
    _, faces = det.detect(img)
    if faces is None or len(faces) == 0:
        return []
    out = []
    for f in sorted(faces, key=lambda f: -(f[2] * f[3]))[:MAX_FACES]:
        x, y, w, h = (int(v) for v in f[:4])
        if w < _MIN_FACE_PX:
            continue                                        # 小さすぎる顔は見えなかった扱い
        m = int(w * 0.2)                                    # 少し広めに切る（髪や輪郭も入れる）
        x0, y0 = max(0, x - m), max(0, y - m)
        x1, y1 = min(img.shape[1], x + w + m), min(img.shape[0], y + h + m)
        # 画面の端にかかった枠は、顔の半分しか写っていない。
        # 実測では、棚のボトルを確信度0.809で顔と見た1件も、
        # 口と顕だけになって一致度が上がらなかった1件も、どちらも
        # 端にかかっていた。確信度では分けられない（本物も0.807〜0.91）。
        edge = (x < 2 or y < 2
                or x + w > img.shape[1] - 2 or y + h > img.shape[0] - 2)
        out.append({"crop": img[y0:y1, x0:x1], "px": w, "edge": edge})
    return out


def detect_face(image_bytes: bytes, rotate: int = 0):
    """一番大きい顔だけを切り出す（診断用に残してある）。"""
    fs = detect_faces(image_bytes, rotate)
    if not fs:
        return None
    _last_size[0] = fs[0]["px"]
    return fs[0]["crop"]


def embed(face_img) -> list | None:
    """顔の切り抜き → 特徴量（128個の数値）。この数値から顔は復元できない。"""
    try:
        import cv2
        import numpy as np
        rec = _get_session()
        img = cv2.resize(face_img, (112, 112))
        v = rec.feature(img)[0]
        v = v / (np.linalg.norm(v) + 1e-9)          # 長さを1に揃える（距離を比べやすく）
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


def last_face_px() -> int:
    """直前に切り出した顔の幅。新しいIDを出してよいかの判断に使う。"""
    return _last_size[0]


def big_enough_to_enroll(px: int | None = None) -> bool:
    """新しい匿名IDを出してよい大きさか。"""
    return (px if px is not None else _last_size[0]) >= _ENROLL_FACE_PX


def big_enough_to_match(px: int | None = None) -> bool:
    """登録済みの人と照合してよい大きさか。

    これ未満でも「顔が見えている」こと自体は確かなので、
    人が居る合図には使う。ただし誰かを決めるには使わない。"""
    return (px if px is not None else _last_size[0]) >= _MATCH_FACE_PX
