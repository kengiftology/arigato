# -*- coding: utf-8 -*-
"""顔の判定が動いていないことを押さえる門（2026-09-26・研究トークA）。

9/26 に「通り過ぎた人」を記録に残す変更を入れるとき、進行役から条件が付いた：
**記録だけ。判定は一切変えない。誰と判定するか・登録するかの結果が1ミリも動かないこと。**
その約束を、口約束ではなく**落ちる試験**にしたもの。ここが落ちたら本番には入らない。

押さえているのは2つ。
  1. **顔を数値にする道すじ**（切り出し→整列→512個の数値）が同じ答えを返すこと
  2. **判定の線**（確定・保留・覚える・登録する）が、決めた値のままであること

9/29 の見切りまで線を動かさないと決めたので、2 は特に効く。
値を変える必要が出たときは、**この試験の数字も一緒に直す**（直すこと自体が「線を動かした」記録になる）。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photos", "face_probe.jpg")


def test_顔を数値にする道すじが同じ答えを返す():
    import cv2
    from server import face

    img = cv2.imread(PROBE)
    assert img is not None, "見本の写真が読めません: " + PROBE
    v = face.embed(img)
    assert v is not None and len(v) == 512
    a = np.asarray(v, dtype=np.float32)
    assert abs(float(np.linalg.norm(a)) - 1.0) < 1e-4          # 長さ1にそろえている
    # 2026-09-26 に測った値。道すじが変わるとここが動く。
    head = [-0.058902, 0.043347, -0.054775, -0.032192, 0.02507]
    assert [round(float(x), 5) for x in a[:5]] == [round(x, 5) for x in head]
    # 自分どうしの近さは 1.0。重心の作り方が変わるとここが動く。
    assert abs(face.score_frames([v], {"pX": [v]})["pX"] - 1.0) < 1e-4


def test_判定の線が決めた値のまま():
    from server.routers import spirit as sp

    # 9/29 の見切りが終わるまで動かさないと決めた線（2026-09-25 時点）
    assert sp.FACE_CONFIRM_FRONT == 0.40      # 正面で「この人だ」と決める線
    assert sp.FACE_CONFIRM_TILT == 0.40       # 傾いた顔で決める線
    assert sp.FACE_HOLD == 0.25               # ここから下は保留にもしない
    assert sp.FACE_LEARN_SIM == 0.45          # 覚えに足してよい近さ
    assert sp.FACE_CORE_SIM == 0.40           # 核と離れすぎた顔は覚えない
    assert sp.FACE_ONE_PX == 140              # 1コマで決めてよい顔の幅
    assert sp.FACE_ONE_SIM == 0.46            # そのときに要る近さ
    assert sp.FACE_ENROLL_MAX_PX == 290       # これより大きい顔から新しい人を作らない
    assert sp.FACE_NEW_MIN_N == 3             # 新しい人に要るコマ数
    assert sp.FACE_NEW_SPAN == 5.0            # そのコマが広がっていてほしい秒数
    assert sp.FACE_MIN_FRAMES == 2
    assert sp.FACE_MIN_FRAMES_SMALL == 3
    assert sp.MIN_PRESENCE == 30.0            # これ未満は「通りすがり」


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
