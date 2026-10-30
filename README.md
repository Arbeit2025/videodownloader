# videodownloader

**Live Demo:** https://videodownloader-ycw2.onrender.com/

サイトのURLを貼ると、そのページにある動画ファイルを見つけてダウンロードできる小さなツールです。単一ファイルの Flask アプリ（`app.py`）＋ SQLite（履歴のみ）で動きます。動画ファイル自体はサーバーに保存しません。

## 使い方

1. トップページのフォームに、動画のあるページのURL（または動画ファイル自体のURL）を貼る
2. 「動画を探す」を押すと、見つかった動画が一覧で出る
3. 「ダウンロード」（サーバー中継でファイルとして保存）か「直接リンクを開く」を選ぶ

## 対応しているもの

- ページ内の `<video>` / `<source>` タグ
- `og:video` などのメタ情報、JSON-LD の `contentUrl`
- 動画ファイル（mp4 / webm / mov / m4v / mkv / avi / ogv 等）への直接リンク
- URL自体が動画ファイルの場合
- m3u8 / mpd は再生リストとして検出します（ダウンロードできるのはリストファイル自体で、映像の結合には対応プレーヤーや ffmpeg が必要です）

## 対応していないもの

- DRM付き動画、ログインが必要なページ
- JavaScriptで後から生成されるプレーヤー
- 直接の動画ファイルを持たない動画サイトの専用形式

## 注意

- 自分に利用権のある動画だけをダウンロードしてください。各サイトの利用規約も確認してください。
- 中継ダウンロードの上限は 200MB です。大きいファイルは直接リンクを使ってください。
- プライベート／ローカルアドレスへのアクセスは安全のためブロックしています。

## ローカル起動

```bash
pip install -r requirements.txt
python app.py
# http://127.0.0.1:5000
```

Render では Start Command を `gunicorn app:app` にしてデプロイできます（履歴の SQLite は再デプロイ等で消えることがあります）。

## テスト

```bash
python -m unittest discover -s tests -v
```
