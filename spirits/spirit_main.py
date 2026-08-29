# spirit_main.py — 精霊常駐（XIAO ESP32C3 / MicroPython）
# 電源ONだけで動く: 自分がWiFi AP「spirit-net」になり、UDP(5005)で命令を受ける
#   SHOW <pid> <anim>   … そのキャラのアニメを1回再生→以後そのキャラのidleループ
#   TEXT <hex> / TCLR   … 字幕帯
#   PING                … PONG応答（生存確認）
# 命令が無ければ現在のキャラが idle で呼吸し続ける。
# 導入: mpremote fs cp spirit_main.py :main.py （旧main.pyは main_face_remote.py に退避）
import socket
import time

import network

import device_player as d

AP_SSID, AP_PASS = "spirit-net", "arigato88"
PORT = 5005
DEFAULT_PID = "cute_07"

# ---- AP起動 ----
ap = network.WLAN(network.AP_IF)
ap.active(True)
ap.config(essid=AP_SSID, security=0)                      # 一時: オープンAP（相性切り分け）
while not ap.active():
    time.sleep_ms(100)
print("AP up:", ap.ifconfig()[0])

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", PORT))
sock.setblocking(False)

d.init_lcd()

_frames_cache = {}
def frames_of(pid, anim):
    key = pid + "_" + anim
    if key not in _frames_cache:
        try:
            _frames_cache[key] = d.load("dev_%s_%s.bin" % (pid, anim))
        except OSError:
            return None
        if len(_frames_cache) > 6:               # RAM節約: 古いのを捨てる
            for k in list(_frames_cache)[:-4]:
                if k != key:
                    del _frames_cache[k]
    return _frames_cache[key]

prev = [None, None]                              # grid, pal

def draw(grid, pal):
    if prev[0] is None:
        d.clear(pal)
        d.draw_grid(grid, pal, None)
    elif prev[1] != pal:
        d.draw_grid(grid, pal, prev[0], repaint=True)
    else:
        d.draw_grid(grid, pal, prev[0])
    prev[0], prev[1] = grid, pal

def poll():
    try:
        data, addr = sock.recvfrom(300)
    except OSError:
        return None, None
    return data.decode().strip(), addr

pid = DEFAULT_PID
pending = None                                   # 次に1回だけ再生するanim

# ---- 独り言（常設の会話窓）: 足元に常に吹き出し。数秒ごとに文が入れ替わる ----
WIN_Y0 = 197
import os
wins = sorted(f for f in os.listdir() if f.startswith("win_%s_" % pid))
print("windows:", len(wins))

mur_i = -1
mur_t = time.ticks_ms()                          # すぐ最初の文を出す

def talk_broadcast(nchars):
    try:
        sock.sendto(b"TALK %d 430 150" % nchars, ("192.168.4.255", 5006))
    except OSError:
        pass

def murmur_step(now, animating_idle):
    """常設窓の文を回す。窓はキャラと重ならないので何も止めなくてよい"""
    global mur_i, mur_t
    if not wins or time.ticks_diff(now, mur_t) < 0:
        return
    mur_i = (mur_i + 1) % len(wins)
    _, nch = d.blit_file(wins[mur_i], WIN_Y0)
    if animating_idle:                           # 静かな時だけ声も出す
        talk_broadcast(nch)
    mur_t = time.ticks_add(now, 7000)            # 次の文まで

while True:
    anim = pending or "idle"
    pending = None
    frames = frames_of(pid, anim) or frames_of(pid, "idle") or frames_of(DEFAULT_PID, "idle")
    interrupted = False
    for dur, pal, grid in frames:
        draw(grid, pal)
        deadline = time.ticks_add(time.ticks_ms(), dur)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            msg, addr = poll()
            if msg:
                handle(msg, addr)
                if pending:                      # 新しい指示が来たら即切替
                    interrupted = True
                    break
            murmur_step(time.ticks_ms(), anim == "idle")
            time.sleep_ms(25)
        if interrupted:
            break
