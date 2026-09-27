# -*- coding: utf-8 -*-
"""地霊のひとまわりを Rhino に描く（2026-09-28）。

本人のロッカーの流れ図（work/esp32_door_lock/tools/locker_flow_diagram.py）と同じ作り方・同じ配色。

  Rhino の Python エディタで開いて実行する（RunPythonScript）。
  何度実行してもよい。実行のたび、同じレイヤの中身を消してから描き直す。

配色（ロッカーの図と同じ意味）
  緑 ふつうの流れ ／ 赤 警告 ／ 黄 注意 ／ 青 自動 ／ 紫 データ ／ 灰 起こり得ない組み合わせ

※「どこまでできているか」はここでは描かない（そちらは scripts/flow.py の1枚）。
  この図は**流れそのもの**を見るためのもの。
"""
import rhinoscriptsyntax as rs

# ---- 見た目の決まり -------------------------------------------------------
BOX_W, BOX_H = 62.0, 26.0      # 箱の大きさ（mm）
COL_X = [0.0, 95.0, 190.0, 285.0]   # 4つの列の中心
ROW_Y = [0.0, -45.0, -90.0, -135.0, -180.0, -225.0]  # 段（上から下へ）
TEXT_H = 3.2
NOTE_H = 2.4

LAYERS = {
    "ふつうの流れ": (60, 125, 83),
    "警告":        (168, 65, 46),
    "注意":        (154, 107, 31),
    "自動":        (47, 93, 119),
    "データ":      (107, 78, 140),
    "起こり得ない": (138, 147, 143),
    "線":          (110, 120, 116),
    "文字":        (20, 28, 26),
}

# ---- 図の中身 -------------------------------------------------------------
# (列, 段, 題, 添え書き, レイヤ)
NODES = [
    (0, 0, "人が来る",           "人感・動きで気づく",     "ふつうの流れ"),
    (0, 1, "滞在が始まる",       "",                       "ふつうの流れ"),
    (0, 2, "立ち寄りか通り過ぎか", "線は本人が決める",       "注意"),
    (0, 3, "滞在が閉じる",       "",                       "ふつうの流れ"),
    (0, 4, "なつき度が動く",     "上がるのは世話のあと",   "データ"),

    (1, 0, "顔を見つける",       "",                       "ふつうの流れ"),
    (1, 1, "小さい顔は見送る",   "70px 未満",              "注意"),
    (1, 2, "誰かを見分ける",     "似ている度 0.40 以上",   "ふつうの流れ"),
    (1, 3, "分からないまま進む", "名前を付けない",         "警告"),
    (1, 4, "初めての人を覚える", "全員と離れていれば",     "自動"),

    (2, 0, "居るうちに声をかける", "",                     "ふつうの流れ"),
    (2, 1, "聞き取って短く返す", "許しは出さない",         "ふつうの流れ"),
    (2, 2, "確かなときだけ名を使う", "確かでなければ黙る", "注意"),
    (2, 3, "その人が整えた場所に触れる", "催促はしない",   "ふつうの流れ"),
    (2, 4, "名指しで人に伝える", "作らない",               "起こり得ない"),

    (3, 0, "5か所を順に見回る",  "1周 約2分",              "自動"),
    (3, 1, "向きを合わせて撮る", "違う景色なら見送る",     "自動"),
    (3, 2, "前と見比べる",       "同じ向きの2枚で",        "自動"),
    (3, 3, "人が来たら中断する", "残りは次の周で",         "注意"),
    (3, 4, "片づけとして覚える", "誰が・どこを",           "データ"),
]

# (元の列, 元の段, 先の列, 先の段)
EDGES = [
    (0, 0, 0, 1), (0, 1, 0, 2), (0, 2, 0, 3), (0, 3, 0, 4),
    (0, 0, 1, 0), (1, 0, 1, 1), (1, 1, 1, 2), (1, 2, 1, 3), (1, 2, 1, 4),
    (1, 2, 2, 0), (2, 0, 2, 1), (2, 1, 2, 2), (2, 2, 2, 3), (2, 3, 2, 4),
    (0, 3, 3, 0), (3, 0, 3, 1), (3, 1, 3, 2), (3, 2, 3, 3), (3, 3, 3, 4),
    (3, 4, 0, 4), (3, 4, 2, 3),
]

TITLES = ["人の流れ", "顔と名前", "声と対話", "場所と覚え"]


# ---- ここから下は描く処理 -------------------------------------------------
def ensure_layers():
    for name, rgb in LAYERS.items():
        layer = u"地霊フロー::" + name
        if not rs.IsLayer(layer):
            rs.AddLayer(layer, rs.CreateColor(*rgb))
        else:
            rs.LayerColor(layer, rs.CreateColor(*rgb))
            old = rs.ObjectsByLayer(layer)
            if old:
                rs.DeleteObjects(old)
    return True


def box(col, row, title, note, layer):
    """箱を1つ描く。中心は列と段で決まる。"""
    cx, cy = COL_X[col], ROW_Y[row]
    rs.CurrentLayer(u"地霊フロー::" + layer)
    pts = [(cx - BOX_W / 2, cy - BOX_H / 2, 0), (cx + BOX_W / 2, cy - BOX_H / 2, 0),
           (cx + BOX_W / 2, cy + BOX_H / 2, 0), (cx - BOX_W / 2, cy + BOX_H / 2, 0),
           (cx - BOX_W / 2, cy - BOX_H / 2, 0)]
    rs.AddPolyline(pts)
    rs.CurrentLayer(u"地霊フロー::文字")
    rs.AddText(title, (cx - BOX_W / 2 + 3, cy + 2, 0), TEXT_H)
    if note:
        rs.AddText(note, (cx - BOX_W / 2 + 3, cy - 7, 0), NOTE_H)


def arrow(c1, r1, c2, r2):
    """箱から箱へ線を引く。同じ列なら縦、列をまたぐなら横から回す。"""
    rs.CurrentLayer(u"地霊フロー::線")
    x1, y1 = COL_X[c1], ROW_Y[r1]
    x2, y2 = COL_X[c2], ROW_Y[r2]
    if c1 == c2:
        a = (x1, y1 - BOX_H / 2, 0)
        b = (x2, y2 + BOX_H / 2, 0)
        line = rs.AddLine(a, b)
    else:
        a = (x1 + BOX_W / 2, y1, 0)
        b = (x2 - BOX_W / 2, y2, 0)
        mid = ((a[0] + b[0]) / 2.0, a[1], 0)
        mid2 = ((a[0] + b[0]) / 2.0, b[1], 0)
        line = rs.AddPolyline([a, mid, mid2, b])
    if line:
        rs.CurveArrows(line, 2)          # 終点に矢
    return line


def titles():
    rs.CurrentLayer(u"地霊フロー::文字")
    for i, name in enumerate(TITLES):
        rs.AddText(name, (COL_X[i] - BOX_W / 2, ROW_Y[0] + BOX_H, 0), TEXT_H * 1.3)
    rs.AddText(u"地霊のひとまわり（2026-09-28）", (COL_X[0] - BOX_W / 2, ROW_Y[0] + BOX_H * 2.2, 0), TEXT_H * 1.8)
    rs.AddText(u"緑 ふつうの流れ ／ 赤 警告 ／ 黄 注意 ／ 青 自動 ／ 紫 データ ／ 灰 作らないと決めたもの",
               (COL_X[0] - BOX_W / 2, ROW_Y[-1] - 22, 0), NOTE_H * 1.2)


def main():
    rs.EnableRedraw(False)
    ensure_layers()
    for (c1, r1, c2, r2) in EDGES:
        arrow(c1, r1, c2, r2)
    for (col, row, title, note, layer) in NODES:
        box(col, row, title, note, layer)
    titles()
    rs.CurrentLayer(u"Default")
    rs.EnableRedraw(True)
    rs.ZoomExtents()
    print(u"地霊フロー：箱 %d・線 %d を描きました" % (len(NODES), len(EDGES)))


if __name__ == "__main__":
    main()
