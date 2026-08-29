# device_player.py — 精霊アニメ再生（XIAO ESP32C3 + ST7789 240x240 / MicroPython）
# 配線: SCL=GPIO8(D8) SDA=GPIO10(D10) RES=GPIO2(D0) DC=GPIO3(D1) BLK=3V3
# SPI: SoftSPI 2MHz MODE3（XIAOはHW SPIと相性が悪い・2026-07-29報告）
# データ: export_device.py が作る dev_<id>_<anim>.bin
# 描画: 前フレームとの差分セルだけ書く（SoftSPIの遅さ対策）。横に連続する同色はまとめて1回で書く。

import struct
import time

from machine import Pin, SoftSPI

SCL, SDA, RES, DC = 8, 10, 2, 3
GRID, CELL = 32, 7
OFF_X, OFF_Y = 8, 1    # 上空白なし: 底(row27)が y=197 の窓上端に接する

dc = Pin(DC, Pin.OUT)
rst = Pin(RES, Pin.OUT)
spi = SoftSPI(baudrate=2000000, polarity=1, phase=1,
              sck=Pin(SCL), mosi=Pin(SDA), miso=Pin(4))


def _cmd(c):
    dc(0); spi.write(bytes([c]))


def _dat(d):
    dc(1); spi.write(d)


def _win(x0, y0, x1, y1):
    _cmd(0x2A); _dat(struct.pack('>HH', x0, x1))
    _cmd(0x2B); _dat(struct.pack('>HH', y0, y1))
    _cmd(0x2C)


def init_lcd():
    rst(1); time.sleep_ms(120); rst(0); time.sleep_ms(120); rst(1); time.sleep_ms(250)
    _cmd(0x01); time.sleep_ms(200); _cmd(0x11); time.sleep_ms(250)
    _cmd(0x3A); _dat(b'\x55'); time.sleep_ms(50); _cmd(0x36); _dat(b'\x00')
    _cmd(0x21); _cmd(0x13); time.sleep_ms(10); _cmd(0x29); time.sleep_ms(50)


def clear(pal):
    _win(0, 0, 239, 239); dc(1)
    line = pal[0:2] * 240
    for _ in range(240):
        spi.write(line)


def _draw_run(c0, c1, r, color2b):
    """セル(c0..c1, r) を同色で一気に描く"""
    x0 = OFF_X + c0 * CELL
    x1 = OFF_X + (c1 + 1) * CELL - 1
    y0 = OFF_Y + r * CELL
    _win(x0, y0, x1, y0 + CELL - 1); dc(1)
    spi.write(color2b * ((c1 - c0 + 1) * CELL * CELL))


def draw_grid(grid, pal, prev, repaint=False):
    """差分セルだけ描く。
    prev=None: キャラ部(非背景)だけ全描画（背景は画面クリア済み前提）
    repaint=True: パレット変更時。非背景セル全部＋背景に戻ったセルを塗り直す
    （全面クリアをしない＝上から下へ黒が流れるのを防ぐ）"""
    def need(i):
        if prev is None:
            return grid[i] != 0
        if repaint:
            return grid[i] != 0 or prev[i] != grid[i]
        return prev[i] != grid[i]

    for r in range(GRID):
        base = r * GRID
        c = 0
        while c < GRID:
            i = base + c
            if not need(i):
                c += 1
                continue
            v = grid[i]                            # 描くべき同色の横連続をまとめる
            c1 = c
            while c1 + 1 < GRID and grid[base + c1 + 1] == v and need(base + c1 + 1):
                c1 += 1
            _draw_run(c, c1, r, pal[v * 2: v * 2 + 2])
            c = c1 + 1



# ---- 文字帯（画面下部の16px帯）: PC側でビットマップ化した文字を表示 ----
TEXT_Y0 = 214
TEXT_H = 24
BLK = bytes([0, 0])
WHT = bytes([255, 255])

def text_clear(bg=BLK):
    _win(0, TEXT_Y0, 239, TEXT_Y0 + TEXT_H - 1); dc(1)
    line = bg * 240
    for _ in range(TEXT_H):
        spi.write(line)

def text_blit(x, w, bits, fg=WHT, bg=BLK, stride=None):
    """bits: TEXT_H行×(stride*8)列の1bit列。先頭 w 列だけ x 位置に描く
    stride指定で「左からw列だけ見せる」＝あつ森風の1文字ずつ表示ができる"""
    _win(x, TEXT_Y0, x + w - 1, TEXT_Y0 + TEXT_H - 1); dc(1)
    stride = stride or (w + 7) // 8
    for r in range(TEXT_H):
        row = bytearray()
        for c in range(w):
            b = bits[r * stride + c // 8]
            row += fg if (b >> (7 - c % 8)) & 1 else bg
        spi.write(row)



# ---- あつ森風の会話窓（フルカラーRGB565をファイルから直接流す） ----
def blit_file(path, y0):
    """win_*.bin: [h:1B][nchars:1B][RGB565 240*h] を y0 から描く。(h, nchars) を返す"""
    with open(path, 'rb') as f:
        h = f.read(1)[0]
        n = f.read(1)[0]
        data = f.read()                          # 一括読み（41KB・C3のRAMで余裕）
    _win(0, y0, 239, y0 + h - 1); dc(1)
    mv = memoryview(data)
    for i in range(0, len(data), 8192):          # 大きなチャンク=速い
        spi.write(mv[i:i + 8192])
    return h, n

def fill_rect(y0, h, color2b):
    _win(0, y0, 239, y0 + h - 1); dc(1)
    line = color2b * 240
    for _ in range(h):
        spi.write(line)

def draw_rows(grid, pal, r0):
    """グリッドの r0 行目以降を強制再描画（会話窓を消した後の復元用）"""
    for r in range(r0, GRID):
        base = r * GRID
        c = 0
        while c < GRID:
            v = grid[base + c]
            if v == 0:
                c += 1
                continue
            c1 = c
            while c1 + 1 < GRID and grid[base + c1 + 1] == v:
                c1 += 1
            _draw_run(c, c1, r, pal[v * 2: v * 2 + 2])
            c = c1 + 1


def load(fname):
    frames = []
    with open(fname, 'rb') as f:
        n = f.read(1)[0]
        for _ in range(n):
            dur = struct.unpack('>H', f.read(2))[0]
            pal = f.read(12)
            grid = f.read(1024)
            frames.append((dur, pal, grid))
    return frames


class Player:
    def __init__(self):
        self.prev = None
        self.prev_pal = None

    def play(self, fname, loops=1):
        frames = load(fname)
        for _ in range(loops):
            for dur, pal, grid in frames:
                t0 = time.ticks_ms()
                if self.prev is None:              # 起動直後だけ全面クリア
                    clear(pal)
                    draw_grid(grid, pal, None)
                elif self.prev_pal != pal:         # 色変化はキャラ部だけ塗り直し
                    draw_grid(grid, pal, self.prev, repaint=True)
                else:                              # ふだんは差分だけ
                    draw_grid(grid, pal, self.prev)
                self.prev, self.prev_pal = grid, pal
                spent = time.ticks_diff(time.ticks_ms(), t0)
                if spent < dur:
                    time.sleep_ms(dur - spent)


def demo(pid='kuwahara', cycles=0):
    """cycles=0 で無限ループ。'ANIM <name>' を印字する（PC側が音の同期に使う）"""
    init_lcd()
    p = Player()
    print('ANIM hatch')
    p.play('dev_%s_hatch.bin' % pid)               # 誕生（1回だけ）
    n = 0
    while cycles == 0 or n < cycles:
        n += 1
        for name, loops in [('idle', 3), ('notice', 1), ('happy', 1),
                            ('idle', 2), ('sad', 1), ('sleep', 2)]:
            print('ANIM ' + name)
            p.play('dev_%s_%s.bin' % (pid, name), loops=loops)


if __name__ == '__main__':
    demo()
