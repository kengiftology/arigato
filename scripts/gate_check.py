# -*- coding: utf-8 -*-
"""9/28 の関門「積もりが見えたか」の材料を集める（2026-09-24）。

  python3 scripts/gate_check.py [日付 例 2026-09-28]

クラウドの記録（/spirit/log）だけを見る。マックからカメラにもラズパイにも触らない。
材料の中身と合否は docs/関門_9-28_積もりが見えたか.md。判断は進行役。
"""
import collections
import datetime as dt
import json
import statistics
import sys
import urllib.request

BASE = "https://arigato-3ipecjbnha-an.a.run.app"
JST = dt.timezone(dt.timedelta(hours=9))
SWING_LIMIT = 79        # 1日79回の首振りで前後比較が壊れた（9/19 の実測）


def fetch(limit=1000, before=None):
    url = "%s/spirit/log?limit=%d" % (BASE, limit)
    if before:
        url += "&before=%f" % before
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())["events"]


def events_of(day: dt.date):
    """その日（日本時間）のできごとを、遡りながら集める。"""
    out, before, seen = [], None, 0
    for _ in range(12):                      # 最大12回遡る
        ev = fetch(1000, before)
        if not ev:
            break
        out += ev
        oldest = ev[-1]["t"]
        if dt.datetime.fromtimestamp(oldest, JST).date() < day:
            break
        before, seen = oldest, seen + len(ev)
    return [e for e in out
            if dt.datetime.fromtimestamp(e["t"], JST).date() == day]


def main():
    day = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today()
    ev = events_of(day)
    print("関門の材料 ／", day, "／ できごと", len(ev), "件")
    if not ev:
        print("  記録が取れない。/spirit/log を確かめる")
        return

    # ①② 挨拶が、誰の・どの区画に触れたか（C が voice の行に person/zone/confirmed を足す前提）
    greets = [e for e in ev if e.get("kind") in ("voice", "greet") and e.get("zone")]
    pairs = collections.defaultdict(set)
    wrong = []
    for g in greets:
        pairs[g.get("person")].add(g["zone"])
        if g.get("confirmed") is False:
            wrong.append(g)
    people = [p for p in pairs if p]
    diff_zones = len({tuple(sorted(pairs[p])) for p in people}) > 1 if len(people) >= 2 else False
    print("\n① 人ごとに違う区画に触れたか　←本丸")
    print("   区画に触れた挨拶 %d件／相手 %d人" % (len(greets), len(people)))
    for p in people:
        print("     %s → %s" % (p, "・".join(sorted(pairs[p]))))
    print("   判定：%s（2人以上・それぞれ別の区画で合格）"
          % ("○" if len(people) >= 2 and diff_zones else "×"))
    print("\n② 間違った相手に言っていないか　←1件でも出たら止める")
    if not greets:
        print("   区画に触れた挨拶が0件なので、まだ判定できない（—）")
    else:
        print("   確定していない相手への挨拶 %d件 → 判定：%s" % (len(wrong), "○" if not wrong else "×"))

    # ③ 履歴が貯まっているか（A の /spirit/history を先に見る。無ければ記録から数える）
    try:
        with urllib.request.urlopen(BASE + "/spirit/history?days=1", timeout=20) as r:
            h = json.loads(r.read().decode())
        print("\n③ 履歴が貯まっているか（A の /spirit/history）")
        print("   人 %s・区画 %s・人×区画の組 %s（%s 以降・更新 %s）"
              % (h.get("people"), h.get("zones"), h.get("pairs"),
                 h.get("since"), h.get("updated", "?")))
        print("   判定：%s（2人以上・2区画以上で合格）"
              % ("○" if (h.get("people", 0) >= 2 and h.get("zones", 0) >= 2) else "×"))
        for who, z in (h.get("table") or {}).items():
            print("     %s → %s" % (who, z))
        return_after_three = True
    except Exception:
        return_after_three = False

    # ③の控え：記録から見える範囲で数える
    cares = [e for e in ev if e.get("kind") == "care" and e.get("zone")]
    by_person = collections.defaultdict(set)
    for c in cares:
        by_person[c.get("person")].add(c["zone"])
    n_people = len([p for p in by_person if p])
    n_zones = len({z for s in by_person.values() for z in s})
    if not return_after_three:
        print("\n③ 履歴が貯まっているか（控え：記録から数えた目安）")
        print("   世話の記録 %d件／人 %d人／区画 %d か所 → 判定：%s（2人以上・2区画以上で合格）"
              % (len(cares), n_people, n_zones, "○" if n_people >= 2 and n_zones >= 2 else "×"))
        print("   ※ /spirit/history がまだ無い。本体は A のこの口")

    # ④ 1周の実測（合否には使わない）
    judges = [e for e in ev if e.get("kind") == "judge"]
    rounds = [e["round_sec"] for e in judges if e.get("round_sec")]
    ages = collections.defaultdict(list)
    for e in judges:
        if e.get("prev_age") is not None:
            ages[e.get("zone", "?")].append(e["prev_age"])
    print("\n④ 1周の実測（数字を見るだけ）")
    print("   首振り（judge の件数）%d回 ／ 壊れる線 %d回 → %s"
          % (len(judges), SWING_LIMIT, "超えている" if len(judges) > SWING_LIMIT else "収まっている"))
    if rounds:
        print("   1周の時間 中央値 %.0f秒（最大 %.0f秒）" % (statistics.median(rounds), max(rounds)))
    for z, a in sorted(ages.items()):
        print("   前の写真の古さ %s：中央値 %.0f秒（最大 %.0f秒）" % (z, statistics.median(a), max(a)))
    if not judges:
        print("   judge の行がまだ無い（B が 9/25 の設定化で入れる）")


if __name__ == "__main__":
    main()
