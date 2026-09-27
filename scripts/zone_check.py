# -*- coding: utf-8 -*-
"""区画の判定が通っているかを、記録から一目で出す（2026-09-27）。

9/27 の午前、**シンク以外の4区画は一度も判定が成立していなかった**のに、
記録は毎回出ていたので外からは動いて見えた。同じ見落としをしないための道具。

    python scripts/zone_check.py [さかのぼる時間]   （既定 6時間）

出すもの：
  ① 区画ごとの「比べられた／飛ばされた」回数（飛ばされた理由つき）
  ② 1周にかかった時間（judge の round_sec・最後まで回った1周だけ）
  ③ 打ち切り（sweep_cut）の回数と、そのとき残っていた区画の数
"""
import collections, json, sys, time, urllib.request

SRV = "https://arigato-3ipecjbnha-an.a.run.app"


def fetch(since: float) -> list:
    ev, before = [], 0
    for _ in range(10):
        u = SRV + "/spirit/log?limit=1000" + (("&before=%f" % before) if before else "")
        got = json.load(urllib.request.urlopen(u, timeout=90))["events"]
        if not got:
            break
        ev += got
        before = got[-1]["t"]
        if before < since:
            break
    return sorted([e for e in ev if e["t"] >= since], key=lambda e: e["t"])


def main() -> None:
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    since = time.time() - hours * 3600
    ev = fetch(since)
    f = lambda t: time.strftime("%H:%M:%S", time.localtime(t))
    print("直近 %.1f 時間（%s 以降）の記録 %d 件" % (hours, f(since), len(ev)))

    ok = collections.Counter()
    ng = collections.Counter()
    why = collections.Counter()
    for e in ev:
        if e["kind"] in ("zone", "zone_same"):
            ok[e.get("zone", "?")] += 1
        if e["kind"] == "zone_skip":
            ng[e.get("zone", "?")] += 1
            why[(e.get("zone", "?"), e.get("why", "?"))] += 1
    names = sorted(set(list(ok) + list(ng)))
    print("\n① 区画ごと")
    if not names:
        print("   区画の判定が1件もありません（人が来ていないか、届いていない）")
    for n in names:
        mark = "通った" if ok[n] else "**1件も通っていない**"
        print("   %-6s 比べられた %2d ／ 飛ばした %2d  %s" % (n, ok[n], ng[n], mark))
    for (n, w), c in why.most_common():
        print("      %s を飛ばした理由: %s ×%d" % (n, w, c))

    print("\n② 1周にかかった時間")
    rounds = [e for e in ev if e["kind"] == "judge" and e.get("round_done")]
    if not rounds:
        print("   最後まで回った1周がまだありません")
    for e in rounds[-8:]:
        print("   %s  %s秒  （最後に見た区画 %s）" % (f(e["t"]), e.get("round_sec"), e.get("zone") or "?"))

    print("\n③ 打ち切り")
    cuts = [e for e in ev if e["kind"] == "sweep_cut"]
    done = len(rounds)
    print("   最後まで回った %d 回 ／ 打ち切り %d 回%s" % (
        done, len(cuts),
        "（%.0f%%が打ち切り）" % (100 * len(cuts) / (done + len(cuts))) if done + len(cuts) else ""))
    for e in cuts[-8:]:
        print("   %s  残り %d 区画 %s" % (f(e["t"]), e.get("left", 0), e.get("poses") or ""))

    print("\n④ 「前」の写真の古さ（prev_age・基準の寿命は 10800秒）")
    ages = [e for e in ev if e["kind"] == "judge" and e.get("prev_age") is not None]
    if ages:
        xs = [e["prev_age"] for e in ages]
        print("   中央値 %d秒 ／ 最長 %d秒 ／ 寿命超え %d件"
              % (sorted(xs)[len(xs) // 2], max(xs), sum(1 for x in xs if x > 10800)))
    else:
        print("   まだ材料がありません（`prev_age` を入れた版が本番に入ってから）")


if __name__ == "__main__":
    main()
