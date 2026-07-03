# esp32cam/main.py — 毎分撮影して arigato サーバーの /timelapse へ送る
# 対象: FREENOVE ESP32-WROVER CAM (OV3660)
#       + cnadler86/micropython-camera-API v0.6.2 WROVER_KIT ビルド (MicroPython v1.27)
# OV3660はこのボードでJPEG直撮りが失敗するため、RGB565で撮って jpeg.Encoder で変換する
# 設定はデバイス上の config.json（config.example.json を参照）
# 配置: mpremote fs cp esp32cam/main.py :main.py
#       mpremote fs cp esp32cam/config.json :config.json

import json
import time
import network
import machine
import gc

with open("config.json") as f:
    CFG = json.load(f)

WIFI_NETWORKS = CFG["wifi"]            # [[ssid, pass], ...]
SERVER = CFG["server"]
ZONE = CFG.get("zone", "test")         # 設置場所ごとに変える（英数字とハイフンのみ）
UPLOAD_KEY = CFG.get("upload_key", "") # Cloud Run の TIMELAPSE_KEY と同じ値
INTERVAL_S = CFG.get("interval_s", 60)
WARMUP_FRAMES = 3                      # AE/AWBが安定するまでの捨てフレーム数

LED = machine.Pin(33, machine.Pin.OUT)  # 基板裏の赤LED（0で点灯）
LED.value(1)


def led_blink(n, on_ms=80, off_ms=120):
    for _ in range(n):
        LED.value(0)
        time.sleep_ms(on_ms)
        LED.value(1)
        time.sleep_ms(off_ms)


def wifi_connect():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return True
    for ssid, passwd in WIFI_NETWORKS:
        print("WiFi接続試行:", ssid)
        try:
            wlan.connect(ssid, passwd)
        except OSError:
            continue
        for _ in range(20):
            if wlan.isconnected():
                print("WiFi接続成功:", ssid, wlan.ifconfig()[0])
                return True
            time.sleep(1)
    return False


def capture_jpeg():
    """カメラを起こして1枚撮り、JPEGに変換して必ず解放する。"""
    from camera import Camera, FrameSize, PixelFormat
    import jpeg
    cam = Camera(pixel_format=PixelFormat.RGB565, frame_size=FrameSize.SVGA)
    try:
        cam.set_vflip(True)  # OV3660は素のままだと上下逆
        for _ in range(WARMUP_FRAMES):
            cam.capture()
            time.sleep_ms(100)
        raw = cam.capture()
        if not raw:
            return None
        # capture()はフレームバッファ参照を返すためコピーしてから解放する
        raw = bytes(raw)
    finally:
        cam.deinit()
    enc = jpeg.Encoder(width=800, height=600, pixel_format="RGB565_BE", quality=85)
    img = enc.encode(raw)
    del raw
    gc.collect()
    return img


def upload(img):
    import requests
    url = "{}/timelapse?zone={}".format(SERVER, ZONE)
    headers = {"Content-Type": "image/jpeg"}
    if UPLOAD_KEY:
        headers["X-Upload-Key"] = UPLOAD_KEY
    r = requests.post(url, data=img, headers=headers)
    try:
        ok = r.status_code == 200
        print("POST", r.status_code, r.text[:120])
    finally:
        r.close()
    return ok


def main():
    print("timelapse起動 zone={} interval={}s".format(ZONE, INTERVAL_S))
    failures = 0
    while True:
        start = time.time()
        try:
            if not wifi_connect():
                raise OSError("WiFi接続失敗")
            img = capture_jpeg()
            if not img:
                raise OSError("撮影失敗")
            print("撮影 {} bytes".format(len(img)))
            if upload(img):
                failures = 0
                led_blink(1)  # 成功: 1回点滅
            else:
                raise OSError("アップロード失敗")
        except Exception as e:
            failures += 1
            print("エラー({}/5):".format(failures), e)
            led_blink(3)  # 失敗: 3回点滅
            if failures >= 5:
                print("連続失敗のため再起動")
                time.sleep(1)
                machine.reset()
        gc.collect()
        # 次の撮影時刻まで待つ（処理時間を差し引く）
        elapsed = time.time() - start
        wait = INTERVAL_S - elapsed
        if wait > 0:
            time.sleep(wait)


main()
