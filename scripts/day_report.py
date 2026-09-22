# -*- coding: utf-8 -*-
"""その日の数字を1枚で出す（2026-09-16）。毎晩これを実行する。

    python scripts/day_report.py               # 今日（日本時間）
    python scripts/day_report.py 2026-09-16    # 日付を指定

やること：
  1. 本番の記録（/spirit/log）から、その日の出来事を数える
  2. docs/毎日の数字.csv の、その日の行を書き直す（無ければ足す）
  3. docs/毎日の数字.png に、日ごとの折れ線（ランチャート）を描き直す

■ 記録が取りきれない日がある
  /spirit/log は新しい順に最大1000件しか返さない。9/16 は1000件で約22時間ぶんだった
  （6割がサーバーの起動 boot）。その日の始まりまで届いていなければ、そう書いて出す。
  過去の日を後から数えることはできないので、その日のうちに実行する。

■ 数えられないもの
  「別人と間違えた」は記録だけでは分からない。誰と照合されたかと、そのときの写真の
  場所を並べて出すので、写真を見て数える。シンクの比較が合っていたかも同じ。

■ 間引かれている数
  「顔が小さい」などの見送りは、サーバーが30秒に1件に間引いて書いている（_log_small）。
  回数ではなく、何が多いかを見るための数。
"""
import collections
import csv
import datetime as dt
import json
import os
import sys
import urllib.request

SERVER = "https://arigato-3ipecjbnha-an.a.run.app"
JST = dt.timezone(dt.timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "..", "docs", "毎日の数字.csv")
PNG_PATH = os.path.join(HERE, "..", "docs", "毎日の数字.png")

# 見分けの結果を、5つの箱に分ける（9/15のタスクの内訳）。
# 「別人と間違えた」は記録から分からないので箱が無い（写真を見て数える）。
FACE_NONE = {"no_face", "too_small_to_match", "too_small", "cut_off",
             "looking_down", "not_front"}          # 顔が写らなかった・使えなかった
FACE_OBJECT = {"looks_like_object", "furniture"}   # 物を人と思いかけて弾いた
FACE_UNSURE = {"one_frame", "not_enough", "passing_by"}   # 確かめきれず見送った
CAMERA_GAP = 300   # カメラだけが人を見た記録は、これ以上あいたら別の1回とみなす（秒）

COLUMNS = ["日付", "記録の欠け", "人が来た", "うち誰か分かった", "照合できた",
           "新しく登録", "物として弾いた", "顔が取れない", "見送り",
           "その人向けの声", "声ぜんぶ", "シンク比較が成立", "シンク比較を飛ばした",
           "なつき度が上がった", "滞在の締め", "サーバー起動",
           "喜んだ（生）", "喜んだ滞在"]


def _page(before: float, limit: int = 1000) -> list:
    url = SERVER + "/spirit/log?limit=%d" % limit
    if before:
        url += "&before=%.6f" % before
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)["events"]


def fetch(day: dt.date | None = None, limit: int = 1000, max_pages: int = 40) -> list:
    """その日の記録を、1000件ずつ遡って取り切る（2026-09-22）。

    /spirit/log は1回に最大1000件しか返さないので、混んだ日は半日ぶんしか届かず、
    9/19〜9/21 はその日の前半を失った。`before=` で「いちばん古い t より前」を
    繰り返し頼み、その日の始まりより前まで届いたら止める。
    古いサーバー（before を知らない）だと同じ1000件が返ってくるので、
    遡れなかったらそこで止める（前と同じ動きに戻るだけ）。"""
    if day is None:
        return _page(0, limit)
    start = dt.datetime(day.year, day.month, day.day, tzinfo=JST).timestamp()
    before = start + 86400 + 3600          # その日の終わり＋1時間（滞在の締めが日をまたぐ分）
    got, seen = [], set()
    for _ in range(max_pages):
        page = _page(before, limit)
        fresh = [e for e in page if (e.get("t"), e.get("kind")) not in seen]
        if not fresh:
            break
        for e in fresh:
            seen.add((e.get("t"), e.get("kind")))
        got.extend(fresh)
        oldest = min(e["t"] for e in page)
        if oldest >= before or oldest < start - 3600 or len(page) < limit:
            break                          # 遡れない／その日の前まで届いた／もう無い
        before = oldest
    return got


def hm(t):
    return dt.datetime.fromtimestamp(t, JST).strftime("%H:%M")


JOY_MERGE_GAP = 1800   # 締まっていない滞在は、喜びどうしが30分以内なら同じ滞在（VISIT_MERGE_GAP と同じ）
JOY_EDGE = 120         # 滞在の端の前後これだけは、その滞在に入れる（締めの記録は数秒遅れる）


def joy_visits(joys: list, closes: list) -> tuple:
    """キャラが喜んだ記録（c3_joy）を「滞在あたり1回」にまとめる（2026-09-22 本人決定）。

    C3 は「90秒気配が途切れたら滞在おわり」、クラウドは「30分以内の出入りは同じ滞在」と
    区切りが違い、少し離れて戻ると C3 はもう一度喜ぶ（それは良い）。ただし研究の数字は
    増やさない。滞在の区切りはクラウドの visit 記録（締めの時刻と、人ごとの滞在秒）に合わせ、
    どの滞在にも入らない喜び（まだ締まっていない滞在）は、30分以内どうしを束ねる。
    人は問わない：9/22 15:23 より前の記録は、喜んだ相手が取り違えられていることがあるため。
    人ごとの内訳は who_from == "served"（段階を渡した相手を覚えていた）の記録だけで出す。"""
    spans = []
    for c in closes:
        stay = c.get("stay") or {}
        longest = max(stay.values()) if isinstance(stay, dict) and stay else 0
        spans.append((c["t"] - longest - JOY_EDGE, c["t"] + JOY_EDGE))
    groups, loose = set(), []
    for j in joys:
        hit = next((i for i, (a, b) in enumerate(spans) if a <= j["t"] <= b), None)
        if hit is None:
            loose.append(j["t"])
        else:
            groups.add(("visit", hit))
    loose.sort()
    last = None
    for t in loose:
        if last is None or t - last > JOY_MERGE_GAP:
            groups.add(("loose", t))
        last = t
    by_person = {}
    for j in joys:
        if j.get("who_from") == "served" and j.get("person"):
            by_person[j["person"]] = by_person.get(j["person"], 0) + 1
    return len(groups), by_person


def count(day: dt.date, events: list) -> tuple:
    start = dt.datetime(day.year, day.month, day.day, tzinfo=JST).timestamp()
    end = start + 86400
    oldest = min(e["t"] for e in events) if events else end
    gap_h = max(0.0, (oldest - start) / 3600)          # その日の始まりから何時間ぶん欠けているか
    ev = sorted((e for e in events if start <= e["t"] < end), key=lambda e: e["t"])

    arrive = [e for e in ev if e["kind"] == "arrive"]
    known = [e for e in arrive if e.get("person", "unknown") != "unknown"
             and e.get("state") in ("ready", "egg")]
    new = [e for e in arrive if e.get("state") == "new_egg"]
    why = collections.Counter(e.get("why") for e in arrive if e.get("person") == "unknown")

    # 人が来てから居なくなるまでを1回と数え、その間に誰か分かったか。
    visits, cur = [], None
    for e in ev:
        if e["kind"] == "presence":
            if not e.get("empty"):
                if cur is None:
                    cur = {"from": e["t"], "to": None, "who": set()}
            elif cur is not None:
                cur["to"] = e["t"]
                visits.append(cur)
                cur = None
        elif e["kind"] == "arrive" and cur is not None and e.get("person", "unknown") != "unknown":
            cur["who"].add(e["person"])
    if cur is not None:
        visits.append(cur)
    for v in visits:
        v["by"] = "人感"

    # 人感の合図が無いのにカメラが人を見た時間も、来た1回として数える。
    # 9/16 19:42 は顔で p01 と分かったのに、人感の合図が無く、どの来訪にも入らなかった。
    def inside(t):
        return any(v["from"] - 60 <= t <= (v["to"] or end) + 60 for v in visits)
    loose = [e for e in arrive if not inside(e["t"])]
    cam = []
    for e in loose:
        if cam and e["t"] - cam[-1]["to"] <= CAMERA_GAP:
            cam[-1]["to"] = e["t"]
        else:
            cam.append({"from": e["t"], "to": e["t"], "who": set(), "by": "カメラ"})
        if e.get("person", "unknown") != "unknown":
            cam[-1]["who"].add(e["person"])
    visits = sorted(visits + cam, key=lambda v: v["from"])

    voice = [e for e in ev if e["kind"] == "voice"]
    for_person = [e for e in voice if "for_p" in (e.get("line") or "")]
    zone = [e for e in ev if e["kind"] == "zone"]
    zone_skip = [e for e in ev if e["kind"] == "zone_skip"]
    bond = [e for e in ev if e["kind"] == "bond_up"]
    closes = [e for e in ev if e["kind"] in ("visit", "visit_short")]
    joys = [e for e in ev if e["kind"] == "c3_joy"]
    joy_n, joy_by = joy_visits(joys, [c for c in closes if c["kind"] == "visit"])

    row = {
        "日付": day.isoformat(),
        "記録の欠け": "%.1f時間" % gap_h if gap_h > 0 else "",
        "人が来た": len(visits),
        "うち誰か分かった": sum(1 for v in visits if v["who"]),
        "照合できた": len(known),
        "新しく登録": len(new),
        "物として弾いた": sum(why[w] for w in FACE_OBJECT),
        "顔が取れない": sum(why[w] for w in FACE_NONE),
        "見送り": sum(why[w] for w in FACE_UNSURE),
        "その人向けの声": len(for_person),
        "声ぜんぶ": len(voice),
        "シンク比較が成立": len(zone),
        "シンク比較を飛ばした": len(zone_skip),
        "なつき度が上がった": len(bond),
        "滞在の締め": len(closes),
        "サーバー起動": sum(1 for e in ev if e["kind"] == "boot"),
        "喜んだ（生）": len(joys),
        "喜んだ滞在": joy_n,
    }
    detail = {"visits": visits, "known": known, "new": new, "why": why,
              "for_person": for_person, "zone": zone, "zone_skip": zone_skip,
              "bond": bond, "closes": closes, "joys": joys, "joy_by": joy_by,
              "shots": [e for e in ev if e["kind"] == "shot" and e.get("person") not in (None, "small")]}
    return row, detail


def print_report(row: dict, d: dict) -> None:
    p = print
    p("■ %s の数字" % row["日付"])
    if row["記録の欠け"]:
        p("  ※ 記録が取りきれていない：その日の始まりから %s ぶん欠けている（/spirit/log は最新1000件まで）"
          % row["記録の欠け"])
    p("")
    p("人が来た回数        %d（うち誰か分かった %d）" % (row["人が来た"], row["うち誰か分かった"]))
    for v in d["visits"]:
        p("   %s〜%s  %-4s %s" % (hm(v["from"]), hm(v["to"]) if v["to"] else "（居るまま）",
                                v["by"], "・".join(sorted(v["who"])) or "分からない"))
    p("")
    p("顔の見分けの内訳")
    p("   照合できた          %d   %s" % (row["照合できた"], dict(collections.Counter(e["person"] for e in d["known"]))))
    p("   新しい人として登録  %d   %s" % (row["新しく登録"], [e["person"] for e in d["new"]]))
    p("   物として弾いた      %d" % row["物として弾いた"])
    p("   顔が取れない        %d   （30秒に1件に間引き）" % row["顔が取れない"])
    p("   見送り              %d   （同上）" % row["見送り"])
    p("   別人と間違えた      ？   → 下の写真を見て数える")
    for e in d["shots"][:20]:
        p("      %s %s %s" % (hm(e["t"]), e.get("person"), e.get("url")))
    if d["why"]:
        p("   見送りの理由: %s" % dict(d["why"].most_common()))
    p("")
    p("その人向けの声      %d（声ぜんぶ %d）" % (row["その人向けの声"], row["声ぜんぶ"]))
    for e in d["for_person"]:
        p("   %s %s" % (hm(e["t"]), e.get("line")))
    p("")
    p("シンクの比較        成立 %d／飛ばした %d   → 合っていたかは写真で見る" %
      (row["シンク比較が成立"], row["シンク比較を飛ばした"]))
    for e in d["zone"]:
        ch = "、".join("%s（%s）" % (c.get("what"), c.get("how")) for c in e.get("changes") or []) or "変化なし"
        p("   %s %s %s" % (hm(e["t"]), "・".join(e.get("who") or []) or "誰も居ない", ch))
    for e in d["zone_skip"]:
        p("   %s 飛ばした：%s %s" % (hm(e["t"]), e.get("why"), e.get("shift") or ""))
    p("")
    p("なつき度が上がった  %d" % row["なつき度が上がった"])
    for e in d["bond"]:
        p("   %s %s → %s（%s）" % (hm(e["t"]), e.get("person"), e.get("bond"), e.get("why")))
    p("")
    p("キャラが喜んだ      生 %d 回 ／ 喜んだ滞在 %d" % (row["喜んだ（生）"], row["喜んだ滞在"]))
    for e in d["joys"]:
        p("   %s 段階%s %s%s" % (hm(e["t"]), e.get("stage"), e.get("person") or "?",
                            "" if e.get("who_from") == "served" else "（相手は参考）"))
    if d["joy_by"]:
        p("   人ごと（渡した相手が確かな分だけ）：%s" % d["joy_by"])
    p("")
    p("滞在の締め %d／サーバー起動 %d" % (row["滞在の締め"], row["サーバー起動"]))


def save_csv(row: dict) -> list:
    """その日の行を書き直す。ただし、**前より少ない数では上書きしない**（2026-09-20）。

    `/spirit/log` は最新1000件までしか返さない。混んだ日は、夜にもう一度走らせると
    その日の前半が窓から外れていて、「人が来た5」のような小さい数が返る。
    9/20 にこれで 9/19 の行（100件）を 5件で潰した。
    少ない数で走らせたときは、書かずに知らせるだけにする（--force で上書きできる）。"""
    rows = []
    old = None
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("日付") == row["日付"]:
                    old = r
                else:
                    rows.append(r)
    if old and "--force" not in sys.argv:
        try:
            was, now = int(old.get("人が来た") or 0), int(row["人が来た"])
        except ValueError:
            was = now = 0
        if was > now:
            print("\n※ 前の行のほうが多いので書き替えません（前 %d件／今回 %d件）。"
                  "記録が窓から流れたあとに走らせると、こうなります。"
                  "それでも書き替えるなら --force を付けてください。" % (was, now))
            rows.append(old)
            rows.sort(key=lambda r: r["日付"])
            return rows
    rows.append({k: row[k] for k in COLUMNS})
    rows.sort(key=lambda r: r["日付"])
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return rows


def draw(rows: list) -> None:
    """日ごとの折れ線。1枚に1つの数だけ描く（目盛りの違う数を同じ軸に載せない）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.ticker import MaxNLocator
    for name in ("Yu Gothic", "Meiryo", "MS Gothic"):
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            break

    panels = ["人が来た", "うち誰か分かった", "新しく登録", "その人向けの声",
              "シンク比較が成立", "なつき度が上がった"]
    days = [r["日付"][5:] for r in rows]
    ink, muted, grid, line = "#1f2328", "#6b7280", "#e5e7eb", "#2f6fd6"
    fig, axes = plt.subplots(2, 3, figsize=(11, 5.6), sharex=True)
    for ax, key in zip(axes.flat, panels):
        vals = [int(r[key] or 0) for r in rows]
        ax.plot(range(len(vals)), vals, color=line, linewidth=2,
                marker="o", markersize=7, markeredgecolor="white", markeredgewidth=2)
        if vals:
            ax.annotate(str(vals[-1]), (len(vals) - 1, vals[-1]), textcoords="offset points",
                        xytext=(0, 8), ha="center", color=ink, fontsize=10)
        ax.set_title(key, loc="left", color=ink, fontsize=11)
        ax.set_ylim(bottom=0, top=max(vals + [1]) * 1.25)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=4))   # 回数なので整数だけ
        ax.grid(axis="y", color=grid, linewidth=1)
        ax.set_axisbelow(True)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.spines["bottom"].set_color(grid)
        ax.tick_params(colors=muted, labelsize=9, length=0)
        ax.set_xticks(range(len(days)))
        ax.set_xticklabels(days, rotation=0)
    fig.suptitle("毎日の数字（本番の記録から・日本時間）", x=0.01, ha="left", color=ink, fontsize=13)
    fig.tight_layout()
    fig.savefig(PNG_PATH, dpi=120, facecolor="white")
    plt.close(fig)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    day = (dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
           else dt.datetime.now(JST).date())
    row, detail = count(day, fetch(day))
    print_report(row, detail)
    rows = save_csv(row)
    try:
        draw(rows)
        print("\n書いた: %s / %s" % (os.path.normpath(CSV_PATH), os.path.normpath(PNG_PATH)))
    except ImportError:
        print("\n書いた: %s（matplotlib が無いので図は省略）" % os.path.normpath(CSV_PATH))


if __name__ == "__main__":
    main()
