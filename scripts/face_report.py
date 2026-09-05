# -*- coding: utf-8 -*-
"""顔がどれだけ取れたかを数える（2026-09-05）。

実測のあいだ、これを実行すれば「何が起きたか」が数字で出る。
ログを目で追うと、判断の記録に埋もれて顔の話が見えなくなる。

    python scripts/face_report.py            # 直近24時間
    python scripts/face_report.py 48         # 直近48時間

読み方は docs/引き継ぎ_カメラと人の識別_2026-09-05.md の
「次にやること」の表を参照。
"""
import collections
import json
import sys
import time
import urllib.request

SERVER = "https://arigato-3ipecjbnha-an.a.run.app"

# arrive の why が何を意味するか。実測の場でこれを見て次の手を決める。
WHY = {
    "no_face": "顔が見つからない（後ろ頭・横顔・画面の外）",
    "too_small_to_match": "顔は見えたが小さすぎて照合できない（70px未満）",
    "too_small": "照合はできたが小さすぎて新しいIDは出せない（120px未満）",
    "cut_off": "顔が画面の端で切れている",
}


def fetch(limit=1000):
    with urllib.request.urlopen(SERVER + "/spirit/log?limit=%d" % limit, timeout=60) as r:
        return json.load(r)["events"]


def hhmm(t):
    return time.strftime("%m/%d %H:%M", time.localtime(t))


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
    since = time.time() - hours * 3600
    ev = [e for e in fetch() if e.get("t", 0) >= since]
    if not ev:
        print("直近%g時間のできごとはまだありません。" % hours)
        return

    arrive = [e for e in ev if e.get("kind") == "arrive"]
    shots = [e for e in ev if e.get("kind") == "shot"]
    print("== 直近%g時間（%s 〜）==" % (hours, hhmm(since)))
    print("できごと %d件 / 顔まわり %d件 / 残した写真 %d枚"
          % (len(ev), len(arrive), len(shots)))
    print()

    # --- 誰かに結びついた回数 ---------------------------------------
    named = [e for e in arrive if e.get("person") and e["person"] != "unknown"]
    print("[1] 誰かに結びついた回数： %d" % len(named))
    by_pid = collections.Counter(e["person"] for e in named)
    for pid, n in by_pid.most_common():
        sims = [e["sim"] for e in named if e["person"] == pid and "sim" in e]
        new = sum(1 for e in named if e["person"] == pid and e.get("state") == "new_egg")
        line = "    %s : %d回" % (pid, n)
        if sims:
            line += "  一致度 %.2f〜%.2f" % (min(sims), max(sims))
        if new:
            line += "  ← このうち%d回は新しく作られたID" % new
        print(line)
    if not named:
        print("    なし")
    print()

    # --- 届かなかった理由 -------------------------------------------
    miss = [e for e in arrive if not e.get("person") or e["person"] == "unknown"]
    print("[2] 届かなかった理由： %d件" % len(miss))
    for why, n in collections.Counter(e.get("why", "?") for e in miss).most_common():
        pxs = [e["px"] for e in miss if e.get("why") == why and "px" in e]
        line = "    %-20s %3d件  %s" % (why, n, WHY.get(why, ""))
        if pxs:
            line += "\n        顔の幅 最小%d / 中央%d / 最大%d px" % (
                min(pxs), sorted(pxs)[len(pxs) // 2], max(pxs))
        print(line)
    if not miss:
        print("    なし")
    print()

    # --- 顔の幅がどこに散ったか -------------------------------------
    pxs = sorted(e["px"] for e in arrive if isinstance(e.get("px"), int))
    if pxs:
        bands = [("45未満（顔として扱わない）", lambda p: p < 45),
                 ("45〜69（人が居る合図だけ）", lambda p: 45 <= p < 70),
                 ("70〜119（照合できる）", lambda p: 70 <= p < 120),
                 ("120以上（新しいIDも出せる）", lambda p: p >= 120)]
        print("[3] 見えた顔の幅の散らばり（%d件）" % len(pxs))
        for name, f in bands:
            n = sum(1 for p in pxs if f(p))
            bar = "#" * int(30.0 * n / len(pxs))
            print("    %-26s %3d %s" % (name, n, bar))
        print("    最小%d / 中央%d / 最大%d px" % (pxs[0], pxs[len(pxs) // 2], pxs[-1]))
        print()

    # --- 人が居た時間帯 ---------------------------------------------
    visits = []
    for e in sorted(ev, key=lambda e: e["t"]):
        if e.get("kind") not in ("arrive", "shot"):
            continue
        if visits and e["t"] - visits[-1][1] < 300:      # 5分あけば別の来訪
            visits[-1][1] = e["t"]
            visits[-1][2] += 1
        else:
            visits.append([e["t"], e["t"], 1])
    print("[4] 人が居たとみられる時間帯： %d回" % len(visits))
    for a, b, n in visits[-12:]:
        print("    %s 〜 %s（%2d分・できごと%d件）"
              % (hhmm(a), time.strftime("%H:%M", time.localtime(b)),
                 round((b - a) / 60), n))
    if not visits:
        print("    なし")
    print()

    # --- いま登録されている人 ---------------------------------------
    with urllib.request.urlopen(SERVER + "/spirit/faces", timeout=30) as r:
        faces = json.load(r)
    print("[5] いま覚えている人： %d人" % len(faces.get("faces", [])))
    for f in faces.get("faces", []):
        print("    %s  見た回数%d  %s  はじめて %s"
              % (f["id"], f.get("shots", 0), f.get("state", ""), hhmm(f.get("born", 0))))
    if faces.get("last_error"):
        print("    ！顔まわりの直近の失敗： %s" % faces["last_error"])


if __name__ == "__main__":
    main()
