# -*- coding: utf-8 -*-
"""区画の決まりの試験（2026-09-25）。

なぜ要るか：9/25、区画の設定をコードから外へ出すときに
「動きが1つも変わっていないこと」を手で確かめた。同じ確認を毎回手でやるのは続かない。
ここに置いておけば、押す前に機械が確かめる。
"""
import sys, os, calendar
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server.routers.spirit as sp


def _day(y, m, d, hh, mm=0):
    """日本時間の壁時計から UNIX 秒。

    `time.mktime` は**走らせた機械の時間帯**で解釈するので使えない。
    2026-09-25、これで試験が私の PC（日本時間）では通り、CI（UTC）では
    日付が1日ずれて落ちた。試験そのものが、走る場所で答えを変えてはいけない。"""
    return calendar.timegm((y, m, d, hh, mm, 0, 0, 0, 0)) - sp.JST


# --- 水切りの猶予（本人決定 2026-09-25）：日付が変わり、そのあと使われるまでは、そのままでよい ---

def test_置いた日は何度見てもそのままでよい():
    st = {}
    t = _day(2026, 9, 25, 22)
    assert sp._grace_over(st, "水切り", t, True, True) is False
    assert sp._grace_over(st, "水切り", t + 5400, True, False) is False


def test_翌日でも誰も使っていなければそのままでよい():
    st = {}
    t = _day(2026, 9, 25, 22)
    sp._grace_over(st, "水切り", t, True, True)
    assert sp._grace_over(st, "水切り", _day(2026, 9, 26, 7), True, False) is False


def test_翌日に使われたあとも入っていれば片づいていない():
    st = {}
    t = _day(2026, 9, 25, 22)
    sp._grace_over(st, "水切り", t, True, True)
    sp._grace_over(st, "水切り", _day(2026, 9, 26, 9), True, True)      # 使われた回
    assert sp._grace_over(st, "水切り", _day(2026, 9, 26, 9, 30), True, False) is True


def test_一度しまえば数え直しになる():
    st = {}
    sp._grace_over(st, "水切り", _day(2026, 9, 25, 22), True, True)
    sp._grace_over(st, "水切り", _day(2026, 9, 26, 9), False, True)     # 空になった
    sp._grace_over(st, "水切り", _day(2026, 9, 26, 9, 5), True, True)   # また洗った
    assert sp._grace_over(st, "水切り", _day(2026, 9, 26, 23), True, False) is False


def test_誰も来ない日は猶予が続く():
    # 本人と決めた形（9/25）。「誰も困っていないなら場所も困らない」
    st = {}
    sp._grace_over(st, "水切り", _day(2026, 9, 25, 22), True, True)
    assert sp._grace_over(st, "水切り", _day(2026, 9, 28, 10), True, False) is False


# --- 区画の設定：状態に何も入れなければ、いままでの値のまま ---

def test_設定が空ならコードの既定値が出る():
    sp._state_cache = {}
    c = sp._zone_cfg("シンク")
    assert c["pose"] == "-0.70_-1.00"
    assert c["crop"]["rotate"] == sp.SINK_ROTATE
    assert c["crop"]["hide_from"] == sp.SINK_HIDE


def test_一部だけ上書きしても他は残る():
    sp._state_cache = {"zone_cfg": {"シンク": {"aim": {"min_conf": 0.30}}}}
    c = sp._zone_cfg("シンク")
    assert c["aim"]["min_conf"] == 0.30
    assert c["crop"]["rotate"] == sp.SINK_ROTATE       # 触っていない所は既定のまま
    sp._state_cache = {}


def test_知らない区画は空で返る():
    sp._state_cache = {}
    assert sp._zone_cfg("まだ無い区画") == {}


def test_どの区画にもいつどう決めたかが書いてある():
    # 9/22 に手で決めた向きの補正が古くなり、9/23〜24 に20時間50分の停止を招いた。
    # 手で決めた数字には、いつ・どうやって決めたかを必ず添える。
    for name, cfg in sp.ZONE_CFG_DEFAULT.items():
        assert cfg.get("decided"), "%s に decided が無い" % name
