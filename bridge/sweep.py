# -*- coding: utf-8 -*-
"""人感が鳴ったら、カメラの首を振って人を探す（2026-09-02）。

カメラは一方向しか見ていない。人感センサーは部屋のどこで動いても鳴る。
実際、人感が気づいた5分後にようやく顔が取れた日があった。
その差を埋めるための首振り。

探し方は「一巡して顔が取れた向きで止まる」。取れなければ元の向きへ戻す。
勝手に向きが変わったままだと、区画の前後比較の「前」と食い違うため。
"""
import time

HOST, PORT = "192.168.0.230", 2020
USER, PWD = "thankU", "39Kitchen"

# 定位置。物の増減を見るための向きで、前後の比較はいつもここで撮った2枚で行う。
# 逆さ吊りなので縦は上限が部屋の床側を向く（実測）。
# 2026-09-05: カメラを付け直した。(-0.5, 1.0) は真下の床だけを見ており、
# 人は写っても頭のてっぺんしか写らなかった。全周を撮り直して選びなおした先が
# (0.7, 0.4)。シンク・コンロ・棚・冷蔵庫・テーブル・床が1枚に収まり、
# 立った人の頭も画面に入る。
HOME = (0.7, 0.4)

# 人を探すときに見て回る向き。
# 縦を1.0に固定していた頃は床ばかり見ていて、立った人の頭が画面の上で
# 切れていた（2026-09-03・顔が口と顎だけになり一致度が上がらなかった）。
# 実測では y=+0.2〜0.6 が人の高さに当たるので、そこを中心に見て回る。
LOOK_POINTS = [(-0.5, 0.4), (0.0, 0.4), (-1.0, 0.4), (0.5, 0.4), (1.0, 0.4),
               (-0.5, 0.1), (0.0, 0.1), (0.5, 0.1)]
SETTLE = 2.5           # 首が動き終わるのを待つ
_ptz = [None, None]    # 繋ぎ直しを避けて使い回す


def _wsdl_dir() -> str | None:
    """ONVIFの定義ファイルの置き場を探す。

    onvif-zeep は定義ファイルを python3.4 用の場所へ入れてしまうことがあり
    （実際にラズパイでそうなった）、既定の場所を見にいくと見つからない。
    見つけた場所を渡してやれば動く。"""
    import glob
    import sys
    for pat in ("/*/lib/*/site-packages/wsdl", "/*/site-packages/wsdl"):
        for base in (sys.prefix, "/usr", "/usr/local"):
            hits = glob.glob(base + pat.replace("/*/", "/", 1)) or glob.glob(base + pat)
            for h in hits:
                if glob.glob(h + "/devicemgmt.wsdl"):
                    return h
    return None


def _connect():
    if _ptz[0] is None:
        from onvif import ONVIFCamera
        d = _wsdl_dir()
        args = (HOST, PORT, USER, PWD)
        cam = ONVIFCamera(*args, wsdl_dir=d) if d else ONVIFCamera(*args)
        media = cam.create_media_service()
        _ptz[0] = cam.create_ptz_service()
        _ptz[1] = media.GetProfiles()[0].token
    return _ptz


def settled(tries: int = 6) -> tuple | None:
    """首が止まるまで待ってから向きを返す。

    動いている途中に聞くと、通りすがりの向きが返ってくる
    （実測：(0.7, 0.4) へ向かう途中で (0.27, 1.0) と答えた）。
    その値が写真に添いて送られると、別の向きで撮った2枚を
    同じ向きとして比べることになる。二度続けて同じなら止まったとみなす。"""
    prev = None
    for _ in range(tries):
        w = where()
        if w is None:
            return None
        if prev is not None and abs(w[0] - prev[0]) < 0.01 and abs(w[1] - prev[1]) < 0.01:
            return w
        prev = w
        time.sleep(0.6)
    return prev


def where() -> tuple | None:
    """いまの向き。取れなければNone。"""
    try:
        ptz, token = _connect()
        p = ptz.GetStatus({"ProfileToken": token}).Position.PanTilt
        return (float(p.x), float(p.y))
    except Exception as e:
        print("where failed:", e, flush=True)
        _ptz[0] = None
        return None


def look(x: float, y: float) -> bool:
    """その向きへ首を向け、止まるまで待つ。

    決め打ちで2.5秒待っていた頃は、遠くへ振ると待ちきれなかった。
    入り口とキッチンは横に1.58ぶん離れていて、2.5秒では戻りきらない。
    そのまま次へ進むと、動いている最中の景色を「静かな状態」として
    測ってしまい、しきい値が167まで跳ね上がって見張りが死んだ（実測）。"""
    try:
        ptz, token = _connect()
        ptz.AbsoluteMove({"ProfileToken": token,
                          "Position": {"PanTilt": {"x": x, "y": y}}})
        time.sleep(SETTLE)
        settled(tries=10)                # 本当に止まったか、カメラに聞く
        return True
    except Exception as e:
        print("look failed:", e, flush=True)
        _ptz[0] = None
        return False


def go_home() -> bool:
    """定位置へ戻る。物の前後比較は、この向きで撮った2枚どうしでしか成り立たない。"""
    return look(*HOME)


def search(grab, found) -> bool:
    """一巡して人を探す。見つけたらその向きで止まり True。

    grab  … いまの1枚を返す関数
    found … その1枚に人が写っていたか判定する関数（クラウドに聞く）
    """
    for x, y in LOOK_POINTS:
        if not look(x, y):
            return False
        time.sleep(1.0)                  # 開いたままの映像が新しい向きに入れ替わるのを待つ
        jpg = grab()
        if jpg is None:
            continue
        if found(jpg):
            print(time.strftime("%H:%M:%S"),
                  "首振りで発見 (%.1f, %.1f)" % (x, y), flush=True)
            return True
    go_home()                            # 見つからなければ定位置へ戻す
    print(time.strftime("%H:%M:%S"), "首振りしたが見つからず", flush=True)
    return False
