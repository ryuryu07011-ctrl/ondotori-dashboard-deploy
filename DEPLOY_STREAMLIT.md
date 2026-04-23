# Streamlit 外部公開手順

## 1. secrets を作成

1. `.streamlit/secrets.toml.example` をコピーして `.streamlit/secrets.toml` を作成
2. 値を実データに置換

```toml
[ondotori]
api_key = "..."
login_id = "..."
login_pass = "..."
base_serial = "..."
```

## 2. ローカル確認

```powershell
python -m streamlit run "APIデータ抽出　ベース.py"
```

## 3. GitHub へアップロード

```powershell
git init
git add .
git commit -m "Prepare app for Streamlit Cloud deployment"
git branch -M main
git remote add origin <YOUR_GITHUB_REPO_URL>
git push -u origin main
```

## 4. Streamlit Community Cloud へデプロイ

1. Streamlit Cloud に GitHub 連携
2. リポジトリ選択
3. Main file path: `APIデータ抽出　ベース.py`
4. Advanced settings > Secrets に `secrets.toml` の中身を貼り付け
5. Deploy

## 5. 顧客共有

- 発行された `https://...streamlit.app` URL を共有
- URL を知っている人だけに限定する場合は別途認証レイヤーを追加
