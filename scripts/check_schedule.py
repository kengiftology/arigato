# -*- coding: utf-8 -*-
"""日程の矛盾を機械で見つける（2026-09-24）。

  python3 scripts/check_schedule.py

93件に増えて、人の目では追えなくなった。今日は番号の衝突（2.7）と
親子の食い違い（4.6 が見送りなのに子が未着手）を人が見つけたが、次は機械が見つける。
"""
import datetime as dt
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import notion_api as n

FREEZE = dt.date(2026, 10, 4)
OPEN = ("未着手", "進行中")


def d(s):
    return dt.date.fromisoformat(s) if s else None


def main():
    ts = n.tasks()
    by_id = {t["id"]: t for t in ts}
    by_num = {t["num"]: t for t in ts}
    today = dt.date.today()
    found = []

    # 依存（Blocked by）を引く
    rows = {}
    q = n.api(f"databases/{n.TASK_DB}/query", "POST", {"page_size": 100})
    pages = q["results"]
    while q.get("has_more"):
        q = n.api(f"databases/{n.TASK_DB}/query", "POST",
                  {"page_size": 100, "start_cursor": q["next_cursor"]})
        pages += q["results"]
    for p in pages:
        rows[p["id"]] = {
            "blocked_by": [r["id"] for r in p["properties"]["Blocked by"]["relation"]],
            "parent": [r["id"] for r in p["properties"]["Parent item"]["relation"]],
        }

    for t in ts:
        r = rows.get(t["id"], {})
        # ① 依存の逆転：先にやるべきものの期限が、後のものより遅い
        for b in r.get("blocked_by", []):
            before = by_id.get(b)
            if (before and before["due"] and t["due"]
                    and t["state"] in OPEN and before["state"] in OPEN
                    and d(before["due_end"]) > d(t["due"])):
                found.append(("依存の逆転", "%s(%s) は %s(%s) を待つのに、期限が後ろ"
                              % (t["num"], t["due"], before["num"], before["due"])))
        # ② 親子の期限の食い違い
        for pa in r.get("parent", []):
            par = by_id.get(pa)
            # 親は期間（始まり〜終わり）を持つので、終わりと比べる
            if par and par["due_end"] and t["due"] and d(t["due"]) > d(par["due_end"]):
                found.append(("親の期間から外れる子", "%s(%s) が親 %s(〜%s) の外"
                              % (t["num"], t["due"], par["num"], par["due_end"])))
            if par and par["state"] in ("済", "見送り") and t["state"] in OPEN:
                found.append(("親子の食い違い", "親 %s が %s なのに、子 %s が %s"
                              % (par["num"], par["state"], t["num"], t["state"])))
        # ③ 凍結後に残る凍結前必須
        if t["must"] and t["state"] in OPEN and t["due"] and d(t["due"]) > FREEZE:
            found.append(("凍結に間に合わない", "%s は凍結前必須なのに期限 %s" % (t["num"], t["due"])))
        # ④ 期限切れの未着手
        if t["state"] in OPEN and t["due"] and d(t["due"]) < today:
            found.append(("期限切れ", "%s %s（%s・%s）" % (t["num"], t["name"][:28], t["who"], t["due"])))
        # ⑤ 担当が空
        if not t["who"] and t["state"] in OPEN:
            found.append(("担当が空", "%s %s" % (t["num"], t["name"][:28])))
        # ⑥ 番号の親がいない
        if "." in t["num"] and t["num"].rsplit(".", 1)[0] not in by_num:
            found.append(("親の番号が無い", "%s の親 %s が台帳に無い"
                          % (t["num"], t["num"].rsplit(".", 1)[0])))

    # ⑦ 同じ担当・同じ日に3件以上（小項目だけ）
    load = {}
    for t in ts:
        if t["level"] == "小" and t["state"] in OPEN and t["due"]:
            load.setdefault((t["due"], t["who"]), []).append(t["num"])
    for (due, who), nums in sorted(load.items()):
        if len(nums) >= 3 and d(due) >= today:
            found.append(("詰まり", "%s %s に %d件（%s）" % (due, who, len(nums), " ".join(nums))))

    # ⑧ 現場の日に、現場が要らない本人の仕事が積まれていないか
    site_days = {t["due"] for t in ts
                 if t.get("site") and t["state"] in OPEN and t["due"]}
    for t in ts:
        if (t["who"] == "本人" and t["state"] in OPEN and t["due"] in site_days
                and not t.get("site") and t["level"] == "小"):
            found.append(("現場の日に机の仕事", "%s %s（%s）— 現場が要らないので別の日へ動かせる"
                          % (t["num"], t["name"][:30], t["due"])))

    # ⑨ 番号の重複
    seen = {}
    for t in ts:
        seen.setdefault(t["num"], []).append(t["name"][:20])
    for num, names in seen.items():
        if len(names) > 1:
            found.append(("番号の重複", "%s が %d件（%s）" % (num, len(names), " / ".join(names))))

    print("見た件数", len(ts), "／", dt.datetime.now().strftime("%m/%d %H:%M"))
    if not found:
        print("矛盾なし")
        return
    kind = None
    for k, msg in sorted(found):
        if k != kind:
            print("\n■", k)
            kind = k
        print("  -", msg)


if __name__ == "__main__":
    main()
