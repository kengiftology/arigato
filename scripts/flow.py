# -*- coding: utf-8 -*-
"""地霊のひとまわりを1枚の図に書き出す（2026-09-28）。

  python3 scripts/flow.py > flow.html

本人のロッカーの流れ図（work/esp32_door_lock/tools/locker_flow_diagram.py）の配色に合わせる：
  緑 ふつうの流れ／赤 警告／黄 注意／青 自動／紫 データ／灰 起こり得ない組み合わせ

各節は4つの状態を持つ：
  未実装 ／ 実装済み・現地で未検証 ／ 現地で検証済み（成功率つき） ／ 証拠は観察期
"""
import html

W, H = 1320, 1020
LANES = [("人の流れ", 120), ("顔と名前", 430), ("声と対話", 740), ("場所と覚え", 1050)]

# (lane, row, 番号, 題, 状態, 数字, 注記)
# 状態: done=現地で検証済み / built=実装済み・現地未検証 / todo=未実装 / obs=証拠は観察期 / warn=穴
NODES = [
 (0,0,"—","人が来る（人感・動き）","done","取りこぼし 0/18（9/23）",""),
 (0,1,"—","滞在が始まる","done","",""),
 (0,2,"2.4.1","立ち寄りか通り過ぎか","built","18〜41%（立ち寄り36・通り過ぎ11・不明14）","線は本人待ち"),
 (0,3,"—","滞在が閉じる","done","",""),
 (0,4,"2.2.1","なつき度が上がる","obs","","材料は観察期"),

 (1,0,"—","顔を見つける","done","",""),
 (1,1,"3.2.3","70px 未満は見送る","done","70px で外れ 0／45px で 88中7","測って下げなかった"),
 (1,2,"3.1.1","誰かを見分ける（線0.40）","warn","4回に1回（9/23・24.5%）","9/29 に見切り"),
 (1,3,"3.1.4","初めての人を覚える","done","0.15・別人0・名前1094枚・ID9",""),
 (1,4,"2.6.5","人 × 区画の履歴","todo","","9/28 に A が入れる"),

 (2,0,"2.7.1","居るうちに声が鳴るか","done","175回中174回（99%）",""),
 (2,1,"2.5.4","許しを出さない・拾って返す","done","9/26 の5往復は全部聞き取れていた","#17 の初例"),
 (2,2,"2.5.5","実機で1往復通るか","built","","起点 9/25 19:39"),
 (2,3,"2.6.7","よく整えた区画に触れて話す","todo","","9/29 が最後"),
 (2,4,"2.5.1","しゃべり方を真似る","obs","","said が溜まってから"),

 (3,0,"2.6.2","5か所を順に見回る","done","1周 126〜133秒",""),
 (3,1,"2.6.1","向きを合わせて撮る","done","zone_skip 0（12:50 の直し以降）","補正はシンクだけに"),
 (3,2,"—","前と見比べる","done","寿命超え 0・古さ中央値 2396秒",""),
 (3,3,"2.6.2b","中断した区画を拾い直す","warn","完走3・打ち切り1（9/27）","拾い直しは確認中"),
 (3,4,"—","片づけとして覚える","warn","9/25以来の1件目 = 9/26 19:22 コンロ p16",""),
]
EDGES = [((0,0),(0,1)),((0,1),(0,2)),((0,2),(0,3)),((0,3),(0,4)),
         ((0,0),(1,0)),((1,0),(1,1)),((1,1),(1,2)),((1,2),(1,3)),((1,2),(1,4)),
         ((1,2),(2,0)),((2,0),(2,1)),((2,1),(2,2)),((2,2),(2,3)),((2,3),(2,4)),
         ((0,3),(3,0)),((3,0),(3,1)),((3,1),(3,2)),((3,2),(3,3)),((3,3),(3,4)),
         ((3,4),(1,4)),((1,4),(2,3)),((3,4),(0,4))]
STATUS = {
 "done": ("現地で検証済み", "var(--ok)"),
 "built":("実装済み・現地で未検証", "var(--auto)"),
 "todo": ("未実装", "var(--todo)"),
 "obs":  ("証拠は観察期", "var(--data)"),
 "warn": ("穴が残っている", "var(--warn)"),
}
BW, BH, ROW0, ROWH = 250, 96, 120, 176


def pos(lane, row):
    return LANES[lane][1], ROW0 + row * ROWH


def main():
    parts = []
    for (a, b) in EDGES:
        x1, y1 = pos(*a); x2, y2 = pos(*b)
        if a[0] == b[0]:
            parts.append('<path d="M%d,%d L%d,%d" class="e"/>' % (x1, y1 + BH // 2, x2, y2 - BH // 2))
        else:
            parts.append('<path d="M%d,%d C%d,%d %d,%d %d,%d" class="e cross"/>'
                         % (x1 + BW // 2, y1, x1 + BW // 2 + 60, y1, x2 - BW // 2 - 60, y2, x2 - BW // 2, y2))
    for lane, row, num, title, st, rate, note in NODES:
        x, y = pos(lane, row)
        label, color = STATUS[st]
        parts.append(
            '<g class="n %s"><rect x="%d" y="%d" width="%d" height="%d" rx="5" style="stroke:%s"/>'
            '<text x="%d" y="%d" class="num">%s</text>'
            '<text x="%d" y="%d" class="ttl">%s</text>'
            '<text x="%d" y="%d" class="st" style="fill:%s">%s</text>'
            '%s%s</g>' % (
                st, x - BW // 2, y - BH // 2, BW, BH, color,
                x - BW // 2 + 12, y - BH // 2 + 20, html.escape(num),
                x - BW // 2 + 12, y - BH // 2 + 42, html.escape(title),
                x - BW // 2 + 12, y + BH // 2 - 30, color, html.escape(label),
                ('<text x="%d" y="%d" class="rt">%s</text>' % (x - BW // 2 + 12, y + BH // 2 - 12, html.escape(rate))) if rate else "",
                ('<text x="%d" y="%d" class="nt">%s</text>' % (x + BW // 2 - 12, y - BH // 2 + 20, html.escape(note))) if note else ""))
    lanes = "".join('<text x="%d" y="52" class="lane">%s</text>' % (x, html.escape(name)) for name, x in LANES)
    counts = {k: sum(1 for n in NODES if n[4] == k) for k in STATUS}
    tpl = open(__file__.rsplit("/", 1)[0] + "/flow_template.html", encoding="utf-8").read()
    print(tpl.replace("{{SVG}}", "".join(parts) + lanes)
             .replace("{{W}}", str(W)).replace("{{H}}", str(H))
             .replace("{{DONE}}", str(counts["done"])).replace("{{BUILT}}", str(counts["built"]))
             .replace("{{TODO}}", str(counts["todo"])).replace("{{WARN}}", str(counts["warn"]))
             .replace("{{OBS}}", str(counts["obs"])).replace("{{N}}", str(len(NODES))))


if __name__ == "__main__":
    main()
