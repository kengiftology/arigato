# -*- coding: utf-8 -*-
"""地霊C3への無線コマンド（UDP 5006・シリアルと同じ文法）。
使い方:
  python spirit_net.py find              → 同じWiFi内の地霊を探す（IP不要）
  python spirit_net.py stat              → 状態まとめ（IP/SRC/M/N/CARE/QUIET）
  python spirit_net.py m 0.7            → コマンド送信（stat/care/quiet on/src ... 何でも）
  python spirit_net.py -i 192.168.0.55 stat → IP指定
IP省略時はブロードキャストで探してから送る。
"""
import socket
import sys

try:                                     # Windowsのコンソールで日本語が化けない・落ちないように
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PORT = 5006


def send(ip, cmd, timeout=3.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    if ip is None:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        ip = "255.255.255.255"
    s.sendto(cmd.encode(), (ip, PORT))
    try:
        data, addr = s.recvfrom(1024)
        return addr[0], data.decode(errors="ignore")
    except (socket.timeout, OSError):   # 相手が再起動中だとWindowsはOSErrorを投げる
        return None, None
    finally:
        s.close()


def main():
    args = sys.argv[1:]
    ip = None
    if args[:1] == ["-i"]:
        ip = args[1]
        args = args[2:]
    if not args or args[0] == "find":
        who, resp = send(ip, "ping")
        print(f"{who}: {resp.strip()}" if who else "見つかりません（同じWiFi？電源？）")
        return
    cmd = " ".join(args)
    who, resp = send(ip, cmd)
    if who:
        print(f"[{who}] {resp.strip()}")
    else:
        print("応答なし（同じWiFiにいるか・地霊の電源を確認）")


if __name__ == "__main__":
    main()
