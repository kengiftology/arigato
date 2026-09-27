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


# --- 区画の数と、基準写真の寿命の釣り合い（2026-09-26） ---

def test_一周が基準写真の寿命より短い():
    """区画を増やすと1周が延びる。基準の寿命を超えると**比較が一度も成立しない**。

    2026-09-26、区画を5つにした時点で 1周2.5時間・基準の寿命2時間となり、
    次に同じ区画へ戻ったとき必ず「基準が古い」で捨てられる状態になっていた。
    しかも記録には baseline だけが並ぶので、**静かに起きる**。
    区画を足すときは、ここが落ちることで気づけるようにしておく。"""
    zones = len(sp._zone_names())
    round_sec = zones * sp.IDLE_CHECK_GAP
    assert round_sec < sp.BASELINE_MAX_AGE, (
        "区画%d個で1周%.1f時間、基準の寿命は%.1f時間。"
        "このままでは比較が成立しない。寿命を延ばすか、点検の間隔を縮めること"
        % (zones, round_sec / 3600, sp.BASELINE_MAX_AGE / 3600))


def test_同じ鍵が2つ並んだ設定が無い():
    """9/27：IH の設定に `decided` が2つ並び、**2つ目（水切りの文面）が勝っていた。**

    「いつ・どうやって決めたか」を必ず書く決まりは守られているのに、
    **中身が別の区画の説明にすり替わっていた。**書いてあるので誰も疑わない。
    Python は黙って後ろを採るので、読んでも気づけない。ここで落とす。"""
    import ast, io, collections, os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "server", "routers", "spirit.py")
    tree = ast.parse(io.open(p, encoding="utf-8").read())
    dups = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            for k, n in collections.Counter(keys).items():
                if n > 1:
                    dups.append("%d行目: %s が %d回" % (node.lineno, k, n))
    assert not dups, "同じ鍵が2つ並んでいる: " + " / ".join(dups)


def test_記録が書けなくなったら健康診断に出る(monkeypatch):
    """9/27：記録を書く処理が黙って落ちると、**全部の数字が静かに減る。**

    「世話が少ない」のか「記録できていない」のかを外から見分けられないのが怖い。
    `_log_event` の失敗を `_log_event` で書くと無限に回るので、health に出す。
    見張りは10分おきに health を叩いているので、そのまま拾える。"""
    import time as _t
    now = _t.time()
    monkeypatch.setattr(sp, "_log_ok", [now - 300, now - 10, "ServiceUnavailable: 503"])
    h = sp._health()
    assert h["ok"] is False
    assert "記録が書けていない" in h["stopped"]
    # 書けているときは止めない
    monkeypatch.setattr(sp, "_log_ok", [now - 5, 0.0, ""])
    h = sp._health()
    assert "記録が書けていない" not in h["stopped"]


def test_滞在は帰ったあとの時間を足さない():
    """9/27：滞在を「いま（＝前後を比べた時刻）− 来た時刻」で測っていた。

    見回りは人が去って静かになってから走るので、**帰ったあとの時間まで足される。**
    しかも1周が150秒に伸びたぶん**滞在時間が装置の都合で伸びる**（#25 に直に効く）。
    最後にその人を見た時刻までで測る。"""
    st = {"visit_of": {"p01": 1000.0}, "seen_at": {"p01": 1300.0}}
    # 前後を比べたのは、その人が帰ってから30分後
    assert sp._person_stay(st, "p01", 3100.0) == 300.0
    # 居るあいだに呼ばれたとき（seen_at がいま）は、これまでと同じ
    st2 = {"visit_of": {"p01": 1000.0}, "seen_at": {"p01": 1042.0}}
    assert sp._person_stay(st2, "p01", 1042.0) == 42.0
    # 覚えが無ければ0
    assert sp._person_stay({}, "p01", 1000.0) == 0.0


def test_AIが断られたら健康診断に出る(monkeypatch):
    """9/27 14:00、Anthropic の残高が切れて AI が断られ始めた。

    **顔も記録も動き続けるので、区画の判定と一言だけが静かに止まる。**
    見張りには一切かからず（health は ok のまま）、偶然 記録を読んでいて見つけた。
    観察期（10/5〜12/6）の9週間でこれが起きると、**「世話が起きなかった」という
    記録だけが残る。**残高切れか一時的な不調かは区別しない。どちらも判定が止まる。"""
    import time as _t
    now = _t.time()
    monkeypatch.setattr(sp, "_ai_ok", [now - 3600, now - 60, "BadRequestError: credit balance"])
    h = sp._health()
    assert h["ok"] is False
    assert "AIが答えていない" in h["stopped"]
    monkeypatch.setattr(sp, "_ai_ok", [now - 30, 0.0, ""])
    assert "AIが答えていない" not in sp._health()["stopped"]


def test_AIの呼び出しが窓口を通っている():
    """控えるのを1か所ずつ書くと必ず抜ける。**窓口は1つ**にしてある。"""
    import io, os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "server", "routers", "spirit.py")
    src = io.open(p, encoding="utf-8").read()
    direct = [l.strip() for l in src.splitlines()
              if ".messages.create(" in l and "_ai_create" not in l and "**kw" not in l]
    assert not direct, "窓口を通していない呼び出しがある: %s" % direct


def test_AIの窓口が自分を呼んでいない():
    """9/27 16:22：窓口を作る一括置換が、**窓口の中身まで書き換えた。**

    `_ai_create` が `_ai_create` を呼ぶ形になり、AI の呼び出しが4分間すべて
    RecursionError で落ちた。**道具が道具自身を壊した。**置換のあと実物を読めば
    すぐ分かったが、テストが無いと次も同じことが起きる。"""
    import ast, io, os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "server", "routers", "spirit.py")
    tree = ast.parse(io.open(p, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ai_create":
            calls = [n for n in ast.walk(node)
                     if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_ai_create"]
            assert not calls, "_ai_create が自分自身を呼んでいる"
            inner = [n for n in ast.walk(node) if isinstance(n, ast.Attribute) and n.attr == "create"]
            assert inner, "_ai_create の中から本物の呼び出しが消えている"
            return
    raise AssertionError("_ai_create が見つからない")
