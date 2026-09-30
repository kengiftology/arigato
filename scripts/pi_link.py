# -*- coding: utf-8 -*-
"""ラズパイの無線の質を、いつも同じ条件で測る（2026-09-30）。

中継器を入れる前と後を**同じ測り方で並べる**ための道具。
9/19・9/29 の数字と比べられるよう、条件を固定してある。

  ・**片方の無線だけを動かして測る**（内蔵と子機は同じ2.4GHzで、同時に動かすと互いに邪魔する。
    9/29、それを知らずに測って「子機のほうが悪い」と誤った）
  ・ping は **1400バイト × 40回・0.3秒おき**（小さい荷物では詰まりが出ない）
  ・**90秒後に必ず内蔵を戻す**仕掛けを先に置く（遠隔で締め出されないため）

    python scripts/pi_link.py            # 手元の PC から、ラズパイに入って測る

比べ方の目安
    落ち 0%・往復 50ms 前後  … 映像を流せる
    落ち 20%前後            … 9/19 はここで、映像が壊れて0枚だった
"""
import subprocess
import sys

PI = "kengk@192.168.0.102"
SCRIPT = r"""
cat > /tmp/link.sh <<'EOS'
#!/bin/bash
( sleep 90; nmcli con up tp ifname wlan0 >/dev/null 2>&1 ) &   # 何があっても戻す
exec > /tmp/link.txt 2>&1
for dev in wlan1 wlan0; do
  other=$([ "$dev" = wlan1 ] && echo wlan0 || echo wlan1)
  echo "=== $dev だけで測る ==="
  nmcli dev disconnect $other >/dev/null 2>&1
  [ "$dev" = wlan0 ] && nmcli con up tp     ifname wlan0 >/dev/null 2>&1
  [ "$dev" = wlan1 ] && nmcli con up tp-usb ifname wlan1 >/dev/null 2>&1
  sleep 6
  iw dev $dev link 2>/dev/null | grep -iE 'SSID|signal:|tx bitrate'
  ping -I $dev -c 40 -i 0.3 -s 1400 -W 2 192.168.0.1 2>&1 | tail -2
done
nmcli con up tp ifname wlan0 >/dev/null 2>&1
echo "=== つながっている先 ==="
nmcli -t -f DEVICE,STATE,CONNECTION dev | grep wlan
EOS
chmod +x /tmp/link.sh
sudo -n nohup /tmp/link.sh >/dev/null 2>&1 &
echo "測っています（約60秒）"
"""


def main() -> None:
    subprocess.run(["ssh", "-o", "BatchMode=yes", PI, SCRIPT], timeout=120)
    print("60秒ほど待ってから、次で結果を読みます：")
    print("  ssh %s 'cat /tmp/link.txt'" % PI)


if __name__ == "__main__":
    main()
