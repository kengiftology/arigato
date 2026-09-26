# -*- coding: utf-8 -*-
"""見回りが区画を順ぐりに回ることの試験（2026-09-26）。

なぜ要るか：9/26、設定に区画を5つ書いたのに、見に行く向き（check_pose）は
1つしか持っていなかった。カメラは毎回シンクだけを見て、残りの4区画は
一度も撮られないまま「違う景色」で飛ばされる。**9/24 に自分で警告した形を、
自分で作っていた。**しかもその状態でも記録は毎回出るので、外から見て気づけない。

ここで固定するのは3つ。
  1. 区画の数だけ向きがあり、重なっていない（重なると、どの区画の写真か分からない）
  2. 一周すると全部の区画を1回ずつ通る
  3. 見本が無い向きでは、橋渡しが「違う景色」で止めない
     （止めると1枚もクラウドへ届かず、静かに全部の区画が落ちる）
"""
import sys, os
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server.routers.spirit as sp


def _poses():
    return [sp._zone_cfg(n).get("pose") for n in sp._zone_names()]


def test_区画ごとに向きがある():
    names = sp._zone_names()
    poses = _poses()
    assert len(names) >= 1
    for n, p in zip(names, poses):
        assert p, "%s に向きが無い" % n


def test_同じ向きの区画はまとめて見る():
    # テーブルとコンロは同じ向き（-0.40_0.00）の1枚を、切り出しで分けたもの。
    # 区画ごとに回すと同じ所へ2回続けて行き、1周が無駄に伸びる。
    # 向きで回し、その向きの区画はまとめて比べる。
    poses = sp._check_poses()
    assert len(poses) == len(set(poses)), "向きの一覧に重なりがある: %s" % (poses,)
    for pose in poses:
        at = sp._zones_at(pose)
        assert at, "向き %s に区画が無い" % pose
        for n in at:
            assert sp._zone_cfg(n).get("pose") == pose


def test_一周で全部の区画が1回ずつ比べられる():
    # zone_rotate を向きの数だけ進めると、active な区画が漏れなく1回ずつ出る。
    # ここが漏れると、その区画は**一度も撮られないまま**静かに落ちる（9/26 の穴）。
    poses = sp._check_poses()
    seen = []
    for i in range(len(poses)):
        seen += list(sp._zones_at(poses[i % len(poses)]))
    assert sorted(seen) == sorted(sp._zone_names()), seen


def test_見本が無い向きでは橋渡しが止めない():
    """9/26 の穴：橋渡しは1枚の見本（sink_ref.jpg）とだけ照らしていた。

    水切りやIHの写真は当然「違う景色」になり、撮り直しを繰り返して
    1枚も送らない。見本が無い向きでは、ぶれと静けさだけで通すのが正しい。"""
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge"))
    import shot_check
    other = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "photos", "other_view.jpg")
    with open(other, "rb") as f:
        jpg = f.read()
    # シンクの向き（見本がある）＝別の景色なので弾かれる
    r_sink = shot_check.check(jpg, jpg, pose=shot_check.SINK_POSE)
    assert r_sink["why"] == "違う景色", r_sink
    # 見本がまだ無い向き＝ずれも景色も見ない。ぶれていなければ通る
    r_new = shot_check.check(jpg, jpg, pose="-9.99_-9.99")
    assert r_new["shift"] is None, r_new
    assert r_new["why"] != "違う景色", r_new


def test_錨は区画をまたがない(monkeypatch):
    """9/26 20:40 の事故の本体。錨（最後に見本と合った1枚）を全区画で1つしか

    持っていなかった。シンクの写真がその錨になり、続く4区画は**同じ1枚**と
    照らして合格した（自分自身と比べれば確かさは 1.0）。**シンクの向きで撮った
    1枚が、水切り・テーブル・コンロ・IH の答えになった**（zone_all of=5・changed=3）。
    見本4枚はすべて登録済みだったので、「見本が無いから素通り」ではない。"""
    cv2 = pytest.importorskip("cv2")
    ref = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "bridge", "sink_ref.jpg")
    other = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "photos", "other_view.jpg")
    with open(ref, "rb") as f:
        sink = f.read()
    with open(other, "rb") as f:
        elsewhere = f.read()
    # シンクの見本＝シンクの写真／水切りの見本＝別の景色
    monkeypatch.setattr(sp, "read_object",
                        lambda o, *a, **k: elsewhere if "zoneref" in str(o) else sink)
    sp._last_good.clear()
    ok_sink, _, _ = sp._view_ok(sink, "シンク")
    assert ok_sink is True, "シンクの1枚がシンクの見本と合わない"
    # 同じ1枚を水切りとして出す。**通ってはいけない**
    ok_rack, resp, _ = sp._view_ok(sink, "水切り")
    assert ok_rack is False, "シンクの写真が水切りとして通った（錨の使い回し）: %s" % (resp,)


def test_見本が無い区画は通さない(monkeypatch):
    """9/26 20:40 の事故：見本を登録していない区画3つが、シンクの向きで撮った

    1枚に対して答えた（zone_all of=5・changed=3）。向きの守りは見本が無いとき
    「判断できないので止めない」で素通りしていた。区画が自分の見本を指していて
    実物が無いなら、**どこを写した1枚か分からない**のだから、通してはいけない。"""
    monkeypatch.setattr(sp, "read_object", lambda *a, **k: None)
    # 自分の見本を指している区画（水切りなど）＝通さない
    for n in sp._zone_names():
        if (sp._zone_cfg(n).get("aim") or {}).get("ref"):
            ok, resp, shift = sp._view_ok(b"x", n)
            assert ok is False, n
    # 共通の見本しか持たない区画（シンク）＝これまでどおり止めない
    ok, _, _ = sp._view_ok(b"x", "シンク")
    assert ok is True


# ── 人が去ったあと、その滞在に関わる区画をぜんぶ見る（2026-09-26・帰属の穴） ──
#
# なぜ要るか：区画が5つになると、1回の滞在で見られるのは1区画だけになる。
# 残りは次の滞在、その次の滞在…と後回しになり、撮る頃には間に何人も来ている。
# **「誰がやったか」が結びつかない。**しかも見送った区画は visit_seen が消えたあとに
# 回ってくるので「静か（誰も来ていない）」と読まれ、本物の片づけまで誤報に数えられる。

def test_一周で全部の向きを1回ずつ見る():
    poses = sp._check_poses()
    rotate, left, seen = 0, [], []
    for _ in range(len(poses)):
        pose, left, rotate = sp._sweep_plan(rotate, left, poses)
        seen.append(pose)
    assert sorted(seen) == sorted(poses), seen
    assert left == [], "1周したのに残っている: %s" % (left,)


def test_一周の起点は滞在ごとにずれる():
    # いつも同じ区画が最初だと、その区画だけ「前」の写真が新しく、
    # ほかは1周ぶん古いままになる。起点をずらして偏りを無くす。
    poses = sp._check_poses()
    starts = []
    rotate = 0
    for _ in range(len(poses) + 1):
        left = []
        pose, left, rotate = sp._sweep_plan(rotate, left, poses)
        starts.append(pose)
        # この滞在の残りは捨てられたものとして、次の滞在へ
    assert len(set(starts[:len(poses)])) == len(poses), starts


def test_設定から消えた向きは捨てる():
    # 区画を止めたあとも古い状態に残っていると、無い場所へ首を振りつづける。
    poses = sp._check_poses()
    pose, left, rotate = sp._sweep_plan(0, ["-9.99_-9.99", poses[0]], poses)
    assert pose == poses[0], pose
    assert "-9.99_-9.99" not in left


def test_区画が無ければ何も見に行かない():
    pose, left, rotate = sp._sweep_plan(3, [], ())
    assert pose == ""
    assert left == []
    assert rotate == 3


# ── 見回りを出す係（/spirit/hint）を、そのまま走らせて確かめる ──
#
# なぜ要るか：組み立て（_sweep_plan）が正しくても、**それを呼ぶ側の条件**が
# 間違っていれば何も起きない。9/26 の穴はまさにそこだった（設定は5区画ぶん
# 書けていて、呼ぶ側が1つしか見ていなかった）。本番では人が来るまで見回りが
# 出ないので、夜のうちに確かめる手立てが他にない。

class _State(dict):
    pass


def _hint_once(monkeypatch, st, now):
    """その時刻に /spirit/hint が何を返すかを、状態を持ち込んで確かめる。"""
    import asyncio
    monkeypatch.setattr(sp, "_load", lambda *a, **k: st)
    monkeypatch.setattr(sp, "_save", lambda s, *a, **k: None)
    monkeypatch.setattr(sp, "_log_event", lambda *a, **k: None)
    monkeypatch.setattr(sp.time, "time", lambda: now)
    return asyncio.run(sp.hint()).strip()


def test_静かになったら1周ぜんぶ回る(monkeypatch):
    poses = list(sp._check_poses())
    t = 1_800_000_000.0
    st = _State({"check_pose": poses[0], "last_seen": t, "last_motion": 0,
                 "checked_at": t - sp.CHECK_GAP - 1, "zone_rotate": 0})
    got = []
    for i in range(len(poses)):
        now = t + sp.CHECK_QUIET_SEC + 1 + i * 30
        tag = _hint_once(monkeypatch, st, now)
        assert tag.startswith("check "), tag
        got.append(tag.split(None, 1)[1])
        st["checked_at"] = now + 20        # 目が見に行って戻ってきた
    assert sorted(got) == sorted(poses), got
    # 1周し終えたら、人が来るまで出ない
    assert _hint_once(monkeypatch, st, t + sp.CHECK_QUIET_SEC + 1 + 5 * 30) == ""


def test_見回りの途中で何度覗かれても同じ向きを返す(monkeypatch):
    poses = list(sp._check_poses())
    t = 1_800_000_000.0
    st = _State({"check_pose": poses[0], "last_seen": t, "last_motion": 0,
                 "checked_at": t - sp.CHECK_GAP - 1, "zone_rotate": 0})
    now = t + sp.CHECK_QUIET_SEC + 1
    first = _hint_once(monkeypatch, st, now)
    # 目は3秒おきに覗きにくる。往復20秒のあいだ、同じ向きでなければ
    # 控えた向きと実際に撮った向きが食い違う。
    for k in range(1, 6):
        assert _hint_once(monkeypatch, st, now + k * 3) == first


def test_人が来たら1周を打ち切る(monkeypatch):
    poses = list(sp._check_poses())
    if len(poses) < 2:
        pytest.skip("区画が1つでは打ち切りを試せない")
    t = 1_800_000_000.0
    st = _State({"check_pose": poses[0], "last_seen": t, "last_motion": 0,
                 "checked_at": t - sp.CHECK_GAP - 1, "zone_rotate": 0})
    now = t + sp.CHECK_QUIET_SEC + 1
    _hint_once(monkeypatch, st, now)                  # 1周の1つ目
    assert st.get("sweep_left"), "1周の残りが立っていない"
    st["checked_at"] = now + 20
    st["last_seen"] = now + 25                        # 人が来た
    assert _hint_once(monkeypatch, st, now + 30) == ""   # すぐには出さない
    assert st.get("sweep_left") == [], "残りを捨てていない"
