# arigato - ありがとうアプリ

## プロジェクト概要
修士論文の研究用Webアプリ。
共有空間の整備記録を投稿し、ありがとうを届け合うシステム。

**GitHubリポジトリ：** https://github.com/kengiftology/arigato
**本番URL：** https://arigato-3ipecjbnha-an.a.run.app

---

## 研究の問い

「管理者なしに、ありがとうだけで共有空間の維持は持続するか」

### 前提の認識
- 今のゼミ室は教員の叱責（B）と掃除のおばちゃん（C）で維持されている
- 顔見知りの小さなコミュニティでさえ、互酬的なA（ありがとう）は自然に起きていない
- BとCがない空間でAだけが機能するかを観察する

### システムの役割
「埋もれているありがとうを掘り起こして届ける装置」
（ありがとうを人工的に生み出すのではなく、本来あったのに届かなかったものを通す）

### 2つのループ
```
基本ループ（既存の人を維持する）：
整備する → 投稿 → ありがとうが届く → また整備したくなる

新しいループ（新しい人を引き込む）：
積み重ねが見える → 自分もやりたくなる → 整備・投稿
```

新しいループの方が研究として面白い。③積み重ねの可視化がその核心。

### 理想の使われ方
- 整備した人が「記録を残す」
- 気づいた人が「誰がやったか確認してありがとうを送る」
- アプリを開くのは「誰かがやってくれたかも」と思ったとき

---

## 研究フィールド

両方を対象にする。ゾーンは使い始めてから自然に決める（先に決めない）。

| 場所 | 特徴 |
|------|------|
| 畑（SBCファーム） | B・Cなし、純粋にAだけ。人と動きは少ない |
| ゼミ室 | 人と動きはある。おばちゃんが来ない区画に絞る |

---

## アーキテクチャ

```
PWA（ブラウザ）
  ↕
FastAPI（Cloud Run / asia-northeast1）
  ↕ Firestore（整備記録・ありがとう・通知購読）
  ↕ GCS（写真 arigato-photos バケット）
```

### CI/CD
GitHub Actions → Cloud Run 自動デプロイ

### 環境変数（Cloud Run設定済み）
- `GCS_BUCKET` = arigato-photos
- `VAPID_EMAIL` / `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY`

---

## 主要ファイル

| ファイル | 役割 |
|---------|------|
| `server/app.py` | FastAPIエントリポイント |
| `server/routers/maintenance.py` | 整備記録のCRUD |
| `server/routers/thanks.py` | ありがとうの送信（/welcomeを先頭に置くこと） |
| `server/routers/push.py` | プッシュ通知のVAPIDキー・購読登録 |
| `server/storage.py` | GCS写真アップロード |
| `server/push.py` | 実際の通知送信ロジック |
| `pwa/app.js` | フロントエンドロジック |
| `pwa/sw.js` | Service Worker（プッシュ通知受信） |

---

## 完了済み ✅
- 整備記録の投稿・表示
- 写真アップロード（GCS）
- ありがとうボタン（手動・自動）
- プッシュ通知（VAPID）
- ゾーン別フィルター（/zone/{zone_id}）

## 未実装・検討中 ❌
- [ ] ③積み重ねの可視化（コミュニティ全体の蓄積表示）
- [ ] NFCタグ連携（ESP32+PN532でNTAG215に書き込み）
- [ ] アドミンページ（ゾーンURL一覧表示）

---

## NFCタグ設計（予定）

各ゾーンにNTAG215タグを設置。かざすとそのゾーンの整備記録が開く。

- 書き込み内容：`https://arigato-3ipecjbnha-an.a.run.app/zone/{zone_id}`
- 書き込みツール：ESP32 + PN532（MicroPython）
- 書き込み保護：NTAG215のパスワード保護機能を使用

---

## よくある注意点

- `thanks.py` のルート順：`/welcome` は必ず `/{maintenance_id}` より前に定義する
- 写真URLはGCSの完全URL。`/maintenance/photo/` プレフィックスを付けない
- GCS バケットはbucket-level IAMで公開（blob.make_public()は不要・エラーになる）
- Firestore の `.where() + .order_by()` は複合インデックスが必要。クライアントソートで回避
