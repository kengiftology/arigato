# m_server.py - WROVER(eye): compute 散らかり度M and serve it over WiFi (/m).
# 背骨の後半。起動時に baseline（きれいな状態）を登録し、顔(XIAO)が /m を取りに来たら
# 現フレームのM（と放置度N）を計算して "M N" のテキストで返す。プル型。
#
# M計算（2026-07-28改訂）＝「元の綺麗な状態とどれだけ違うか」：
#   16x16=256ブロックの平均輝度・全体平均を引く正規化（全体変化=日照を除去）に加え、
#   ①変化を0/1でなく0..1のなだらかなスコアに（T0..T1）→ 閾値ぎわでMが跳ねない
#   ②連続K_PERSIST回変化が続いたマスだけ算入 → 影・人・一時変化を無視、置きっぱなしだけ拾う
#   ③Mが低い間はbaselineをゆっくり追従（BASE_ALPHA）→ 日照ドリフトで誤検知しない
#
# HW: FREENOVE WROVER (OV3660)。 Run: mpremote connect COM19 run esp32cam/m_server.py
#   GET /m        -> "M N"（0..1 のテキスト）
#   GET /baseline -> 現在の景色を新しい基準として登録し直す（設置時・きれいにした後）
#   GET /health   -> ok
#   GET /pose?a1=90&a2=100&a3=80 -> 三軸サーボをその角度へ（治具制限でクランプ）。省略軸は動かさない
#   GET /pose     -> 今の角度 "a1 a2 a3"
#   GET /servo/off-> サーボ全軸脱力（※この個体は空回りする）
#   GET /ui       -> スマホ用の首コントローラ画面（スライダーで3軸を遠隔操作）
#   POST /update  -> OTA更新。新しいmain.pyを流し込む（文法チェック合格時のみ採用→自動再起動）
#                    curl.exe -s -X POST --data-binary "@esp32cam/m_server.py" http://192.168.0.202/update
# 首(サーボ)はカメラと同じLEDCを使うため、カメラ初期化の後にattachし、撮影のたびにdutyを検証して自動復旧する。

import json, time, socket, gc, struct
import network, machine
import servo3

with open("config.json") as f:
    CFG = json.load(f)
WIFI = CFG["wifi"]

# Mの計測画像は4:3（センサーの全画角）。1:1(240x240)はその中央を切り取った狭い絵になるため、
# 部屋をなるべく広く見張る目的では4:3を使う。320/16=20、240/16=15 でブロックに割り切れる。
W = 320               # 計測画像の幅
MH = 240              # 計測画像の高さ
B = 16                # 16x16 ブロック
NB = B * B            # 256

# --- M（散らかり度）の計算パラメータ ---
# 各マスの変化を 0/1 でなく 0..1 の「なだらかなスコア」にする（閾値ぎわで跳ねない）
T0 = 6.0              # この輝度差から「変わり始め」（スコア>0）
T1 = 20.0             # この輝度差で「完全に変わった」（スコア=1）
# 輪郭（隣接画素の差）でも比べる。物が増える＝輪郭が増える／日照は面で明るさが変わるだけ、
# なので「日照 vs 物」を原理的に切り分けられる。実測（2026-08-08）: 同じ景色2枚の輪郭差は
# 平均0.17・最大1.11（＝ノイズ）、輪郭エネルギーの中央値は11.5。それを根拠に閾値を決めた。
E0 = 3.0              # この輪郭差から効き始める（ノイズ最大の約3倍）
E1 = 12.0             # この輪郭差で満点（典型的な輪郭エネルギーと同程度＝構造が変わった）
EDGE_W = 0.5          # 輪郭スコアの重み（0=輝度だけ / 1=輪郭だけ）

S_ON = 0.4            # このスコア以上を「変化中」とみなし、持続カウントを進める
K_PERSIST = 2         # 連続K回（/mの間隔×K ≒ 4分）変化が続いたマスだけMに算入（影・一時変化を無視）
BASE_ALPHA = 0.05     # きれいな間、基準を今の景色にゆっくり寄せる割合（日照ドリフト対策）
M_LOW = 0.08          # このM未満なら「きれい」とみなし基準を追従させる

# 5方向スキャンを自分で回す間隔（秒）。0で停止。/scan/every?s=600 で運転中も変えられる。
# PCなしで自走させるため、この周期でWROVER自身が首を振って scan0..4.jpg を撮り直す。
SCAN_EVERY_S = 0      # 既定オフ。自走させるなら /scan/every?s=300

# クラウドの脳（地霊）へ写真を送る間隔（秒）。0で停止。
# 送る前にクラウドへ在室を確認し、無人のときだけ撮って送る（人を写さない）。
SPIRIT_PUSH_S = 120
SPIRIT_SERVER = CFG.get("server", "")
SPIRIT_KEY = CFG.get("upload_key", "")

M_HI = 0.30           # このM以上が続くと放置度Nが育つ
N_FULL_S = 600        # Nが0->1になるまでの秒数。Mが下がると即リセット


def wifi_connect():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    try:
        wlan.config(pm=network.WLAN.PM_NONE)
    except Exception:
        pass
    if not wlan.isconnected():
        for ssid, pw in WIFI:
            try:
                wlan.connect(ssid, pw)
            except OSError:
                continue
            for _ in range(20):
                if wlan.isconnected():
                    break
                time.sleep(1)
            if wlan.isconnected():
                break
    return wlan


_T0 = time.ticks_ms()
def el():
    return time.ticks_diff(time.ticks_ms(), _T0) // 1000


# WiFiに繋がるまで諦めない（スタンドアロン運用：電源投入だけで復帰できるように）
wlan = wifi_connect()
while not wlan.isconnected():
    print("WIFI retry in 5s... (check config.json / signal)")
    time.sleep(5)
    wlan = wifi_connect()
ip = wlan.ifconfig()[0]
print("=========================================")
print("  WROVER(eye) IP =", ip)
print("  put this into face_view.py WROVER_IP")
print("=========================================")

servo3.clear_pins()                 # 残りPWMの掃除（カメラより先）
from camera import Camera, FrameSize, PixelFormat
cam = Camera(pixel_format=PixelFormat.GRAYSCALE, frame_size=FrameSize.QVGA)
cam.set_vflip(True)
servo3.attach_all(center=True)      # カメラの後でattach（LEDC競合回避）→ 全軸90°保持
print("servos attached & holding:", servo3.angles(), "duty check:", servo3.verify())


def block_stats(gray):
    """白黒画像 -> 256ブロックごとの (平均輝度, 輪郭エネルギー)。
    輪郭＝隣の画素との差の平均。物が増えると輪郭が増えるので、面で明るさが変わるだけの
    日照と区別できる。同じ1パス・同じ2px間引きの中で両方数えるので計算量はほぼ据え置き。
    ※MicroPythonでフルSobelを回すと数十秒かかるため、この簡易勾配で代用している。"""
    h = len(gray) // W
    bsx = W // B
    bsy = h // B
    means = [0.0] * NB
    edges = [0.0] * NB
    for by in range(B):
        y0 = by * bsy
        for bx in range(B):
            x0 = bx * bsx
            s = 0; e = 0; cnt = 0
            yy = y0
            while yy < y0 + bsy:
                row = yy * W + x0
                xx = 0
                while xx < bsx - 2:
                    p = gray[row + xx]
                    s += p; cnt += 1
                    e += abs(gray[row + xx + 2] - p)          # 横方向の輪郭
                    if yy + 2 < y0 + bsy:
                        e += abs(gray[row + W * 2 + xx] - p)  # 縦方向の輪郭
                    xx += 2
                yy += 2
            i = by * B + bx
            means[i] = s / cnt
            edges[i] = e / cnt
    return means, edges


def capture_means(save_raw=None, warm=2):
    # 捨て撮りで露出・ゲインを落ち着かせる。基準を撮るときは長めに温める：
    # 起動直後の2枚だけだと、まだ眠い（低コントラストな）絵が基準になり、
    # 以後ずっと「輪郭が増えた＝散らかった」と誤読され続ける（実測で27%ズレた）。
    for _ in range(warm):
        cam.capture(); time.sleep_ms(80)
    g = cam.capture()
    if g:
        # capture()はフレームバッファ参照で、画素が符号付き(-128..127)として読まれる。
        # そのままだと128以上の画素が負に化け、中間グレー付近で「隣と250違う」幻の輪郭が出る
        # （実測: 輪郭エネルギーが13.5→34.7に水増しされていた）。bytes化して0..255に固定する。
        g = bytes(g)
    fixed = servo3.rehold()       # 撮影でサーボdutyが飛んでいたら復旧（LEDC競合対策）
    if fixed:
        print("[+%ds] SERVO reheld after capture: %s" % (el(), fixed))
    if not g:
        return None
    if save_raw:                  # 基準の見た目も残す（/baseline.bmp で確認できるように）
        try:
            with open(save_raw, "wb") as f:
                f.write(g)
        except Exception as e:
            print("save baseline raw failed:", e)
    return block_stats(g)


# カラー撮影のサイズ候補。4:3＝センサーの全画角（部屋を一番広く写せる）。
# 3:2や1:1は上下/左右が切れるので、広さ優先なら4:3を使う。
_CAND = [
    ("svga", "SVGA",     800, 600),   # 4:3
    ("vga",  "VGA",      640, 480),   # 4:3
    ("cif",  "CIF",      400, 296),   # ほぼ4:3
    ("qvga", "QVGA",     320, 240),   # 4:3
    ("hvga", "HVGA",     480, 320),   # 3:2（上下が切れる）
    ("240",  "R240X240", 240, 240),   # 1:1（左右が切れる）
]
SIZES = {}
for _k, _attr, _w, _h in _CAND:
    _fs = getattr(FrameSize, _attr, None)
    if _fs is not None:
        SIZES[_k] = (_fs, _w, _h)
# 大きい4:3から順に試し、メモリ不足なら自動的に小さくする（最後まで4:3を優先）
SIZE_FALLBACK = [k for k in ("svga", "vga", "cif", "qvga", "hvga", "240") if k in SIZES]


def capture_color_jpeg(size="240"):
    """一時的にカラー(RGB565)へ切り替えてJPEGを作り、必ず白黒カメラに戻す。
    Mの計算には白黒しか要らないが、人が見る画像はカラーの方が分かりやすいので確認用。
    OV3660はJPEG直撮りが失敗するため、RGB565で撮って jpeg.Encoder で変換する。
    大きいサイズはメモリ不足で失敗しうるので、順に小さくして再挑戦する。"""
    global cam
    import jpeg
    if size not in SIZES:
        size = "240"
    order = SIZE_FALLBACK[SIZE_FALLBACK.index(size):]
    img = None
    try:
        cam.deinit()
    except Exception:
        pass
    for name in order:
        fs, w, h = SIZES[name]
        raw = None
        try:
            gc.collect()
            c = Camera(pixel_format=PixelFormat.RGB565, frame_size=fs)
            try:
                c.set_vflip(True)
                for _ in range(3):
                    c.capture(); time.sleep_ms(80)
                r = c.capture()
                if r:
                    raw = bytes(r)
            finally:
                c.deinit()
            if raw:
                img = jpeg.Encoder(width=w, height=h, pixel_format="RGB565_BE", quality=80).encode(raw)
                del raw
                gc.collect()
                print("[+%ds] color capture %s %dB" % (el(), name, len(img)))
                break
        except Exception as e:
            print("[+%ds] color capture %s failed:" % (el(), name), e)
            gc.collect()
    try:                       # 白黒へ復帰（M計算に必須。ここは何があっても通す）
        cam = Camera(pixel_format=PixelFormat.GRAYSCALE, frame_size=FrameSize.QVGA)
        cam.set_vflip(True)
    except Exception as e:
        print("[+%ds] RESTORE GRAY CAM FAILED:" % el(), e)
    servo3.rehold()
    gc.collect()
    return img


# 部屋を見渡す5方向（軸2は90固定）。首を振って1枚ずつ撮り、scanN.jpg に残す。
SCAN_POSES = [
    (0,   90, 45),
    (90,  90, 45),
    (90,  90, 135),
    (180, 90, 45),
    (90,  90, 90),
]


_scan_t = None          # 最後にスキャンした時刻（起動からの秒）


def do_sweep():
    """5方向へ首を振って撮り、scanN.jpg に保存する。最後は必ず見張りの向き(90/90/90)へ戻す。
    人がいる間は呼ばない（人を写さないため）。撮影中はサーバーが一時的に応答しない。"""
    global _scan_t
    print("[+%ds] SWEEP start" % el())
    ok = 0
    try:
        for i, (a1, a2, a3) in enumerate(SCAN_POSES):
            servo3.pose(a1, a2, a3)
            time.sleep_ms(700)
            j = capture_color_jpeg("qvga")   # 5連続なので軽いサイズで（メモリ・電流の余裕を残す）
            if j:
                try:
                    with open("scan%d.jpg" % i, "wb") as f:
                        f.write(j)
                    ok += 1
                except Exception as e:
                    print("  scan%d save failed:" % i, e)
            del j
            gc.collect()
    finally:
        servo3.pose(90, 90, 90)      # 見張りの向きへ復帰
    _scan_t = el()
    print("[+%ds] SWEEP done %d/5" % (el(), ok))
    # 撮影後に白黒カメラが生きているか確認するだけ（ここで再起動すると再起動ループになりうる）
    if capture_means() is None:
        print("WARN: gray camera not returning frames after sweep")


def send_file(cl, name, ctype=b"image/jpeg"):
    """保存済みファイルをそのまま返す（Content-Length付き）。"""
    import os
    n = os.stat(name)[6]
    cl.send(b"HTTP/1.0 200 OK\r\nContent-Type: " + ctype + b"\r\nContent-Length: %d\r\n\r\n" % n)
    with open(name, "rb") as f:
        while True:
            b = f.read(1024)
            if not b:
                break
            send_all(cl, b)


def bmp_header(w, h):
    """8bit白黒BMPのヘッダ＋パレット。ブラウザがそのまま表示できる形式。"""
    off = 14 + 40 + 1024
    hdr = b"BM" + struct.pack("<IHHI", off + w * h, 0, 0, off)
    hdr += struct.pack("<IiiHHIIiiII", 40, w, h, 1, 8, 0, w * h, 2835, 2835, 256, 0)
    pal = bytearray()
    for i in range(256):
        pal += bytes((i, i, i, 0))
    return hdr + bytes(pal)


def send_bmp_from_bytes(cl, gray, w=W, h=None):
    h = h or (len(gray) // w)
    hdr = bmp_header(w, h)
    cl.send(b"HTTP/1.0 200 OK\r\nContent-Type: image/bmp\r\nContent-Length: %d\r\n\r\n" % (len(hdr) + w * h))
    send_all(cl, hdr)
    for y in range(h - 1, -1, -1):        # BMPは下の行から並べる
        send_all(cl, gray[y * w:(y + 1) * w])


def send_bmp_from_file(cl, path, w=W, h=None):
    import os
    size = os.stat(path)[6]
    h = h or (size // w)
    hdr = bmp_header(w, h)
    cl.send(b"HTTP/1.0 200 OK\r\nContent-Type: image/bmp\r\nContent-Length: %d\r\n\r\n" % (len(hdr) + w * h))
    send_all(cl, hdr)
    with open(path, "rb") as f:
        for y in range(h - 1, -1, -1):
            f.seek(y * w)
            send_all(cl, f.read(w))


baseline = None           # 基準の平均輝度（256マス）
baseline_e = None         # 基準の輪郭エネルギー（256マス）
_pcnt = [0] * NB          # 各マスの「変化が連続何回続いたか」


_base_t = None      # 基準を登録した時刻（起動からの秒）


def register_baseline(color=True):
    """基準を登録する。カラー版を先に撮ってから白黒の基準を撮る＝通常運転と同じ
    カメラ状態（再初期化直後・十分温まった状態）で基準を取るため。順番が逆だと
    基準だけコントラストが違い、静止した部屋でもMが上がってしまう。"""
    global baseline, baseline_e, _base_t
    if color:                                # 人が見て確認できるカラー版（先に撮る）
        try:
            j = capture_color_jpeg("vga")
            if j:
                with open("baseline.jpg", "wb") as f:
                    f.write(j)
        except Exception as e:
            print("baseline color save failed:", e)
    st = capture_means(save_raw="baseline.raw", warm=8)
    baseline, baseline_e = st if st else (None, None)
    _base_t = el()
    for i in range(NB):
        _pcnt[i] = 0
    return baseline is not None


def calc_M(cur, cur_e):
    """M = 「元の綺麗な状態とどれだけ違うか」(0..1)。
    ・輝度: 全体平均を引いた差 → 0..1のなだらかなスコア（T0..T1）。日照の全体変化を除去
    ・輪郭: 全体比で正規化した差 → 0..1のスコア（E0..E1）。物が増えると輪郭が増える
      → 影や日照は「面の明るさ」しか動かさないので、輪郭を併用すると物と切り分けられる
    ・両者を EDGE_W で混ぜ、連続K_PERSIST回続いたマスだけ算入（一時変化を無視）
    ・Mが低い(きれい)間は基準をゆっくり追従（日照ドリフト対策）"""
    global baseline, baseline_e
    mb = sum(baseline) / NB
    mc = sum(cur) / NB
    eb = sum(baseline_e) / NB
    ec = sum(cur_e) / NB
    escale = (eb / ec) if ec > 0.01 else 1.0   # 全体のコントラスト変化を打ち消す
    tot = 0.0
    for i in range(NB):
        d = abs((cur[i] - mc) - (baseline[i] - mb))
        sb = (d - T0) / (T1 - T0)
        if sb < 0.0:
            sb = 0.0
        elif sb > 1.0:
            sb = 1.0
        de = abs(cur_e[i] * escale - baseline_e[i])
        se = (de - E0) / (E1 - E0)
        if se < 0.0:
            se = 0.0
        elif se > 1.0:
            se = 1.0
        s = sb * (1.0 - EDGE_W) + se * EDGE_W
        if s >= S_ON:
            _pcnt[i] += 1
        else:
            _pcnt[i] = 0
        if _pcnt[i] >= K_PERSIST:
            tot += s
    M = tot / NB
    if M < M_LOW:          # きれい → 基準を今の景色にゆっくり寄せる
        a = BASE_ALPHA
        for i in range(NB):
            baseline[i] = baseline[i] * (1.0 - a) + cur[i] * a
            baseline_e[i] = baseline_e[i] * (1.0 - a) + cur_e[i] * a
    return M


_t_high = None
def calc_N(M):
    """放置度N = Mが高いまま経過した時間（0..1）。Mが下がると即リセット。"""
    global _t_high
    now = time.ticks_ms()
    if M >= M_HI:
        if _t_high is None:
            _t_high = now
        dt = time.ticks_diff(now, _t_high) / 1000.0
        return min(1.0, dt / N_FULL_S)
    _t_high = None
    return 0.0


# ---- 無人検知（AtomS3のハートビートで在室/不在を判定）----
# ・ハートビートが一度も来ていない間は「不在扱い」＝常に撮る（AtomS3なしでも背骨が動く）。
# ・来ていれば：15秒受信が無ければ occupied（安全側＝撮らない）、
#   直近stateが empty のときだけ撮ってMを更新。在室中は _last_M を凍結して返す。
HB_STALE_MS = 15000
_last_state = None       # "empty" / "occupied" / None(未接続)
_last_hb = None          # 最終ハートビート時刻(ticks_ms)
_last_M = 0.0            # 在室中はこの値を凍結して返す

def presence_empty():
    if _last_hb is None:
        return True                                   # AtomS3未接続 → 従来通り常に撮る
    if time.ticks_diff(time.ticks_ms(), _last_hb) > HB_STALE_MS:
        return False                                  # ハートビート途絶 → 在室扱い(安全側)
    return _last_state == "empty"


def spirit_push():
    """クラウドの脳へいまの景色を1枚送る。無人のときだけ（人を写さない）。
    失敗しても次の周期に再挑戦するだけで、本体の他の仕事には影響させない。"""
    if not SPIRIT_SERVER:
        return
    import requests
    try:                                  # クラウド側の在室状態を確認（C3が報告している）
        r = requests.get(SPIRIT_SERVER + "/spirit/presence", timeout=10)
        occ = r.text.strip() != "empty"
        r.close()
        if occ:
            print("[+%ds] SPIRIT skip (occupied)" % el())
            return
    except Exception as e:
        print("[+%ds] SPIRIT presence err:" % el(), e)
        return
    j = capture_color_jpeg("vga")
    if not j:
        print("[+%ds] SPIRIT capture failed" % el())
        return
    try:
        headers = {"Content-Type": "image/jpeg"}
        if SPIRIT_KEY:
            headers["X-Upload-Key"] = SPIRIT_KEY
        r = requests.post(SPIRIT_SERVER + "/spirit/frame", data=j, headers=headers)
        print("[+%ds] SPIRIT push %d %s" % (el(), r.status_code, r.text[:80]))
        r.close()
    except Exception as e:
        print("[+%ds] SPIRIT push err:" % el(), e)
    finally:
        del j
        gc.collect()


if register_baseline():
    print("baseline registered. M starts near 0 on the clean scene.")
else:
    print("WARN: baseline capture failed (camera?).")

# スマホ用の首コントローラ画面（/ui）。スライダーを離した時だけ /pose を送る（ESPを洪水にしない）
UI_HTML = u"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>首コントローラ</title><style>
body{font-family:sans-serif;margin:16px;background:#ffffff;color:#222}
.ax{margin:22px 0}input[type=range]{width:100%;height:34px}
button{padding:12px 20px;margin:6px 4px;font-size:16px;border-radius:8px;border:1px solid #999}
.v{font-size:28px;font-weight:bold;color:#06c}#st{margin-left:10px;color:#080}
.sh{display:inline-block;margin:6px;font-size:13px;vertical-align:top}
img{background:#eee;min-height:60px}
</style></head><body>
<h2>首コントローラ（三軸）</h2>
<div class="ax">軸1 パン <span class="v" id="v1">90</span>°
<input type="range" id="s1" min="0" max="180" value="90"></div>
<div class="ax">軸2 <span class="v" id="v2">90</span>°
<input type="range" id="s2" min="45" max="135" value="90"></div>
<div class="ax">軸3 <span class="v" id="v3">90</span>°
<input type="range" id="s3" min="45" max="135" value="90"></div>
<button onclick="send('a1=90&a2=90&a3=90')">全部90°へ</button><span id="st"></span>

<h2>基準（きれいな状態）と今</h2>
<div id="mn">読み込み中…</div>
<p><button onclick="rebase()">この景色を新しい基準にする</button>
<button onclick="reload()">写真を更新</button></p>
<div>
<div style="display:inline-block;margin:4px">基準<br><img id="imgb" width="220" alt="基準"></div>
<div style="display:inline-block;margin:4px">今<br><img id="imgn" width="220" alt="今"></div>
</div>

<h2>部屋の5方向</h2>
<p><button onclick="shot(0)">5方向を撮り直す</button><span id="sw"></span></p>
<div>
<div class="sh"><b>1</b> パン0/チルト45<br><img id="p0" width="200" alt="1"></div>
<div class="sh"><b>2</b> パン90/チルト45<br><img id="p1" width="200" alt="2"></div>
<div class="sh"><b>3</b> パン90/チルト135<br><img id="p2" width="200" alt="3"></div>
<div class="sh"><b>4</b> パン180/チルト45<br><img id="p3" width="200" alt="4"></div>
<div class="sh"><b>5</b> 全部90（見張りの向き）<br><img id="p4" width="200" alt="5"></div>
</div>
<script>
function refl(t){var a=t.trim().split(" ");
 s1.value=a[0];s2.value=a[1];s3.value=a[2];
 v1.textContent=a[0];v2.textContent=a[1];v3.textContent=a[2];}
function send(q){st.textContent="...";
 fetch("/pose?"+q).then(function(r){return r.text()})
 .then(function(t){refl(t);st.textContent="OK";})
 .catch(function(){st.textContent="接続エラー";});}
function reload(){var t=Date.now();
 imgb.src="/baseline.jpg?t="+t;
 imgn.onload=function(){cache(0)}; imgn.onerror=function(){cache(0)};
 imgb.onload=function(){imgn.src="/view.jpg?force=1&t="+t;};
 imgb.onerror=function(){imgn.src="/view.jpg?force=1&t="+t;};
 fetch("/m").then(function(r){return r.text()}).then(function(x){
  var a=x.trim().split(" ");
  mn.textContent="散らかり度 M="+a[0]+"  放置度 N="+a[1]+(a[2]=="1"?"  (無人)":"  (在室・凍結中)");});}
function rebase(){if(!confirm("今の景色を『きれいな基準』にします。よろしいですか？"))return;
 mn.textContent="基準を撮り直しています…";
 fetch("/baseline").then(function(){setTimeout(reload,1200);});}
function shot(i){if(i>=5){sw.textContent=" 完了（中央へ戻しました）";return;}
 sw.textContent=" 撮影中 "+(i+1)+"/5 …首が動きます";
 var im=document.getElementById("p"+i);
 im.onload=function(){shot(i+1)}; im.onerror=function(){shot(i+1)};
 im.src="/shot.jpg?p="+i+"&force=1&t="+Date.now();}
function cache(i){if(i>=5)return;var im=document.getElementById("p"+i);
 im.onload=function(){cache(i+1)}; im.onerror=function(){cache(i+1)};
 im.src="/scan.jpg?p="+i+"&t="+Date.now();}
s1.onchange=function(){send("a1="+s1.value)};
s2.onchange=function(){send("a2="+s2.value)};
s3.onchange=function(){send("a3="+s3.value)};
s1.oninput=function(){v1.textContent=s1.value};
s2.oninput=function(){v2.textContent=s2.value};
s3.oninput=function(){v3.textContent=s3.value};
fetch("/pose").then(function(r){return r.text()}).then(refl);
reload();
</script></body></html>""".encode()


def send_all(cl, data):
    """send()は「送れた分だけ」返す。全部吐き切るまで回す（大きい応答の途中切れ対策）。"""
    mv = memoryview(data)
    while len(mv):
        n = cl.send(mv)
        if not n:
            break
        mv = mv[n:]


def recv_request(cl):
    """リクエストをヘッダ終端(\\r\\n\\r\\n)まで読み切る。
    未読データを残したままcloseするとRSTになり、送信済みの応答まで相手側で破棄される。"""
    req = b""
    while b"\r\n\r\n" not in req and len(req) < 4096:
        chunk = cl.recv(512)
        if not chunk:
            break
        req += chunk
    return req

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", 80))
srv.listen(1)
srv.settimeout(5)
_next_scan = time.ticks_add(time.ticks_ms(), SCAN_EVERY_S * 1000 if SCAN_EVERY_S > 0 else 60000)
_next_spirit = time.ticks_add(time.ticks_ms(), 20000)   # 起動20秒後に初回送信
print("SERVER up on :80  /m /baseline /health /ui   scan every %ds  spirit every %ds" % (SCAN_EVERY_S, SPIRIT_PUSH_S))

try:
    while True:
        if not wlan.isconnected():
            print("[+%ds] wifi dropped, reconnecting" % el())
            wifi_connect()
        # --- 自走スキャン: 一定時間ごとに自分で5方向を撮る（PC不要）---
        if SCAN_EVERY_S > 0 and time.ticks_diff(time.ticks_ms(), _next_scan) >= 0:
            _next_scan = time.ticks_add(time.ticks_ms(), SCAN_EVERY_S * 1000)
            if presence_empty():        # 人がいる間は撮らない
                try:
                    do_sweep()
                except Exception as e:
                    print("[+%ds] SWEEP ERR:" % el(), e)
                    try:
                        servo3.pose(90, 90, 90)
                    except Exception:
                        pass
            else:
                print("[+%ds] SWEEP skipped (occupied)" % el())

        # --- クラウドの脳へ写真を送る（無人時のみ・SPIRIT_PUSH_S間隔）---
        if SPIRIT_PUSH_S > 0 and time.ticks_diff(time.ticks_ms(), _next_spirit) >= 0:
            _next_spirit = time.ticks_add(time.ticks_ms(), SPIRIT_PUSH_S * 1000)
            try:
                spirit_push()
            except Exception as e:
                print("[+%ds] SPIRIT ERR:" % el(), e)

        try:
            cl, addr = srv.accept()
        except OSError:
            continue
        try:
            cl.settimeout(5)
            req = recv_request(cl)      # ヘッダを最後まで読み切る（RST防止）
            # 振り分けは「リクエスト行のパス」だけで判定する。
            # リクエスト全体で判定すると、ブラウザが送る Referer: .../ui などに誤爆して
            # 画像を頼まれたのにHTMLを返す（=画像が表示されない）事故が起きる。
            _l0 = req.split(b"\r\n", 1)[0]
            _pp = _l0.split(b" ")
            path = _pp[1] if len(_pp) > 1 else b"/"
            if req.startswith(b"POST") and b"/update" in path:
                # OTA更新: POST /update に新しいmain.pyを流し込む（USB不要の書き込み口）
                # ?file=servo3.py を付ければ他のモジュールも更新できる（.pyのみ・パス不可）
                head, _, body = req.partition(b"\r\n\r\n")
                tgt = "main.py"
                fline = head.split(b"\r\n", 1)[0]
                if b"file=" in fline:
                    cand = fline.split(b"file=", 1)[1].split(b" ", 1)[0].split(b"&", 1)[0].decode()
                    if cand.endswith(".py") and "/" not in cand:
                        tgt = cand
                ln = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        ln = int(line.split(b":", 1)[1])
                while len(body) < ln:
                    chunk = cl.recv(1024)
                    if not chunk:
                        break
                    body += chunk
                if ln > 1000 and len(body) == ln:
                    ok = False
                    try:
                        compile(body, tgt, "exec")   # 文法チェックしてから採用
                        ok = True
                    except SyntaxError as se:
                        print("UPDATE rejected (syntax):", se)
                    if ok:
                        with open("_new.py", "wb") as f:
                            f.write(body)
                        import os
                        os.rename("_new.py", tgt)
                        cl.send(b"HTTP/1.0 200 OK\r\n\r\nupdated %s %d bytes, rebooting\n" % (tgt.encode(), ln))
                        print("[+%ds] OTA UPDATE %s %dB -> reboot" % (el(), tgt, ln))
                        time.sleep_ms(300)
                        cl.close()
                        machine.reset()
                    else:
                        cl.send(b"HTTP/1.0 400 Bad Request\r\n\r\nsyntax error, kept old main.py\n")
                else:
                    cl.send(b"HTTP/1.0 400 Bad Request\r\n\r\nbad length (%d/%d)\n" % (len(body), ln))
            elif path == b"/baseline" or path.startswith(b"/baseline?"):
                # 末尾を厳密に見る（"/baseline.bmp" に誤爆して基準を撮り直さないため）
                ok = register_baseline()
                cl.send(b"HTTP/1.0 200 OK\r\n\r\n%s\n" % (b"baseline set" if ok else b"failed"))
                print("[+%ds] baseline re-registered: %s" % (el(), ok))
            elif path.startswith(b"/presence"):
                line = req.split(b"\r\n", 1)[0]        # 例: GET /presence?state=empty HTTP/1.0
                st = "empty" if b"state=empty" in line else "occupied"
                if st != _last_state:                  # 変化した時だけ出す（洪水にしない）
                    print("[+%ds] PRESENCE -> %s" % (el(), "EMPTY (will shoot)" if st == "empty" else "occupied (no shoot)"))
                _last_state = st
                _last_hb = time.ticks_ms()
                cl.send(b"HTTP/1.0 200 OK\r\n\r\nok\n")
            elif path.startswith(b"/pose"):
                # 例: GET /pose?a1=90&a2=100&a3=80  (省略軸は動かさない) / GET /pose は現在角度
                line = req.split(b"\r\n", 1)[0]
                args = {}
                if b"?" in line:
                    q = line.split(b"?", 1)[1].split(b" ", 1)[0]
                    for kv in q.split(b"&"):
                        if b"=" in kv:
                            k, v = kv.split(b"=", 1)
                            try:
                                args[k.decode()] = int(v)
                            except ValueError:
                                pass
                if args:
                    set_ = servo3.pose(args.get("a1"), args.get("a2"), args.get("a3"))
                    print("[+%ds] POSE -> %s (%s)" % (el(), set_, addr[0]))
                a = servo3.angles()
                body = b"%d %d %d\n" % (a.get("a1", -1), a.get("a2", -1), a.get("a3", -1))
                cl.send(b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\n" + body)
            elif path.startswith(b"/servo/off"):
                servo3.off()
                cl.send(b"HTTP/1.0 200 OK\r\n\r\noff\n")
                print("[+%ds] SERVO off (%s)" % (el(), addr[0]))
            elif path.startswith(b"/ui"):
                cl.send(b"HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %d\r\n\r\n" % len(UI_HTML))
                send_all(cl, UI_HTML)
                print("[+%ds] UI served %dB (%s)" % (el(), len(UI_HTML), addr[0]))
            elif path.startswith(b"/view.jpg"):
                # 今の景色をカラーJPEGで（軽いのでブラウザ表示向き）。?big=1 で800x600
                if presence_empty() or b"force=1" in path:
                    sz = "qvga"        # 既定も4:3（広い画角）
                    line0 = req.split(b"\r\n", 1)[0]
                    if b"size=" in line0:
                        sz = line0.split(b"size=", 1)[1].split(b" ", 1)[0].split(b"&", 1)[0].decode()
                    elif b"big=1" in line0:
                        sz = "vga"
                    j = capture_color_jpeg(sz)
                    if j:
                        cl.send(b"HTTP/1.0 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(j))
                        send_all(cl, j)
                        print("[+%ds] VIEW.JPG %dB (%s)" % (el(), len(j), addr[0]))
                    else:
                        cl.send(b"HTTP/1.0 500 Internal Server Error\r\n\r\ncapture failed\n")
                else:
                    cl.send(b"HTTP/1.0 403 Forbidden\r\n\r\noccupied\n")
            elif path.startswith(b"/baseline.jpg"):
                try:
                    send_file(cl, "baseline.jpg")
                except Exception as e:
                    cl.send(b"HTTP/1.0 404 Not Found\r\n\r\nno baseline.jpg (%s)\n" % str(e).encode())
            elif path.startswith(b"/shot.jpg"):
                # 指定の向きへ首を振って1枚撮る（撮った絵は scanN.jpg に保存）。最後の1枚で中央へ戻す
                p = 0
                if b"p=" in path:
                    try:
                        p = int(path.split(b"p=", 1)[1].split(b"&", 1)[0])
                    except ValueError:
                        p = 0
                p = max(0, min(len(SCAN_POSES) - 1, p))
                if presence_empty() or b"force=1" in path:
                    a1, a2, a3 = SCAN_POSES[p]
                    servo3.pose(a1, a2, a3)
                    time.sleep_ms(700)
                    j = capture_color_jpeg("vga")
                    if p == len(SCAN_POSES) - 1:
                        servo3.pose(90, 90, 90)      # 見張りの基準向きへ必ず戻す
                    if j:
                        try:
                            with open("scan%d.jpg" % p, "wb") as f:
                                f.write(j)
                        except Exception as e:
                            print("scan save failed:", e)
                        cl.send(b"HTTP/1.0 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(j))
                        send_all(cl, j)
                        print("[+%ds] SHOT p=%d %dB (%s)" % (el(), p, len(j), addr[0]))
                    else:
                        cl.send(b"HTTP/1.0 500 Internal Server Error\r\n\r\ncapture failed\n")
                else:
                    cl.send(b"HTTP/1.0 403 Forbidden\r\n\r\noccupied\n")
            elif path.startswith(b"/scan/every"):
                # 例: /scan/every?s=600 （0で自走停止）。引数なしなら現状を返す
                if b"s=" in path:
                    try:
                        SCAN_EVERY_S = int(path.split(b"s=", 1)[1].split(b"&", 1)[0])
                        _next_scan = time.ticks_add(time.ticks_ms(),
                                                    SCAN_EVERY_S * 1000 if SCAN_EVERY_S > 0 else 60000)
                        print("[+%ds] scan interval -> %ds" % (el(), SCAN_EVERY_S))
                    except ValueError:
                        pass
                last = -1 if _scan_t is None else (el() - _scan_t)
                body = b"every=%ds last_scan=%ds_ago\n" % (SCAN_EVERY_S, last)
                cl.send(b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body))
                send_all(cl, body)
            elif path.startswith(b"/scan.jpg"):
                # 前回撮った5方向を保存分から返す（撮り直さないので速い）
                p = 0
                if b"p=" in path:
                    try:
                        p = int(path.split(b"p=", 1)[1].split(b"&", 1)[0])
                    except ValueError:
                        p = 0
                try:
                    send_file(cl, "scan%d.jpg" % max(0, min(4, p)))
                except Exception:
                    cl.send(b"HTTP/1.0 404 Not Found\r\n\r\nnot scanned yet\n")
            elif path.startswith(b"/baseline.bmp"):
                # 今の「きれいな状態の基準」を画像で確認する
                try:
                    send_bmp_from_file(cl, "baseline.raw")
                except Exception as e:
                    cl.send(b"HTTP/1.0 404 Not Found\r\n\r\nno baseline image (%s)\n" % str(e).encode())
            elif path.startswith(b"/view.bmp"):
                # 今カメラが見ている景色（人がいる時は撮らない）
                if presence_empty() or b"force=1" in path:
                    for _ in range(2):
                        cam.capture(); time.sleep_ms(60)
                    g = cam.capture()
                    servo3.rehold()
                    if g:
                        send_bmp_from_bytes(cl, g)
                    else:
                        cl.send(b"HTTP/1.0 500 Internal Server Error\r\n\r\ncapture failed\n")
                else:
                    cl.send(b"HTTP/1.0 403 Forbidden\r\n\r\noccupied\n")
            elif path.startswith(b"/photo"):
                # 検証用: Mが見ているのと同じ240x240白黒の生画像を返す。
                # 人を写さない原則のため在室中は拒否（検証でどうしても要る時だけ ?force=1）
                if presence_empty() or b"force=1" in path:
                    for _ in range(2):
                        cam.capture(); time.sleep_ms(60)
                    g = cam.capture()
                    if g:
                        cl.send(b"HTTP/1.0 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: %d\r\n\r\n" % len(g))
                        send_all(cl, g)
                        print("[+%ds] PHOTO %dB (%s)" % (el(), len(g), addr[0]))
                    else:
                        cl.send(b"HTTP/1.0 500 Internal Server Error\r\n\r\ncapture failed\n")
                else:
                    cl.send(b"HTTP/1.0 403 Forbidden\r\n\r\noccupied (no photo of people)\n")
            elif path.startswith(b"/mdiag"):
                # Mの内訳（輝度寄与・輪郭寄与・採用マス数）。数値が合わない時の切り分け用
                st = capture_means()
                if st is None or baseline is None:
                    cl.send(b"HTTP/1.0 500 Internal Server Error\r\n\r\nno data\n")
                else:
                    cur, cur_e = st
                    mb = sum(baseline) / NB; mc = sum(cur) / NB
                    ebm = sum(baseline_e) / NB; ecm = sum(cur_e) / NB
                    esc = (ebm / ecm) if ecm > 0.01 else 1.0
                    tb = te = 0.0; nb_ = 0
                    for i in range(NB):
                        d = abs((cur[i] - mc) - (baseline[i] - mb))
                        sb = (d - T0) / (T1 - T0)
                        sb = 0.0 if sb < 0 else (1.0 if sb > 1 else sb)
                        de = abs(cur_e[i] * esc - baseline_e[i])
                        se = (de - E0) / (E1 - E0)
                        se = 0.0 if se < 0 else (1.0 if se > 1 else se)
                        sc = sb * (1.0 - EDGE_W) + se * EDGE_W
                        if sc >= S_ON and _pcnt[i] >= K_PERSIST:
                            tb += sb * (1.0 - EDGE_W); te += se * EDGE_W; nb_ += 1
                    body = ("M=%.3f bright=%.3f edge=%.3f blocks=%d escale=%.3f base_m=%.1f cur_m=%.1f base_e=%.2f cur_e=%.2f\n"
                            % ((tb + te) / NB, tb / NB, te / NB, nb_, esc, mb, mc, ebm, ecm)).encode()
                    cl.send(b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body))
                    send_all(cl, body)
            elif path.startswith(b"/m"):
                empty = presence_empty()
                if empty:
                    st = capture_means()
                    if st is not None and baseline is not None:
                        _last_M = calc_M(st[0], st[1])
                M = _last_M
                N = calc_N(M)
                # 3列目=無人フラグ(1=無人で実測 / 0=在室で凍結値)。顔(XIAO)は先頭2列しか読まないので互換
                body = b"%.3f %.3f %d\n" % (M, N, 1 if empty else 0)
                cl.send(b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\n" + body)
                print("[+%ds] /m -> M=%.3f N=%.3f %s (%s)" %
                      (el(), M, N, "EMPTY" if empty else "occupied(hold)", addr[0]))
            elif path.startswith(b"/health"):
                cl.send(b"HTTP/1.0 200 OK\r\n\r\nok\n")
            else:
                cl.send(b"HTTP/1.0 404 Not Found\r\n\r\n")
        except Exception as e:
            print("[+%ds] SERVE ERR:" % el(), e)
        finally:
            try:
                time.sleep_ms(150)   # 送信バッファが吐き出されるのを待ってから閉じる（RST防止）
            except Exception:
                pass
            cl.close()
            gc.collect()
except KeyboardInterrupt:
    cam.deinit()
    srv.close()
    print("stopped")
except Exception as e:
    # 想定外の致命傷 → 5秒後に自動再起動して自己回復（スタンドアロン運用）
    print("FATAL:", e, "-> reboot in 5s")
    time.sleep(5)
    machine.reset()
