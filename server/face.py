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
# 2026-09-12：本人が710枚を手で仕分けた答えの表で測り直した。
# 0.42 は「同じ人を別人にする」が71%（人ごとに同じ重み）で、35時間に8つのIDが
# 生まれていた原因そのものだった。0.30 は、Tシャツのプリントが人に結びつくのを
# 20枚中1枚以下に抑えられる範囲でいちばん低いところ。
# 本番と同じ形（8枚おぼえる・2コマまとめる・4人）での実測：
#   いま(SFace・引き伸ばし・0.42・5枚・1コマ) 本人71.7% 別人13.2% 新ID15.2%
#   これ(mbf・揃える・0.30・8枚・2コマ)       本人96.6% 別人 1.4% 新ID 2.0%
_SIM_THRESHOLD = 0.30        # これ以上似ていたら同一人物とみなす（低いほど緩い）
# 大きさの線は3本ある。一本だけにしていた頃は「小さい顔」が
# その場で捨てられ、人が居たことさえ残らなかった（実測：9/3〜9/5の3日間で
# 顔が取れたのは3回）。分けると、小さい顔を「大きく撮り直せ」の合図に使える。
_MIN_FACE_PX = 45            # これ未満は顔として扱わない（見えてもいない）
_MATCH_FACE_PX = 70          # これ未満は照合しない。特徴が出ず、別人に結びつく
# 新しい匿名IDを出すのは、これ以上の大きさで写ったときだけ。
# 小さい顔は特徴が曖昧で、同じ人でも一致度が0.42前後まで落ちる。
# 実際にそれで同じ人が2つのIDに割れた（2026-09-03・0.407）。
_ENROLL_FACE_PX = 100        # 2026-09-10: 立って入ってくる人の正面は101〜138px（2回の入室の実測）。120では1回の入室に2コマしか残らない
# 顔だと言い切る自信の下限。0.60では誰も居ない台所の棚を83x83の顔と見て
# 匿名IDを発行してしまった（2026-09-02・確信度ちょうど0.60）。
# 本物の顔は実測で0.76〜0.94に出るので、この間に線を引く。
_DET_CONF = 0.80

# 顔が「起きている」か「うつむいている」か。
#
# 2026-09-05の実測（同一人物・同じカメラ・同じ距離）:
#   顔を上げたとき   一致度の中央 0.616 / 他人を本人と誤る 0%
#   うつむいたとき   一致度の中央 0.506 / 同じ人でも60%しか通らない
#   シンク作業中     一致度の中央 0.399
# しかも「同じ人の正面」と「同じ人のうつむき」は最大0.270で、まったく
# 一致しない。うつむき顔を記憶に混ぜると、そのIDは誰でも吸い込む網になる
# （実測で他人の86〜93%を吸い込んだ）。だから記憶にも照合にも使わない。
#
# 起き具合は、目と目の幅に対する「目から口までの縦の長さ」で測る。
# 実測では 正面15件が1.03〜1.22、うつむき13件は0.87〜1.85に散り、
# この帯で正面15/15を通し、うつむき11/13を弾けた。
_RATIO_JUNK = 3.0            # これを超える「起き具合」は人の顔ではない（物の誤検出）
_UP_MIN, _UP_MAX = 0.80, 1.30   # 2026-09-10: 天井から見るので前を向いた顔でも縮む。立って入ってくる正面の実測 0.97〜1.29
# 2026-09-12 夜：照合（誰かを決める）と登録（新しいIDを作る）で帯を分けた。
# 真上のカメラでは正面がめったに撮れず、この帯で1日のコマの48%を捨てていた
# （実測：9/12 の123コマ中59コマが「うつむき」）。仕分け済み710枚で測り直すと、
#   帯        使える人の顔   鍋が通る   本人に正しく結びつく
#   〜1.30      498枚       13/24        71.8%
#   〜1.70      545枚       13/24        74.3%   ← 鍋は増えず、精度は上がる
#   〜2.00      569枚       13/24        69.4%
#   〜2.50      610枚       23/24        68.7%   ← ここで鍋が流れ込む
# 照合は1.70まで許す。登録は1.30のまま（鍋が覚えに入ると、そのIDが網になる）。
_UP_MAX_MATCH = 1.70

_session = None
_detector = None
_last_size = [0]             # 直前に切り出した顔の幅
_lock = threading.Lock()


_MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
_YUNET_PATH = os.path.join(_MODEL_DIR, "yunet.onnx")
_SFACE_PATH = os.path.join(_MODEL_DIR, "sface.onnx")
_MBF_PATH = os.path.join(_MODEL_DIR, "mbf.onnx")

# ArcFace系が前提にしている、112x112の中の目・鼻・口の置き場所。
# 顔をここへ合わせてから渡す（2026-09-12）。以前は切り抜きを112x112に
# 引き伸ばしているだけで、目の位置が毎回ずれていた。
# 実測（同じ写真・同じモデル）：引き伸ばしだけ 本人80.5%・別人11.7%
#                               揃える         本人87.2%・別人 6.0%
_ARC_TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                          [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)
_mbf = None
_align_det = None


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


def _get_mbf():
    """顔の特徴量を作る器（ArcFace系 w600k_mbf・512次元）。同梱モデルを読むだけ。

    2026-09-12：SFace（128次元）から差し替えた。本人が仕分けた710枚での実測で、
    本人と分かる率・別人と間違える率・鍋やTシャツのプリントを弾く力の
    すべてで上回り、しかも1枚あたり33.5ms→18.7msと速い。
    さらに重いモデル（r50 225ms・r100 1231ms）も測ったが、r100でも
    本人97.1%・別人0.6%と差はわずかで、逆に鍋を23/24も人だと思った。"""
    global _mbf
    if _mbf is None:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = 2
        _mbf = ort.InferenceSession(_MBF_PATH, so, providers=["CPUExecutionProvider"])
    return _mbf


def _align(face_img, pts=None):
    """切り抜きの中の目・鼻・口を、112x112の決まった置き場所に合わせる。

    pts（検出のときに取れた5点）が渡されればそれを使う。無ければ取り直す。
    どちらもできなければ None。そのときは引き伸ばしに落とす。"""
    import cv2
    global _align_det
    if pts is not None:
        M, _ = cv2.estimateAffinePartial2D(np.asarray(pts, dtype=np.float32),
                                           _ARC_TEMPLATE, method=cv2.LMEDS)
        if M is not None:
            return cv2.warpAffine(face_img, M, (112, 112), borderValue=0)
    if _align_det is None:
        _align_det = cv2.FaceDetectorYN.create(_YUNET_PATH, "", (320, 320), 0.5, 0.3, 5000)
    h, w = face_img.shape[:2]
    s = max(1.0, 160.0 / max(h, w))                  # 小さい切り抜きは拡大してから見る
    im = cv2.resize(face_img, (int(w * s), int(h * s))) if s > 1.0 else face_img
    pad = int(0.25 * max(im.shape[:2]))              # 端が切れているので余白を足す
    im = cv2.copyMakeBorder(im, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
    _align_det.setInputSize((im.shape[1], im.shape[0]))
    _, fs = _align_det.detect(im)
    if fs is None or len(fs) == 0:
        return None
    f = max(fs, key=lambda f: f[2] * f[3])
    M, _ = cv2.estimateAffinePartial2D(f[4:14].reshape(5, 2).astype(np.float32),
                                       _ARC_TEMPLATE, method=cv2.LMEDS)
    if M is None:
        return None
    return cv2.warpAffine(im, M, (112, 112), borderValue=0)


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
        ratio = _up_ratio(f)
        x, y, w, h = (int(v) for v in f[:4])
        if w < _MIN_FACE_PX:
            continue                                        # 小さすぎる顔は見えなかった扱い
        if ratio is not None and ratio > _RATIO_JUNK:
            # 目と口の位置関係が人の顔としてあり得ない（実測：横顔でも2.5前後まで）。
            # 2026-09-10 16:55〜17:06、待つ向きの物が起き具合4〜32の「顔」として
            # 毎分出て、弾かれはしたが「人が居る」扱いになり、滞在が切れず人感の
            # 記録が10秒おきに揺れた。顔として数えない。
            continue
        m = int(w * 0.2)                                    # 少し広めに切る（髪や輪郭も入れる）
        x0, y0 = max(0, x - m), max(0, y - m)
        x1, y1 = min(img.shape[1], x + w + m), min(img.shape[0], y + h + m)
        # 画面の端にかかった枠は、顔の半分しか写っていない。
        # 実測では、棚のボトルを確信度0.809で顔と見た1件も、
        # 口と顕だけになって一致度が上がらなかった1件も、どちらも
        # 端にかかっていた。確信度では分けられない（本物も0.807〜0.91）。
        edge = (x < 2 or y < 2
                or x + w > img.shape[1] - 2 or y + h > img.shape[0] - 2)
        # どこに写っていたかも返す。動かない「顔」は置いてある物なので、
        # 場所が変わらないことを手がかりに人と分ける（2026-09-05）。
        # 目・鼻・口の5点は、ここで既に取れている。切り抜きの中の座標に直して
        # 持ち回る。これが無いと、特徴量を作るたびに顔検出をもう一度走らせる
        # ことになり、1枚197msかかった（持ち回れば20ms・2026-09-12の実測）。
        pts = (f[4:14].reshape(5, 2).astype(np.float32) - np.float32([x0, y0])).tolist()
        out.append({"crop": img[y0:y1, x0:x1], "px": w, "edge": edge, "pts": pts,
                    "pos": (x + w // 2, y + h // 2), "ratio": ratio,
                    # up＝照合に使ってよい／front＝新しいIDを出してよい正面らしさ
                    "up": ratio is not None and _UP_MIN <= ratio <= _UP_MAX_MATCH,
                    "front": ratio is not None and _UP_MIN <= ratio <= _UP_MAX})
    return out


def _up_ratio(f):
    """顔の起き具合。目と目の幅に対する、目から口までの縦の長さ。

    YuNetが返す5点（右目・左目・鼻・右口角・左口角）から測る。
    うつむいた顔を上から見ると、この比が帯から外れる。"""
    try:
        p = np.asarray(f[4:14], dtype=np.float32).reshape(5, 2)
        eye_w = float(np.linalg.norm(p[0] - p[1]))
        if eye_w < 1.0:
            return None
        eye = (p[0] + p[1]) / 2.0
        mouth = (p[3] + p[4]) / 2.0
        return float(np.linalg.norm(mouth - eye)) / eye_w
    except Exception:
        return None


def detect_face(image_bytes: bytes, rotate: int = 0):
    """一番大きい顔だけを切り出す（診断用に残してある）。"""
    fs = detect_faces(image_bytes, rotate)
    if not fs:
        return None
    _last_size[0] = fs[0]["px"]
    return fs[0]["crop"]


def embed(face_img, pts=None) -> list | None:
    """顔の切り抜き → 特徴量（512個の数値）。この数値から顔は復元できない。

    2026-09-12：目・鼻・口を決まった置き場所に合わせてから渡す。
    揃えないと同じ数字は出ない（測定はすべて揃えた状態で取った）。"""
    try:
        import cv2
        import numpy as np
        img = _align(face_img, pts)
        if img is None:
            img = cv2.resize(face_img, (112, 112))   # 取り直せなければ引き伸ばしに落とす
        x = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32)
        x = ((x - 127.5) / 127.5).transpose(2, 0, 1)[None]
        s = _get_mbf()
        v = s.run(None, {s.get_inputs()[0].name: x})[0][0]
        v = v / (np.linalg.norm(v) + 1e-9)          # 長さを1に揃える（距離を比べやすく）
        return [float(x) for x in v]
    except Exception as e:
        logger.warning("embed failed: %s", e)
        return None


def match(vec: list, known: dict) -> tuple:
    """既知の特徴量たちと比べて (ID, 似ている度) を返す。
    知らない顔なら (None, 最大類似度)。knownは {id: [特徴量, ...]}。"""
    return match_frames([vec], known)


def match_frames(vecs: list, known: dict) -> tuple:
    """1回の入室ぶん（数コマ）をまとめて、誰かを1つ決める（2026-09-12）。

    人ごとに「そのコマが一番似た覚え」を出し、コマ全体で平均する。
    1コマずつ決めると、たまたま似た他人のコマに引っぱられる。

    実測（mbf・揃える・8枚おぼえる・4人）:
      1コマで決める  本人88.3% ／ 別人と間違える7.8%
      2コマまとめる  本人95.6% ／ 別人と間違える1.2%   ← 6分の1になる
      3コマまとめる  本人96.2% ／ 別人と間違える1.2%
    3コマ以上はほとんど変わらないので、2コマ揃えば決めてよい。"""
    vs = [np.asarray(v, dtype=np.float32) for v in (vecs or []) if v]
    if not vs or not known:
        return None, 0.0
    best_id, best = None, 0.0
    for pid, kvs in known.items():
        ks = [np.asarray(kv, dtype=np.float32) for kv in kvs]
        # 覚えの長さが違うものは、モデルを差し替える前の古い覚え。混ぜると壊れる。
        ks = [k for k in ks if k.shape == vs[0].shape]
        if not ks:
            continue
        per = [max(float(np.dot(v, k)) for k in ks) for v in vs]     # 内積＝似ている度
        s = sum(per) / len(per)
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
