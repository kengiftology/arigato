"""C3 の宛先を本物へ戻す。届くまで何度でも送る。

止めている間、C3 は届かない相手への接続待ちで塞がっていて、無線の命令にほとんど答えない。
答えるのは、見張りに起こし直された直後などの短い隙間だけ。だから**打ち続けて隙間を捉える**。
"""
import socket, sys, time

sys.stdout.reconfigure(encoding="utf-8")
C3 = ("192.168.0.233", 5006)
GOOD = "src arigato-3ipecjbnha-an.a.run.app 443"
END = time.time() + 900


def send(c, wait=1.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(wait)
    try:
        s.sendto(c.encode(), C3)
        return s.recvfrom(2048)[0].decode("utf-8", "replace")
    except Exception:
        return ""
    finally:
        s.close()


n = 0
while time.time() < END:
    n += 1
    o = send(GOOD)
    if o:
        print(time.strftime("%H:%M:%S"), "届きました（%d回目）:" % n,
              " / ".join(l.strip() for l in o.splitlines() if l.strip()), flush=True)
        time.sleep(3)
        st = send("stat", 4)
        for l in st.splitlines():
            if l.startswith(("SRC", "NET", "HIDE", "CARE", "PIR")):
                print("   ", l.strip(), flush=True)
        if "arigato" in st:
            print(time.strftime("%H:%M:%S"), "宛先が本物に戻りました", flush=True)
            break
    if n % 20 == 0:
        print(time.strftime("%H:%M:%S"), "%d回送りました。まだ届きません" % n, flush=True)
    time.sleep(1)
else:
    print(time.strftime("%H:%M:%S"), "15分送り続けても届きません。電源の入れ直しが要ります", flush=True)
