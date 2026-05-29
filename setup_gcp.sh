#!/bin/bash
# GCPリソースのセットアップスクリプト
# 実行前に: gcloud auth login && gcloud config set project YOUR_PROJECT_ID

PROJECT_ID=$(gcloud config get-value project)
BUCKET="arigato-photos"
REGION="asia-northeast1"

echo "Project: $PROJECT_ID"

# APIを有効化
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  cloudbuild.googleapis.com

# Firestoreをネイティブモードで初期化（未設定の場合）
gcloud firestore databases create --location=$REGION 2>/dev/null || echo "Firestore already exists"

# Cloud Storageバケットを作成
gsutil mb -l $REGION gs://$BUCKET 2>/dev/null || echo "Bucket already exists"

# バケットを公開読み取り可能に設定
gsutil iam ch allUsers:objectViewer gs://$BUCKET

echo ""
echo "=== VAPIDキーの生成 ==="
python3 -c "
from py_vapid import Vapid
v = Vapid()
v.generate_keys()
print('VAPID_PUBLIC_KEY=', v.public_key.decode())
print('VAPID_PRIVATE_KEY=', v.private_key.decode())
" 2>/dev/null || echo "pip install py_vapid してから再実行してください"

echo ""
echo "=== GitHub Secretsに追加するもの ==="
echo "GCP_PROJECT_ID: $PROJECT_ID"
echo "GCP_SA_KEY: サービスアカウントキー（既存のものを使用）"
echo "VAPID_PUBLIC_KEY: 上記で生成した値"
echo "VAPID_PRIVATE_KEY: 上記で生成した値"
echo "VAPID_EMAIL: mailto:kengk0328@gmail.com"
