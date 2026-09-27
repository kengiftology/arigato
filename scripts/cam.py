# -*- coding: utf-8 -*-
"""新しい場所を足すときに使う、カメラまわりの共通部分（2026-09-27）。

ここにあるのは3つだけ。
  ・見回りを止める／戻す（設置作業の間、勝手に動かれると困る）
  ・**見回りとまったく同じ道順で1枚撮る**（入り口 → その向き → 待つ → 止まってから撮る）
  ・鍵を読む（会話にも記録にも出さない）

**「見回りと同じ道順」がいちばん大事。**別の撮り方で撮った見本は、
見回りの撮る1枚と永久に合わない（2026-09-26〜27 に2度、これで丸一日を失った）。
"""
import io
import os
import re
import sys
import time
import urllib.request

BRIDGE = os.environ.get("BRIDGE_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge")
sys.path.insert(0, BRIDGE)
SHOT = os.environ.get("TAPO_SHOT", "C:/tmp/tapo.jpg")   # 橋渡しが書き替えつづける1枚
SRV = os.environ.get("SPIRIT_SERVER", "https://arigato-3ipecjbnha-an.a.run.app")
KEYFILE = os.environ.get("SPIRIT_KEY_FILE",
                         os.path.expanduser("~/.arigato/keys_2026-09-16.txt"))
HOME_POSE = (-0.32, 0.30)      # 待つ向き（入り口）。見回りは必ずここから出る
WAIT = float(os.environ.get("VIEW_SETTLE", "12"))   # 振ってから待つ秒数（実測9〜10秒＋余裕）


def key() -> str:
    """鍵を読む。**中身は絶対に表示しない。**"""
    k = os.environ.get("SPIRIT_KEY")
    if k:
        return k
    m = re.search(r"new, 2026-09-16\)=([0-9a-f]+)",
                  io.open(KEYFILE, encoding="utf-8", errors="ignore").read())
    if not m:
        raise SystemExit("鍵が読めません: %s" % KEYFILE)
    return m.group(1)


def pause(on: bool) -> None:
    """見回りを止める／戻す。**作業の前後で必ず呼ぶ。**"""
    u = "%s/spirit/home?pause=%d&key=%s" % (SRV, 1 if on else 0, key())
    urllib.request.urlopen(urllib.request.Request(u, data=b"", method="POST"), timeout=15).read()
    print("見回りを%s" % ("止めました" if on else "戻しました"), flush=True)


def raw(wait: float = 10.0) -> bytes:
    """いまの1枚を読む。**書き込み途中の半端な1枚を掴まない。**"""
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            if time.time() - os.path.getmtime(SHOT) < 2.0:
                d = open(SHOT, "rb").read()
                if len(d) > 20000 and d[:2] == bytes((255, 216)) and d[-2:] == bytes((255, 217)):
                    return d
        except OSError:
            pass
        time.sleep(0.2)
    raise SystemExit("写真が取れません。橋渡しが動いているか確かめてください: %s" % SHOT)


def still(a: bytes, b: bytes) -> float:
    import shot_check
    return shot_check.still(shot_check.gray(a), shot_check.gray(b))


def shoot(x: float, y: float, wait: float = None) -> bytes:
    """**見回りとまったく同じ道順**で1枚撮る。

    入り口へ戻す → その向きへ振る → 待つ → 1秒あけた2枚が同じになってから撮る。
    待ち時間の既定は12秒。**カメラが「止まった」と言っても、映像は9〜10秒遅れて
    入れ替わる**（2026-09-27 実測）。ここを削ると、振り向く途中の景色が撮れる。"""
    import sweep, shot_check
    w = WAIT if wait is None else wait
    sweep.look(*HOME_POSE)
    time.sleep(w)
    sweep.look(x, y)
    time.sleep(w)
    prev = raw()
    for _ in range(6):
        time.sleep(1.0)
        jpg = raw()
        if still(prev, jpg) <= shot_check.STILL_MAX:
            return jpg
        prev = jpg
    return prev


def go_home() -> None:
    import sweep
    sweep.go_home()


def aim_fix() -> tuple:
    """覚えた向きの補正。**見回りはこれを足した向きへ振る。**

    設定に書く向きは補正を足す前の値。撮るときは足した値を使う。"""
    import json
    p = os.path.join(BRIDGE, "aim_fix.json")
    p = p if os.path.exists(p) else os.path.expanduser("~/.arigato/bridge_pc/aim_fix.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
        return float(d.get("dx", 0.0)), float(d.get("dy", 0.0))
    except Exception:
        return 0.0, 0.0
