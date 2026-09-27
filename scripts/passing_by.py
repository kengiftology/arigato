# -*- coding: utf-8 -*-
"""立ち寄り率・通り過ぎ率（#25・2026-09-27）。

**率を1つに決めない。**「立ち寄り／通り過ぎ／不明」の3つを数え、
通り過ぎ率は**上限と下限の幅**で出す。設計は `docs/通り過ぎ率の設計_2026-09-27.md`。

なぜ幅なのか：**顔が取れないのは、速く通り過ぎた人にいちばん起きやすい。**
不明を分母から外すと、通り過ぎた人だけが消えた分母になり、率は必ず低く出る
（9/10〜15 の「誰か分かった率 100%」と同じ型の誤り）。

    python scripts/passing_by.py [さかのぼる日数]    （既定 7日）

数え方（2026-09-27 本人決定）：
  通り過ぎ … その人の滞在が 30秒（MIN_PRESENCE）未満
  立ち寄り … 30秒以上
  不明     … 人が居たのは確かだが、顔が1枚も取れていない滞在
"""
import collections, json, sys, time, urllib.request

SRV = "https://arigato-3ipecjbnha-an.a.run.app"
LINE = 30.0          # 通り過ぎの線。3秒〜30秒は分布がほぼ空なので、この範囲ならどこでも同じ
MERGE = 1800.0       # その人が30分来なければ、その人の滞在はおわり（VISIT_MERGE_GAP）


def fetch(since: float) -> list:
    ev, before = [], 0
    for _ in range(12):
        u = SRV + "/spirit/log?limit=1000" + (("&before=%f" % before) if before else "")
        got = json.load(urllib.request.urlopen(u, timeout=90))["events"]
        if not got:
            break
        ev += got
        before = got[-1]["t"]
        if before < since:
            break
    return sorted([e for e in ev if e["t"] >= since], key=lambda e: e["t"])


def stays_from_faces(ev: list) -> list:
    """顔が写った時刻から、人ごとの滞在を組み直す。

    `visit` の `stay` は使わない（2026-09-27 より前は、その人が帰ったあとの時間まで
    足していたため）。`spans` が入っている記録ならそちらが正しいが、
    古い記録と並べるために、ここでは常に顔の時刻から組み直す。"""
    seen = collections.defaultdict(list)
    for e in ev:
        if e["kind"] == "arrive" and str(e.get("person") or "").startswith("p"):
            seen[e["person"]].append(e["t"])
    out = []
    for pid, ts in seen.items():
        ts.sort()
        first = prev = ts[0]
        for t in ts[1:]:
            if t - prev > MERGE:
                out.append((pid, first, prev))
                first = t
            prev = t
        out.append((pid, first, prev))
    return out


def main() -> None:
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 7.0
    since = time.time() - days * 86400
    ev = fetch(since)
    stays = stays_from_faces(ev)
    near = [s for s in stays if s[2] - s[1] >= LINE]
    pass_ = [s for s in stays if s[2] - s[1] < LINE]
    # 不明：人が居たのは確かだが、顔が1枚も取れていない滞在
    unknown = [e for e in ev if e["kind"] == "visit" and e.get("seen") and not (e.get("who") or e.get("spans"))]
    n_near, n_pass, n_unk = len(near), len(pass_), len(unknown)
    print("直近 %.0f日（%s 以降）" % (days, time.strftime("%m/%d %H:%M", time.localtime(since))))
    print("  立ち寄り（%.0f秒以上） %3d" % (LINE, n_near))
    print("  通り過ぎ（%.0f秒未満） %3d" % (LINE, n_pass))
    print("  不明（顔が1枚も無い滞在） %3d" % n_unk)
    if n_near + n_pass:
        lo = 100.0 * n_pass / (n_near + n_pass + n_unk) if (n_near + n_pass + n_unk) else 0
        hi = 100.0 * (n_pass + n_unk) / (n_near + n_pass + n_unk) if (n_near + n_pass + n_unk) else 0
        print("\n  通り過ぎ率 = **%.0f%% 〜 %.0f%%**" % (lo, hi))
        print("    下限＝不明を「立ち寄った」とみなした場合／上限＝不明を「通り過ぎた」とみなした場合")
        print("    ※ 不明 %d件。**率を1つの数字で書かない。**" % n_unk)
    else:
        print("\n  材料がありません")
    # 顔が1枚しか取れず、長さ0秒になった滞在（＝本当に通り過ぎたとは限らない）
    zero = [s for s in pass_ if s[2] - s[1] == 0]
    print("\n  通り過ぎのうち、顔が1枚だけで長さ0秒だったもの: %d件" % len(zero))
    print("  （10分居たが顔が1回しか撮れなかった場合も、ここに入る）")


if __name__ == "__main__":
    main()
