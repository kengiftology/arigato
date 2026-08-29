# lcd_serve.py — XIAO常駐: PCからのシリアル行コマンドで顔と字幕を出す（MicroPython）
#   PLAY <anim> [loops]     … dev_<pid>_<anim>.bin を再生（ブロッキング）
#   TEXT <hex>              … 240x16 1bitビットマップ(hex)を下帯に描く
#   TCLR                    … 下帯を消す
#   PID <id>                … 対象キャラ切替
#   PING                    … PONG を返す
# 使い方: PCから mpremote run lcd_serve.py（プロセスを繋ぎっぱなしにする）
import sys, select
import device_player as d

d.init_lcd()
p = d.Player()
pid = 'kuwahara'
print('LCD READY')
poll = select.poll()
poll.register(sys.stdin, select.POLLIN)
buf = ''
while True:
    if poll.poll(50):
        ch = sys.stdin.read(1)
        if ch == '\n':
            line = buf.strip(); buf = ''
            if not line:
                continue
            parts = line.split(' ')
            cmd = parts[0]
            try:
                if cmd == 'PING':
                    print('PONG')
                elif cmd == 'PID':
                    pid = parts[1]; print('OK')
                elif cmd == 'PLAY':
                    loops = int(parts[2]) if len(parts) > 2 else 1
                    p.play('dev_%s_%s.bin' % (pid, parts[1]), loops=loops)
                    print('DONE ' + parts[1])
                elif cmd == 'TEXT':
                    d.text_blit(0, 240, bytes.fromhex(parts[1]))
                    print('OK')
                elif cmd == 'TCLR':
                    d.text_clear(); print('OK')
                else:
                    print('ERR unknown ' + cmd)
            except Exception as e:
                print('ERR ' + repr(e))
        elif ch != '\r':
            buf += ch
