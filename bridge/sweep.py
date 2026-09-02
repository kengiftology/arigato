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

# 見て回る向き。逆さ吊りなので縦は上限が部屋の床側を向く（実測）。
LOOK_POINTS = [(-0.5, 1.0), (0.0, 1.0), (-1.0, 1.0), (0.5, 1.0), (1.0, 1.0)]
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
    """その向きへ首を向ける。"""
    try:
        ptz, token = _connect()
        ptz.AbsoluteMove({"ProfileToken": token,
                          "Position": {"PanTilt": {"x": x, "y": y}}})
        time.sleep(SETTLE)
        return True
    except Exception as e:
        print("look failed:", e, flush=True)
        _ptz[0] = None
        return False


def search(grab, found) -> bool:
    """一巡して人を探す。見つけたらその向きで止まり True。

    grab  … いまの1枚を返す関数
    found … その1枚に人が写っていたか判定する関数（クラウドに聞く）
    """
    home = where()
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
    if home:                             # 見つからなければ元の向きへ戻す
        look(*home)                      # 区画の前後比較が食い違わないように
    print(time.strftime("%H:%M:%S"), "首振りしたが見つからず", flush=True)
    return False
