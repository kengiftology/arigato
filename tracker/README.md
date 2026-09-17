# tracker — 顔の精度B：追跡を使う新しい構成（M1 マック）

人物の検出・追跡・顔の判定をマックで行い、クラウドには「来た・いる・去った」だけを送る。
経緯は Windows 側の引き継ぎメモ（2026-09-17）。

## 用意（2026-09-17）
- Python は uv で入れた 3.11（`~/.local/bin/uv`）。`/usr/local` の道具は使わない（sha256sum などがIntel用で動かない）
- `cd tracker && uv sync` で `.venv` ができる
- カメラの URL は `~/.arigato/tapo.env` に `TAPO_URL=rtsp://…/stream1` と本人が書く（リポジトリに書かない）

## 計測（カメラなし・zidane.jpg 1280x720・2人）
| 構成 | 追跡 | 顔（2人分） | 合計 |
|---|---|---|---|
| YOLO11n CPU ＋ 顔 CPU | 36ms | 249ms | 285ms |
| **YOLO11n CPU ＋ 顔 CoreML** | 33ms | 35ms | **68ms（15fps目標で14.4fps出た）** |
| YOLO11n MPS ＋ 顔 CoreML | 34ms | 42ms | 76ms |
| YOLO11n ONNX（CPU / CoreML）＋ 顔 CoreML | 40ms | 34ms | 74ms |

- 2304x1296 でも変わらない（人の枠の中だけで顔を探すため）
- YOLO の CoreML 書き出しは torch 2.14 と coremltools の組み合わせで失敗。CPU で足りているので追わない
- 顔は1人あたり約17ms。5人同時・毎コマなら約120ms → 顔は毎コマでなく、トラックごとに間引けば足りる
- プロセスのメモリ 約1.2GB

```
uv run python bench.py --src rtsp --fps 10 --secs 60          # 主ストリーム
uv run python bench.py --src rtsp --sub --fps 10 --secs 60    # 副ストリーム
```
