# -*- coding: utf-8 -*-
"""**首を振り終えてから、映像が入れ替わるまで何秒かかるか**を測る（2026-09-27）。

新しい場所では必ず最初にこれを測る。**カメラは「止まりました」と返すが、映像はまだ
前の景色のまま**である。キッチンの実測では、カメラは4.2秒で止まったと言い、
映像が入れ替わったのは**9〜10秒後**だった。4秒で撮っていた見回りは、
**振り向く途中の景色**を送りつづけ、クラウドは正しく「違う景色」と弾いていた。
丸半日かかって、ここに行き着いた。

    python scripts/aim_settle.py -0.03 -1.00        # その向きへ振って測る

出た秒数に余裕を2秒足して、`bridge/tapo_bridge.py` の SETTLE_AFTER_MOVE に入れる。
**いちばん大きく振る向き**（入り口から遠い区画）で測ること。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cam
import server.routers.spirit as sp


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("使い方: python scripts/aim_settle.py <よこ> <たて>  例) -0.03 -1.00")
    x, y = float(sys.argv[1]), float(sys.argv[2])
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
    import sweep
    cam.pause(True)
    try:
        sweep.look(*cam.HOME_POSE)
        time.sleep(6)
        t0 = time.time()
        sweep.look(x, y)
        moved = time.time()
        print("カメラが「止まった」と返すまで %.1f 秒" % (moved - t0), flush=True)
        seq = []
        while time.time() - moved < secs:
            try:
                d = cam.raw(3.0)
            except SystemExit:
                break
            if not seq or seq[-1][1] != d:
                seq.append((round(time.time() - moved, 1), d))
            time.sleep(0.5)
        last = seq[-1][1]
        print("届いた1枚 %d 枚。**最後の1枚と一致したところから先が、本当の景色**。" % len(seq))
        first_ok = None
        for sec, d in seq:
            r = sp._frame_match(last, d, (0.0, 1.0))[2]
            mark = "◎ 同じ景色" if r >= 0.5 else ("△" if r >= 0.1 else "× まだ前の景色")
            if r >= 0.5 and first_ok is None:
                first_ok = sec
            print("  振り終えて %5.1f 秒  確かさ %.3f  %s" % (sec, r, mark), flush=True)
        if first_ok is not None:
            print("\n**映像が入れ替わったのは %.1f 秒後。**待ち時間は %.0f 秒にしてください"
                  "（実測＋余裕2秒）。" % (first_ok, first_ok + 2))
        else:
            print("\n最後まで入れ替わりませんでした。映像が止まっている可能性があります。")
    finally:
        cam.go_home()
        cam.pause(False)


if __name__ == "__main__":
    main()
