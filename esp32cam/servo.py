# esp32cam/servo.py — SG90/MG90S サーボ制御（パンチルト用）
# 50Hz PWM、パルス幅 0.5ms(0度) 〜 2.4ms(180度)
# 使い方:
#   from servo import Servo
#   pan = Servo(14)
#   pan.move_to(90)        # ゆっくり90度へ
#   pan.detach()           # 動かし終わったら信号を止める（ジッタ防止）

import time
from machine import Pin, PWM

_PERIOD_MS = 20.0
# 手持ちSG90の実測較正値（2026-07-04、端から端で約180°・唸りなし）
_MIN_MS = 0.30  # 0度のパルス幅
_MAX_MS = 2.70  # 180度のパルス幅


class Servo:
    def __init__(self, pin, start_deg=None):
        self._pin = pin
        self._pwm = None
        self._deg = start_deg  # 現在角度（不明ならNone）

    def _duty(self, deg):
        ms = _MIN_MS + (_MAX_MS - _MIN_MS) * deg / 180.0
        return int(ms / _PERIOD_MS * 65535)

    def attach(self):
        if self._pwm is None:
            self._pwm = PWM(Pin(self._pin), freq=50)

    def detach(self):
        """PWMを止める。SG90は軽負荷なら無信号でも位置を保持する。"""
        if self._pwm is not None:
            self._pwm.deinit()
            self._pwm = None

    def write(self, deg):
        """即座にその角度のパルスを出す（ステップ移動の内部用）。"""
        deg = max(0, min(180, deg))
        self.attach()
        self._pwm.duty_u16(self._duty(deg))
        self._deg = deg

    def move_to(self, deg, step=2, step_ms=20):
        """ゆっくり目標角度へ。電流スパイクと振動を抑える。
        現在角度が不明（起動直後）のときは一気に動く（1回だけ）。"""
        deg = max(0, min(180, deg))
        if self._deg is None:
            self.write(deg)
            time.sleep_ms(500)  # 到達待ち
            return
        cur = self._deg
        d = step if deg > cur else -step
        while abs(deg - cur) > step:
            cur += d
            self.write(cur)
            time.sleep_ms(step_ms)
        self.write(deg)
        time.sleep_ms(100)
