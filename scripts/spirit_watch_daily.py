"""1日ぶんの記録を自分で読んで、おかしなところだけを書き出す見張り（2026-09-24）。

**トークが起きていなくても動く形にするために作った。**
これまでは作業トークが2分おきにクラウドを読んでいたが、その形だと
(1) トークが終わると見張りも止まる (2) 起きている間ずっと費用がかかる。
Windows のタスクから1日1回呼べば、どちらも無くなる。

書き出す先：
  C:/Users/kengk/.arigato/watch/YYYY-MM-DD.md   その日のまとめ（毎日1枚）
  C:/Users/kengk/.arigato/watch/ALERT.md        おかしなところがあった日だけ（無ければ消す）

使い方：
  python spirit_watch_daily.py            きのうの朝からいままで
  python spirit_watch_daily.py 2026-09-23 その日の0時から24時まで
"""
import json, os, sys, time, urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "https://arigato-3ipecjbnha-an.a.run.app"
OUT = "C:/Users/kengk/.arigato/watch"
JUDGE_GAP_H = 3.0          # 見回りの判定がこれだけ空いたらおかしい
AIM_WARN_PX = 100.0        # 1日の「ずれ」の真ん中がこれを超えたら知らせる
# なぜ「判定が0件」を待たないか（9/24 の実例）：カメラの向きの補正が古くなると、
# ずれは一気に壊れるのではなく**じわじわ大きくなり**、ある日いきなり全部弾かれて
# 判定が止まる。丸1日気づけなかった（20時間50分）。実測では正しい向きで 0〜106px、
# 壊れたときで 274px。100px は「まだ通るが、近づいている」位置なので、
# **止まる前に直せる**。ずれは研究トークBが判定の記録に毎回残す。


def aim_len(sh):
    """ずれの大きさ（縦横をまとめた長さ）。数でなければ None。"""
    try:
        dx, dy = float(sh[0]), float(sh[1])
        return (dx * dx + dy * dy) ** 0.5
    except Exception:
        return None


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def fetch(since: float, until: float) -> list:
    """since〜until の記録を、1000件ずつ遡って全部読む。"""
    rows, before = [], until
    for _ in range(40):                      # 念のための上限（4万件）
        url = "%s/spirit/log?limit=1000&before=%f" % (BASE, before)
        try:
            with urllib.request.urlopen(url, timeout=90) as f:
                got = json.load(f).get("events", [])
        except Exception as e:
            print("記録が読めません:", e)
            break
        got = [r for r in got if r.get("t")]
        if not got:
            break
        rows += got
        oldest = min(r["t"] for r in got)
        if oldest <= since or len(got) < 1000:
            break
        before = oldest
    return sorted((r for r in rows if since <= r["t"] <= until), key=lambda r: r["t"])


def hhmm(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def main():
    if len(sys.argv) > 1:
        day = time.strptime(sys.argv[1], "%Y-%m-%d")
        since = time.mktime(day)
        until = since + 86400
        name = sys.argv[1]
    else:
        until = time.time()
        since = until - 86400
        name = time.strftime("%Y-%m-%d", time.localtime(until))

    rows = fetch(since, until)
    by = {}
    for r in rows:
        by.setdefault(r.get("kind"), []).append(r)

    joys = by.get("c3_joy", [])
    stops = by.get("stage_stop", [])
    skips = by.get("judge_skip", [])
    judges = by.get("judge", [])
    cares = [r for r in by.get("care", []) if r.get("zone")]
    boots = by.get("c3_boot", [])
    wd = [r for r in boots if r.get("why") == "wd"]
    wrong = [r for r in joys if r.get("person") and r.get("person_now")
             and r["person"] != r["person_now"]]
    noone = [r for r in cares if not r.get("who")]

    # 見回りが長く空いた所（止まっていた疑い）
    gaps, marks = [], [since] + [r["t"] for r in judges] + [until]
    for a, b in zip(marks, marks[1:]):
        if b - a > JUDGE_GAP_H * 3600:
            gaps.append((a, b))

    # カメラの向きのずれ（判定の記録に入っていれば見る）
    # 判定には `aim_shift`、飛ばした記録には前から `shift` が入っている。どちらも同じ「ずれ」
    aims = [v for v in (aim_len(r.get("aim_shift") or r.get("shift")) for r in judges + skips)
            if v is not None]
    aim_mid = median(aims)
    aim_max = max(aims) if aims else None

    bad = []
    if aim_mid is not None and aim_mid > AIM_WARN_PX:
        bad.append("カメラの向きのずれが大きい（真ん中 %.0fpx・いちばん大きいとき %.0fpx）。"
                   "%.0fpx を超えると判定が全部弾かれて止まる。**止まる前に直せる段階**"
                   % (aim_mid, aim_max, 150))
    if stops:  bad.append("段階の受け渡しを止めた記録が %d件（守りは切ってあるので、出るのはおかしい）" % len(stops))
    if skips:  bad.append("判定を飛ばした記録が %d件" % len(skips))
    if wrong:  bad.append("違う相手に喜んだ疑いが %d件" % len(wrong))
    if wd:     bad.append("見張りによる起動し直しが %d回（通信が5分以上途切れた）" % len(wd))
    if gaps:   bad.append("見回りの判定が %d回、%.1f時間以上あいた" % (len(gaps), JUDGE_GAP_H))
    if not judges: bad.append("見回りの判定が1件も無い（止まっている疑い）")

    L = ["# %s の見張り（自動・%s 作成）" % (name, time.strftime("%H:%M")), ""]
    span = lambda t: time.strftime("%m/%d %H:%M", time.localtime(t))
    L.append("記録 %d件（%s〜%s）" % (len(rows), span(since), span(until)))
    L.append("")
    L.append("| 見たもの | 数 |")
    L.append("|---|---|")
    L.append("| 見回りの判定 | %d（うち切り出しで採点 %d）|"
             % (len(judges), sum(1 for r in judges if r.get("scope") == "sink_crop")))
    L.append("| 判定を飛ばした | %d |" % len(skips))
    L.append("| 喜び | %d（相手が違う疑い %d）|" % (len(joys), len(wrong)))
    L.append("| 段階の受け渡しを止めた | %d |" % len(stops))
    L.append("| 世話 | %d（やった人が空 %d）|" % (len(cares), len(noone)))
    L.append("| C3 の起動 | %d（うち見張りによる %d）|" % (len(boots), len(wd)))
    if aim_mid is not None:
        L.append("| カメラの向きのずれ | 真ん中 %.0fpx ／ いちばん大きいとき %.0fpx（%d回ぶん）|"
                 % (aim_mid, aim_max, len(aims)))
    L.append("")
    if aim_mid is not None:
        L.append("※ ずれは日ごとに並べて見ること。**じわじわ大きくなって、ある日いきなり止まる。**")
        L.append("")
    if joys:
        L.append("喜びの中身（時刻・相手・段階・顔を確かめてからの秒数）：")
        for r in joys:
            L.append("- %s %s 段階%s %s秒%s" % (hhmm(r["t"]), r.get("person"), r.get("stage"),
                     r.get("face_ago"), "  ←相手が違う疑い" if r in wrong else ""))
        L.append("")
    for r in skips:
        L.append("- 飛ばした %s 確かさ %s ずれ %s" % (hhmm(r["t"]), r.get("resp"), r.get("shift")))
    for r in stops:
        L.append("- 止めた %s %s（写っていたのは %s）" % (hhmm(r["t"]), r.get("person"), r.get("other_best")))
    for a, b in gaps:
        L.append("- 判定が空いた %s〜%s（%.1f時間）" % (hhmm(a), hhmm(b), (b - a) / 3600))
    L.append("")
    L.append("## 見立て")
    L += ["- " + b for b in bad] if bad else ["- おかしなところはありません"]

    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, name + ".md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    alert = os.path.join(OUT, "ALERT.md")
    if bad:
        open(alert, "w", encoding="utf-8").write(
            "# %s に気になるところがあります\n\n" % name + "\n".join("- " + b for b in bad) + "\n")
    elif os.path.exists(alert):
        os.remove(alert)
    print("\n".join(L))


if __name__ == "__main__":
    main()
