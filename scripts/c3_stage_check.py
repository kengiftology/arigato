"""C3に段階が届いているかを確かめる道具（2026-09-19・研究トークD）。

つかい方（作業コピーの中から）：
    python scripts/c3_stage_check.py                 いまの様子を見るだけ
    python scripts/c3_stage_check.py --bond p02 9    p02 のなつき度を9にしてから見る
    python scripts/c3_stage_check.py --bond p02 0    もどす

見えるもの：
    クラウド /spirit/full       散らかり・放置・無人か・**段階0〜4**
    C3 の stat                  C3 が受け取った STAGE と、クラウドに通じているか

※わざと /spirit/m は叩かない。/m は「C3が来た」を数える口でもあるので、
  人が叩くと、止まっていても生きているように見えてしまう。

合言葉は C:\\Users\\kengk\\.arigato\\keys_2026-09-16.txt から読む（画面には出さない）。
"""
import argparse
import json
import socket
import sys

if hasattr(sys.stdout, "reconfigure"):       # Windows の既定(cp1252)だと日本語で落ちる
    sys.stdout.reconfigure(encoding="utf-8")

import urllib.parse
import urllib.request

BASE = "https://arigato-3ipecjbnha-an.a.run.app"
C3 = ("192.168.0.233", 5006)
KEYFILE = r"C:\Users\kengk\.arigato\keys_2026-09-16.txt"
STAGES = ("知らない", "見たことある", "顔見知り", "なついている", "べったり")


def key() -> str:
    for line in open(KEYFILE, encoding="utf-8"):
        if line.startswith("TIMELAPSE_KEY ("):
            return line.split("=", 1)[1].strip()
    raise SystemExit("合言葉が見つかりません: " + KEYFILE)


def get(path: str) -> str:
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return r.read().decode("utf-8", "replace").strip()


def post(path: str) -> str:
    req = urllib.request.Request(BASE + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace").strip()


def udp(cmd: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(cmd.encode(), C3)
        return s.recvfrom(2048)[0].decode("utf-8", "replace").strip()
    except Exception as e:
        return "(返事なし: %s)" % e
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bond", nargs=2, metavar=("だれ", "いくつ"),
                    help="先になつき度を手で書き換える（例: --bond p02 9）")
    ap.add_argument("--stage", type=int, choices=range(5), metavar="0-4",
                    help="C3に段階を手で入れて、態度が変わるか見る（机上の試験）")
    a = ap.parse_args()

    if a.stage is not None:
        print("C3に段階を手入れ:", udp("stage %d" % a.stage).replace("\n", " "))

    if a.bond:
        who, value = a.bond[0], int(a.bond[1])
        q = urllib.parse.urlencode({"who": who, "value": value, "key": key()})
        print("なつき度を書き換え:", post("/spirit/bond?" + q))

    # ※ここで /spirit/m を叩いてはいけない。/m を呼ぶと、誰が呼んでも
    #   「C3が来た」と数えられ、止まっていても生きて見える（2026-09-19 にこれで1時間迷った）。
    #   同じ値は /spirit/full からも読めるので、確認はこちらを使う。
    full = json.loads(get("/spirit/full"))
    print("\nクラウド /spirit/full :")
    if "stage" in full:
        i = full["stage"]
        print("  → 段階 %d（%s）" % (i, STAGES[i] if 0 <= i < 5 else "?"))
    else:
        print("  → 段階がまだ入っていない（本番が古いまま）")
    print("  いま居る人 : %s %s" % (full.get("person"), full.get("person_state")))
    print("  散らかり %.3f / 放置 %.3f / 無人 %s" % (
        full.get("score", 0), full.get("N", 0), full.get("empty")))

    print("\nC3 stat:")
    for line in udp("stat").splitlines():
        if line.startswith(("M ", "NET ", "PIR ", "VER ")):
            print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
