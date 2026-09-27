# -*- coding: utf-8 -*-
"""区画をブラウザから足す口の試験（2026-09-27）。

なぜ要るか：本人の希望は「**誰でも、どこにでも、新しい場所を追加できる**」。
それまで区画はコードの中にしかなく、足すにはコードを直す必要があった。
ここで固定するのは4つ。

  1. 画面が開くこと
  2. **合言葉が要ること**（手元では鍵が未設定なので通る作り。本番と同じ形で確かめる）
  3. **`decided`（いつ・どうやって決めたか）が無いと保存できないこと**
  4. 保存した区画が**見回りの一覧に現れること**（現れなければ、足せた意味がない）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server.keys as keys
import server.routers.spirit as sp

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI                      # noqa: E402
from fastapi.testclient import TestClient        # noqa: E402


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(sp.router)
    return TestClient(app)


@pytest.fixture()
def state(monkeypatch):
    """状態を手元のひとつの辞書に差し替える（本番の記録には触らない）。"""
    st = {}
    monkeypatch.setattr(sp, "_load", lambda *a, **k: st)
    monkeypatch.setattr(sp, "_save", lambda s, *a, **k: None)
    monkeypatch.setattr(sp, "_log_event", lambda *a, **k: None)
    return st


def test_設定の画面が開く(client):
    r = client.get("/spirit/setup")
    assert r.status_code == 200
    for word in ("区画の設定", "合言葉", "いつ・どうやって決めたか", "区画を足す"):
        assert word in r.text


def test_設定を読める(client, state):
    r = client.get("/spirit/zones/cfg")
    assert r.status_code == 200
    j = r.json()
    assert "シンク" in j["zones"]
    assert j["photo_base"].startswith("https://")


def test_合言葉が要る(client, state, monkeypatch):
    # 本番と同じく、鍵が決まっている状態にする（本物の鍵は16進の文字列）
    monkeypatch.setattr(keys, "CURRENT", "0123456789abcdef")
    monkeypatch.setattr(keys, "PREV", "")
    assert client.post("/spirit/zones/cfg?zone=ためし&key=ちがう",
                       json={"decided": "2026-09-27 試験"}).status_code == 401
    assert client.post("/spirit/zones/cfg?zone=ためし",
                       json={"decided": "2026-09-27 試験"}).status_code == 401
    # 2026-09-27：日本語の鍵を送られると例外になり、401 ではなく 500 を返していた。
    # どちらも通さないが、**壊れたように見えると原因を追う先を間違える。**
    assert client.post("/spirit/zones/cfg?zone=ためし&key=" + "あ" * 8,
                       json={"decided": "2026-09-27 試験"}).status_code == 401
    # 合っていれば通る
    r = client.post("/spirit/zones/cfg?zone=ためし&key=0123456789abcdef",
                    json={"pose": "-0.50_0.00", "active": True,
                          "decided": "2026-09-27 試験"})
    assert r.status_code == 200, r.text


def test_いつどう決めたかが無いと保存できない(client, state):
    r = client.post("/spirit/zones/cfg?zone=ためし", json={"pose": "-0.50_0.00", "active": True})
    assert r.status_code == 400
    assert "decided" in r.json()["detail"]


def test_足した区画が見回りの一覧に現れる(client, state):
    r = client.post("/spirit/zones/cfg?zone=れいぞうこの上",
                    json={"id": "fridge", "active": True, "pose": "-0.20_-0.40",
                          "aim": {"ref": "spirit/zoneref/fridge.jpg", "band": [0.0, 1.0],
                                  "min_conf": 0.08, "max_shift": 60},
                          "crop": {"box": [0.1, 0.1, 0.9, 0.9], "rotate": 180},
                          "rule": {"kind": "change"},
                          "decided": "2026-09-27 本人が写真を見て選んだ"})
    assert r.status_code == 200, r.text
    assert "れいぞうこの上" in r.json()["names"]
    assert "れいぞうこの上" in sp._zone_names()          # 見回りが回る一覧
    assert sp._zone_cfg("れいぞうこの上")["pose"] == "-0.20_-0.40"
    # 向きも1周に加わる（ここが抜けると、足しても一生撮られない）
    assert "-0.20_-0.40" in sp._check_poses()


def test_区画を止められる(client, state):
    r = client.post("/spirit/zones/cfg?zone=IH",
                    json={"active": False, "decided": "2026-09-27 いったん止める"})
    assert r.status_code == 200
    assert "IH" not in sp._zone_names()


def test_おかしな値は保存できない(client, state):
    bad = [{"pose": "まんなか"}, {"pose": "-2.00_0.00"},
           {"crop": {"box": [0.9, 0.1, 0.2, 0.9]}}, {"rule": {"kind": "てきとう"}}]
    for body in bad:
        body = dict(body, decided="2026-09-27 試験")
        r = client.post("/spirit/zones/cfg?zone=ためし", json=body)
        assert r.status_code == 400, body
