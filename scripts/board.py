# -*- coding: utf-8 -*-
"""Notion のタスクDBから、締切盤（1画面で読める盤面）を作る（2026-09-24）。

  python3 scripts/board.py > /tmp/board.html

Notion のタイムラインは、85件が同じ高さで横に散らばって読めない。
ここでは「1日＝1列、その日にやることが全部そこに並ぶ」形にする。
"""
import datetime as dt
import html
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import notion_api as n

START, END = dt.date(2026, 9, 24), dt.date(2026, 10, 5)
FREEZE = dt.date(2026, 10, 4)
WD = "月火水木金土日"
PEAKS = {
    dt.date(2026, 9, 26): "撤去期の設計を決める日",
    dt.date(2026, 9, 28): "関門：積もりが見えたか",
    dt.date(2026, 9, 29): "精度を見切る日",
    dt.date(2026, 9, 30): "機能を入れられる最後の日",
    dt.date(2026, 10, 2): "未踏の一点を決める日",
    dt.date(2026, 10, 4): "凍結",
    dt.date(2026, 10, 5): "観察開始",
}
HOURS = {3: "本人 11–15時", 4: "本人 11–15時", 5: "本人 終日", 6: "本人 終日",
         0: "ゼミ", 1: "本人 終日", 2: "本人 11–15時"}
WHO_CLASS = {"本人": "me", "進行役": "lead"}


def cls_of(t):
    if t["state"] == "済":
        return "done"
    return WHO_CLASS.get(t["who"], "talk")


def short(who):
    return (who or "—").split()[0]


def main():
    tasks = [t for t in n.tasks() if t["level"] == "小" and t["due"]]
    by_day = {}
    for t in tasks:
        d = dt.date.fromisoformat(t["due"])
        by_day.setdefault(d, []).append(t)
    today = dt.date.today()
    left = (FREEZE - today).days
    done = sum(1 for t in tasks if t["state"] == "済")
    must_left = sum(1 for t in tasks if t["must"] and t["state"] != "済")

    cols = []
    d = START
    while d <= END:
        items = sorted(by_day.get(d, []), key=lambda t: (t["who"] != "本人", t["num"]))
        rows = "".join(
            '<li class="t {c}"><span class="who">{w}</span><span>{chk}<span class="num">{num}</span> {name}</span></li>'.format(
                c=cls_of(t) + (" key" if t["must"] and t["state"] != "済" else ""),
                w=html.escape(short(t["who"])),
                chk='<span class="check">✓</span> ' if t["state"] == "済" else "",
                num=html.escape(t["num"]), name=html.escape(t["name"].lstrip("✓— ").split(" ", 1)[-1]))
            for t in items)
        peak = PEAKS.get(d)
        cols.append(
            '<div class="day{today}{peak}{after}"><div class="date"><span class="d">{m}/{dd}</span>'
            '<span class="w">{w}</span></div><div class="hours">{h}</div>{pk}<ul class="tasks">{rows}</ul></div>'.format(
                today=" is-today" if d == today else "", peak=" is-peak" if peak else "",
                after=" is-after" if d > FREEZE else "",
                m=d.month, dd=d.day, w=WD[d.weekday()], h=HOURS.get(d.weekday(), ""),
                pk='<div class="peak">%s</div>' % html.escape(peak) if peak else "",
                rows=rows or '<li class="t"><span class="who">—</span><span>予定なし</span></li>'))
        d += dt.timedelta(days=1)

    tpl = open(__file__.rsplit("/", 1)[0] + "/board_template.html", encoding="utf-8").read()
    print(tpl.replace("{{DAYS}}", "\n".join(cols))
             .replace("{{LEFT}}", str(left))
             .replace("{{DONE}}", str(done))
             .replace("{{MUST}}", str(must_left))
             .replace("{{UPDATED}}", dt.datetime.now().strftime("%m/%d %H:%M")))


if __name__ == "__main__":
    main()
