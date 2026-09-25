# -*- coding: utf-8 -*-
"""向きの守りの試験（2026-09-25）。

なぜ要るか：9/23〜24、カメラの向きがずれて見回りの写真が全部「違う景色」で弾かれ、
20時間50分 記録が止まった。弾く仕組み自体は正しく働いていた。
ここで確かめるのは「正しいものを通し、違うものを弾く」という、その仕組みの性質。

写真は `bridge/sink_ref.jpg`（もともとリポジトリにある見本）だけを使い、
ずらした版・暗くした版はその場で作る。新しい写真は置かない。
"""
import sys, os, io as _io
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server.routers.spirit as sp

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
REF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "bridge", "sink_ref.jpg")
OTHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photos", "other_view.jpg")


def _jpg(im):
    ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 92])
    assert ok
    return buf.tobytes()


def _base():
    im = cv2.imread(REF)
    assert im is not None, "見本の写真が読めない: %s" % REF
    return im


def _shifted(px):
    im = _base()
    M = np.float32([[1, 0, px], [0, 1, 0]])
    return _jpg(cv2.warpAffine(im, M, (im.shape[1], im.shape[0]), borderMode=cv2.BORDER_REFLECT))


@pytest.fixture(autouse=True)
def _use_ref(monkeypatch):
    ref = open(REF, "rb").read()
    monkeypatch.setattr(sp, "read_object", lambda n: ref if n == sp.AIM_REF_OBJ else None)
    sp._last_good["jpg"], sp._last_good["ref_at"] = None, 0.0
    sp._state_cache = {}
    sp._ref_cache[0], sp._ref_cache[1] = 0.0, None
    yield
    sp._state_cache = {}


def test_同じ写真は通る():
    ok, conf, shift = sp._view_ok(open(REF, "rb").read())
    assert ok is True
    assert conf > 0.9
    assert abs(shift[0]) <= 2 and abs(shift[1]) <= 2


def test_少しのずれなら通る():
    # 9/25 の実測：正しい向きでも数px〜百数十pxのずれは出る
    ok, conf, _ = sp._view_ok(_shifted(20))
    assert ok is True, "20pxのずれで弾いてはいけない（確かさ %s）" % conf


def test_同じ景色がずれただけなら通し_ずれを正しく報せる():
    """確かさは「カメラが動いたか」ではなく「同じ景色か」を見ている（2026-09-25 に測り直した）。

    同じ写真を横にずらすと、確かさは高いまま（600pxで0.42）でずれの数字が正しく出る。
    カメラが本当に別の場所を向いたときは、写る中身が変わるので確かさが落ちる（実測0.007〜0.013）。
    **「ずれすぎて比べられない」を弾くのは、確かさではなく `SHIFT_MAX_PX` のほう。**守りは2段構え。
    ここを取り違えて「確かさが大きなずれを弾く」と思い込んでいた。"""
    ok, conf, shift = sp._view_ok(_shifted(600))
    assert ok is True, "同じ景色がずれただけなら、確かさは通す（%s）" % conf
    assert abs(shift[0] - 600) < 40, "ずれの数字が出ていない: %s" % shift
    assert abs(shift[0]) > sp.SHIFT_MAX_PX, "この大きさは SHIFT_MAX_PX 側で弾かれる"


def test_本物の別の向きは弾く():
    # 9/24 に撮った「待つ向き」。中身がまるで違うので確かさが落ちる（実測 0.007）
    ok, conf, _ = sp._view_ok(open(OTHER, "rb").read())
    assert ok is False, "別の向きの写真を通してはいけない（確かさ %s）" % conf
    assert conf < sp.AIM_MIN_CONF


def test_確かさが低いときずれの数字を当てにしない():
    # 9/24 の実測：別方向の写真のほうが「ずれ」が小さく出た（-32px・確かさ0.01）。
    # ずれだけを見ると逆の結論になるので、確かさで判断していることを確かめる
    noise = _jpg(np.random.RandomState(0).randint(0, 255, _base().shape, dtype=np.uint8))
    ok, conf, _ = sp._view_ok(noise)
    assert ok is False
    assert conf < sp.AIM_MIN_CONF


def test_見本が無いときは止めない():
    # 見本が読めないことを理由に判定を止めると、気づかないまま記録が消える
    sp.read_object = lambda n: None
    ok, conf, shift = sp._view_ok(open(REF, "rb").read())
    assert ok is True and conf is None


def test_区画ごとの線が効く():
    sp._state_cache = {"zone_cfg": {"シンク": {"aim": {"min_conf": 0.999}}}}
    ok, conf, _ = sp._view_ok(_shifted(200), "シンク")
    assert ok is False, "区画ごとの線が使われていない（確かさ %s）" % conf


def test_ずれと確かさを毎回測れる():
    # 9/24 の停止は「ずれを記録に残していなかったので、いつから何pxか追えなかった」
    shift, conf = sp._aim_now(_shifted(40))
    assert shift is not None and conf is not None
    assert abs(shift[0]) > 10
