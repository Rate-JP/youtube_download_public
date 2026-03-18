# YTDownload (YouTube Downloader API)

`yt-dlp` + `ffmpeg` をバックエンドに、YouTube の動画 / プレイリストを **mp4 / m4a / mp3** で取得するための FastAPI サービスです。  
Docker イメージ内に **XRDP + GUI Chrome** を同梱しており、RDP でログインして YouTube にサインインし、Chrome の Cookie を `yt-dlp` に渡す構成を想定しています。

> ⚠️ 注意  
> 本ツールの利用は、YouTube 等の利用規約・法令・権利者の権利を遵守した範囲で行ってください。  
> 本リポジトリは技術検証 / 個人運用を想定しています。

---

## できること

- **`POST /formats`**  
  URL を解析し、動画またはプレイリストの基本情報と、選択可能なプリセット情報を取得します。プレイリストはページング対応です。
- **`POST /download`**  
  ダウンロードジョブを投入します。動画 / 音声ごとの並列数制御があり、上限を超えた分は待機キューに入ります。
- **`GET /progress` / `POST /progress`**  
  progress key を使って、単体または複数ジョブの進捗を取得します。
- **`POST /files/download`**  
  完了済みファイルを取得します。複数指定時は ZIP で返します。
- **`GET /server/status`**  
  バージョン情報、各キュー状況、YouTube 情報取得キュー、日次上限の使用状況を取得します。
- **`GET /cleanup/{secret_path}`**  
  保存期限を過ぎたファイルを削除します。

---

## 主な仕様

### 1. プリセット選択方式

この API は `/formats` で詳細な全 format 一覧を返すのではなく、**フロント側が扱いやすいプリセット前提**で動きます。

- 動画画質: `2160p` / `1440p` / `1080p` / `720p`
- 音声形式: `m4a` / `mp3`
- fallback policy: `nearest_lower`
- preferred container: `mp4`

### 2. `/download` の指定方法

`/download` は **リクエスト全体で共通指定**しつつ、必要なら `items[]` 側で個別上書きできます。

例:

- リクエスト全体で `media_type=audio` `audio_format=mp3`
- `items[]` には URL や playlist 内 index だけを並べる
- 一部 item だけ別 quality を指定して上書きすることも可能

### 3. 既存ファイル再利用

同じ出力ファイルがすでに存在する場合は、新規ダウンロードを行わず **`status=reused`** で返します。

- 既存ファイルがある場合は再ダウンロードしません
- 同じ progress key のジョブが実行中の場合も、そのジョブを参照します
- `reused` の場合は **変換上限を消費しません**

### 4. キュー制御

- 動画ジョブ: `MAX_CONCURRENT_VIDEO_JOBS`
- 音声ジョブ: `MAX_CONCURRENT_AUDIO_JOBS`
- YouTube 情報取得（`/formats` や `/download` 事前情報取得）: `MAX_CONCURRENT_YOUTUBE_INFO_JOBS`

上限を超えたジョブは失敗ではなく、**待機状態 (`queued`)** になります。

### 5. プレイリストページング

`POST /formats` で `target_type=playlist` の場合、`playlist_start_index` / `playlist_end_index` で範囲指定できます。  
ただし 1 回で返す件数は `PLAYLIST_MAX_ITEMS` を上限として丸められます。

---

## 動作要件

- Ubuntu 22.04 / 24.04 系
- Docker / Docker Compose Plugin
- RDP クライアント（Windows の「リモート デスクトップ接続」など）
- 公開用ドメイン（推奨）
- 必要に応じて Nginx / Northflank などのリバースプロキシ

---

## クイックスタート

### 1) ビルド

```bash
git clone <this-repo>
cd <this-repo>
docker build -t ytdownload:latest .
```

### 2) `.env` 例

```dotenv
# ------------------------------------------------------------
# App / Security
# ------------------------------------------------------------
ALLOWED_IPS=127.0.0.1/32,::1/128
TRUST_PROXY_HEADERS=false
TRUSTED_PROXY_IPS=

PORT=8000
DOWNLOAD_ROOT=dl
REQUEST_TIMEOUT=300
FILE_TTL_HOURS=12

MAX_DURATION_SECONDS_MP3=1800
MAX_DURATION_SECONDS_MP4=1800
MAX_FILE_SIZE_MB_MP3=512
MAX_FILE_SIZE_MB_MP4=2048

YOUTUBE_COOKIES_ENABLED=true
YOUTUBE_COOKIE_FILE=/app/youtube_cookies.txt
YOUTUBE_COOKIE_REFRESH_MINUTES=30
YOUTUBE_COOKIE_REFRESH_SCRIPT=get_youtube_cookie.py
YOUTUBE_COOKIE_REFRESH_TIMEOUT_SECONDS=120

MAX_CONCURRENT_VIDEO_JOBS=3
MAX_CONCURRENT_AUDIO_JOBS=10
MAX_CONCURRENT_YOUTUBE_INFO_JOBS=2
MAX_DOWNLOAD_ITEMS_PER_REQUEST=10

FORMATS_DAILY_LIMIT=300
CONVERSIONS_DAILY_LIMIT=300
LIMIT_RESET_TIMEZONE=Asia/Tokyo
SERVER_STATUS_COMMAND_TIMEOUT_SECONDS=5

CLEANUP_SECRET_PATH=cleanup-change-me
PROGRESS_RETENTION_MINUTES=1440
LOCK_TIMEOUT_SECONDS=900

PLAYLIST_MAX_ITEMS=100
PLAYLIST_CONTINUE_ON_ERROR=true
PLAYLIST_SAVE_ROOT=dl/playlists

CLEANUP_REQUIRE_HEADER_TOKEN=false
CLEANUP_HEADER_TOKEN=

NORTHFLANK_PUBLIC_BASE_URL=
UVICORN_WORKERS=1
LOG_LEVEL=INFO

# ------------------------------------------------------------
# Container / Entrypoint
# ------------------------------------------------------------
APP_MODULE=app.main:app
APP_HOST=0.0.0.0
APP_PORT=8000
APP_CMD=

CHROME_BINARY=/usr/bin/google-chrome
CHROME_REMOTE_DEBUGGING_HOST=127.0.0.1
CHROME_REMOTE_DEBUGGING_PORT=9222

YOUTUBE_LOGIN_BASE_DIR=/app/youtube_login
YOUTUBE_PROFILE_DIR=/app/youtube_login/chrome_profile

RDP_USER=appuser
RDP_PASSWORD=change-me
RDP_START_URL=https://www.youtube.com/

DENO_BINARY=/usr/local/bin/deno

# ------------------------------------------------------------
# get_youtube_cookie.py / optional advanced
# ------------------------------------------------------------
COOKIE_OUTPUT_BASE_DIR=/app
CHROME_DEBUG_WAIT_SECONDS=15
YOUTUBE_NAVIGATION_WAIT_SECONDS=5
COOKIE_DOMAIN_REGEX=
YOUTUBE_COOKIE_REFRESH_URL=https://www.youtube.com/
```

---

## 主な ENV の意味

### アプリ本体

| ENV | 既定値 | 説明 |
|---|---:|---|
| `ALLOWED_IPS` | `127.0.0.1/32,::1/128` | API の許可元 IP / CIDR（CSV） |
| `TRUST_PROXY_HEADERS` | `false` | `X-Forwarded-*` を信頼するか |
| `TRUSTED_PROXY_IPS` | 空 | 信頼するプロキシ IP / CIDR（CSV） |
| `PORT` | `8000` | API ポート |
| `DOWNLOAD_ROOT` | `dl` | 通常ダウンロード保存先 |
| `REQUEST_TIMEOUT` | `300` | 外部コマンドや処理全体のタイムアウト秒 |
| `FILE_TTL_HOURS` | `12` | cleanup 対象になるまでの保存時間 |
| `MAX_DURATION_SECONDS_MP3` | `1800` | mp3 / m4a 取得時の最大長さ（秒） |
| `MAX_DURATION_SECONDS_MP4` | `1800` | mp4 取得時の最大長さ（秒） |
| `MAX_FILE_SIZE_MB_MP3` | `512` | mp3 想定最大サイズ（MB） |
| `MAX_FILE_SIZE_MB_MP4` | `2048` | mp4 想定最大サイズ（MB） |
| `YOUTUBE_COOKIES_ENABLED` | `true` | Cookie 利用の有効 / 無効 |
| `YOUTUBE_COOKIE_FILE` | `youtube_cookies.txt` 相当 | Cookie 保存先 |
| `YOUTUBE_COOKIE_REFRESH_MINUTES` | `30` | Cookie 再取得までの目安分数 |
| `YOUTUBE_COOKIE_REFRESH_SCRIPT` | `get_youtube_cookie.py` | Cookie 更新スクリプト |
| `YOUTUBE_COOKIE_REFRESH_TIMEOUT_SECONDS` | `120` | Cookie 更新スクリプトのタイムアウト秒 |
| `MAX_CONCURRENT_VIDEO_JOBS` | `3` | 動画ジョブの最大並列数 |
| `MAX_CONCURRENT_AUDIO_JOBS` | `10` | 音声ジョブの最大並列数 |
| `MAX_CONCURRENT_YOUTUBE_INFO_JOBS` | `2` | YouTube 情報取得の最大並列数。旧名 `MAX_CONCURRENT_VIDEO_FETCH_JOBS` も互換利用可 |
| `MAX_DOWNLOAD_ITEMS_PER_REQUEST` | `10` | `/download` 1 回で指定できる最大件数 |
| `FORMATS_DAILY_LIMIT` | `300` | `/formats` の 1 日あたり上限。`0` 以下で実質無制限 |
| `CONVERSIONS_DAILY_LIMIT` | `300` | 新規変換ジョブの 1 日あたり上限。旧 `FILE_DOWNLOADS_DAILY_LIMIT` も互換利用可 |
| `LIMIT_RESET_TIMEZONE` | `Asia/Tokyo` | 日次上限リセット基準のタイムゾーン |
| `SERVER_STATUS_COMMAND_TIMEOUT_SECONDS` | `5` | `/server/status` のバージョン取得コマンド待機秒 |
| `CLEANUP_SECRET_PATH` | `cleanup-change-me` | cleanup 用の URL シークレット |
| `PROGRESS_RETENTION_MINUTES` | `1440` | 進捗情報の保持時間（分） |
| `LOCK_TIMEOUT_SECONDS` | `900` | ロックのタイムアウト秒 |
| `PLAYLIST_MAX_ITEMS` | `100` | `/formats` の playlist 1 回返却上限 |
| `PLAYLIST_CONTINUE_ON_ERROR` | `true` | プレイリスト処理途中のエラー時に継続するか |
| `PLAYLIST_SAVE_ROOT` | `dl/playlists` | プレイリスト保存先 |
| `CLEANUP_REQUIRE_HEADER_TOKEN` | `false` | cleanup に追加ヘッダを要求するか |
| `CLEANUP_HEADER_TOKEN` | 空 | cleanup 用ヘッダトークン |
| `NORTHFLANK_PUBLIC_BASE_URL` | 空 | 互換目的の ENV。現行コードでは直接参照されません |
| `UVICORN_WORKERS` | `1` | 1 固定前提。複数ワーカーは非対応 |
| `LOG_LEVEL` | `INFO` | ログレベル |

### コンテナ / RDP / Chrome

| ENV | 既定値 | 説明 |
|---|---:|---|
| `APP_MODULE` | `app.main:app` | uvicorn 起動対象 |
| `APP_HOST` | `0.0.0.0` | API bind 先 |
| `APP_PORT` | `8000` | API bind ポート |
| `APP_CMD` | 空 | 指定時は独自コマンドで起動 |
| `CHROME_BINARY` | `/usr/bin/google-chrome` | Chrome バイナリ |
| `CHROME_REMOTE_DEBUGGING_HOST` | `127.0.0.1` | Chrome DevTools host |
| `CHROME_REMOTE_DEBUGGING_PORT` | `9222` | Chrome DevTools port |
| `YOUTUBE_LOGIN_BASE_DIR` | `/app/youtube_login` | YouTube ログイン保存ベース |
| `YOUTUBE_PROFILE_DIR` | `/app/youtube_login/chrome_profile` | Chrome プロファイル保存先 |
| `RDP_USER` | `appuser` | RDP ログインユーザー |
| `RDP_PASSWORD` | `change-me` | RDP ログインパスワード |
| `RDP_START_URL` | `https://www.youtube.com/` | RDP ログイン時に開く URL |
| `DENO_BINARY` | `/usr/local/bin/deno` | Deno バイナリ |

### Cookie 取得スクリプト補助 ENV

| ENV | 既定値 | 説明 |
|---|---:|---|
| `COOKIE_OUTPUT_BASE_DIR` | `/app` | 相対パス解決ベース |
| `CHROME_DEBUG_WAIT_SECONDS` | `15` | DevTools 起動待機秒 |
| `YOUTUBE_NAVIGATION_WAIT_SECONDS` | `5` | YouTube 遷移後の待機秒 |
| `COOKIE_DOMAIN_REGEX` | 空 | Cookie 抽出対象ドメイン絞り込み |
| `YOUTUBE_COOKIE_REFRESH_URL` | `https://www.youtube.com/` | Cookie 更新時に開く URL |

---

## 起動例（Docker）

推奨: API はローカルバインド（`127.0.0.1:8000`）し、外部公開は Nginx / Northflank などのプロキシ越しで行います。  
RDP は必要に応じて公開し、必ず IP 制限してください。

```bash
docker run -d --name ytdownload \
  --restart unless-stopped \
  --env-file .env \
  -p 127.0.0.1:8000:8000 \
  -p 3389:3389 \
  -v "$PWD/dl:/app/dl" \
  -v "$PWD/youtube_login:/app/youtube_login" \
  -v "$PWD/youtube_cookies.txt:/app/youtube_cookies.txt" \
  ytdownload:latest
```

### bind mount の意味

- `./dl:/app/dl`  
  ダウンロード済みファイルをホスト側へ保持
- `./youtube_login:/app/youtube_login`  
  Chrome プロファイル・ログイン状態を保持
- `./youtube_cookies.txt:/app/youtube_cookies.txt`  
  エクスポート済み Cookie ファイルを保持

---

## 初回セットアップ（YouTube ログイン & Cookie）

1. RDP で VPS に接続します（`<VPSのIP>:3389`）
2. デスクトップ起動後、Chrome が自動起動し、YouTube が開きます
3. YouTube にログインします
4. `youtube_login` を bind mount していれば、ログイン状態は Chrome プロファイルとして保持されます
5. 必要に応じて Cookie 更新スクリプトを手動実行します

```bash
python /app/get_youtube_cookie.py
```

> `youtube_login` と `youtube_cookies.txt` を bind mount していれば、ログイン情報や Cookie はホスト側に保持されます。  
> ただし Cookie の鮮度や YouTube 側状態によっては、再起動後に **RDP 接続 → YouTube 表示 → `get_youtube_cookie.py` 実行** が必要です。

---

## API 一覧

- `GET /healthz`
- `POST /formats`
- `POST /download`
- `GET /progress?key=...`
- `POST /progress`
- `POST /files/download`
- `GET /server/status`
- `GET /cleanup/{secret_path}`

Swagger UI:

- `http://127.0.0.1:8000/docs`

---

# API 詳細

## 共通: エラーレスポンス形式

業務エラー時は概ね次の形式です。

```json
{
  "success": false,
  "error_code": "conversions_daily_limit_exceeded",
  "message": "動画/音声変換の1日あたりの上限に達しました",
  "limit": 300,
  "used_today": 300,
  "requested": 1,
  "reset_at": "2026-03-19T15:00:00+00:00",
  "timezone": "Asia/Tokyo"
}
```

主な `error_code` 例:

- `ip_not_allowed`
- `formats_daily_limit_exceeded`
- `conversions_daily_limit_exceeded`
- `download_items_limit_exceeded`
- `duration_limit_exceeded`
- `invalid_quality`
- `progress_key_not_found`
- `file_not_ready`
- `file_not_found`
- `cleanup_token_invalid`

---

## 1. `GET /healthz`

### 概要

サーバーの簡易ヘルスチェックです。

### Python サンプル

```python
import requests

BASE_URL = "http://127.0.0.1:8000"

resp = requests.get(f"{BASE_URL}/healthz", timeout=30)
resp.raise_for_status()
print(resp.json())
```

### レスポンス例

```json
{
  "status": "ok"
}
```

---

## 2. `POST /formats`

### 概要

URL を解析し、**動画**または**プレイリスト**としての基本情報を返します。  
プレイリスト時は `playlist_start_index` / `playlist_end_index` によるページングが可能です。

### リクエストパラメータ

| パラメータ | 型 | 必須 | 説明 |
|---|---|---:|---|
| `url` | string (URL) | 必須 | YouTube 動画 URL またはプレイリスト URL |
| `target_type` | string | 任意 | `auto` / `video` / `playlist`。省略時は `auto` |
| `playlist_start_index` | integer | 任意 | プレイリスト取得開始位置（1 始まり） |
| `playlist_end_index` | integer | 任意 | プレイリスト取得終了位置（1 始まり） |

### `target_type` の意味

- `auto`: URL から自動判定
- `video`: 動画として扱う
- `playlist`: プレイリストとして扱う

### Python サンプル（動画 URL）

```python
import json
import requests

BASE_URL = "http://127.0.0.1:8000"

payload = {
    "url": "https://www.youtube.com/watch?v=VIDEO_ID",
    "target_type": "auto"
}

resp = requests.post(f"{BASE_URL}/formats", json=payload, timeout=120)
resp.raise_for_status()
print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

### レスポンス例（動画）

```json
{
  "success": true,
  "resource_type": "video",
  "video_id": "VIDEO_ID",
  "title": "サンプル動画",
  "sanitized_title": "サンプル動画",
  "duration_seconds": 245,
  "thumbnail_url": "https://i.ytimg.com/vi/VIDEO_ID/maxresdefault.jpg",
  "uploader": "example channel",
  "upload_date": "2026-03-01",
  "available_presets": {
    "video_qualities": ["2160p", "1440p", "1080p", "720p"],
    "audio_formats": ["m4a", "mp3"],
    "fallback_policy": ["nearest_lower"],
    "preferred_container": ["mp4"]
  },
  "cookie_warning": null
}
```

### Python サンプル（プレイリスト URL）

```python
import json
import requests

BASE_URL = "http://127.0.0.1:8000"

payload = {
    "url": "https://www.youtube.com/playlist?list=PLAYLIST_ID",
    "target_type": "playlist",
    "playlist_start_index": 1,
    "playlist_end_index": 15
}

resp = requests.post(f"{BASE_URL}/formats", json=payload, timeout=120)
resp.raise_for_status()
print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

### レスポンス例（プレイリスト）

```json
{
  "success": true,
  "resource_type": "playlist",
  "playlist_id": "PLAYLIST_ID",
  "playlist_title": "カラオケ",
  "playlist_count": 40,
  "uploader": "抹茶ラテ@Rate",
  "accepted_count": 15,
  "rejected_count": 0,
  "available_presets": {
    "video_qualities": ["2160p", "1440p", "1080p", "720p"],
    "audio_formats": ["m4a", "mp3"],
    "fallback_policy": ["nearest_lower"],
    "preferred_container": ["mp4"]
  },
  "playlist_chunk": {
    "start_index": 1,
    "end_index": 15,
    "page_size": 15,
    "returned_count": 15,
    "returned_start_index": 1,
    "returned_end_index": 15,
    "has_more": true,
    "next_start_index": 16,
    "next_end_index": 30
  },
  "entries": [
    {
      "index": 1,
      "video_id": "VIDEO_ID_001",
      "title": "1曲目",
      "duration_seconds": 240,
      "thumbnail_url": "https://i.ytimg.com/vi/VIDEO_ID_001/default.jpg",
      "uploader": "example channel",
      "upload_date": "2026-02-01",
      "is_downloadable": true,
      "reject_reason": null
    }
  ],
  "cookie_warning": null
}
```

### レスポンス項目の意味

#### 共通

| 項目 | 説明 |
|---|---|
| `success` | 成功時は `true` |
| `resource_type` | `video` または `playlist` |
| `available_presets` | フロント側で選択できるプリセット候補 |
| `cookie_warning` | Cookie 利用に関する警告。問題なければ `null` |

#### 動画時

| 項目 | 説明 |
|---|---|
| `video_id` | YouTube 動画 ID |
| `title` | 元の動画タイトル |
| `sanitized_title` | ファイル名用にサニタイズされたタイトル |
| `duration_seconds` | 動画長さ（秒） |
| `thumbnail_url` | サムネイル URL |
| `uploader` | 投稿者名 |
| `upload_date` | 投稿日（`YYYY-MM-DD`） |

#### プレイリスト時

| 項目 | 説明 |
|---|---|
| `playlist_id` | プレイリスト ID |
| `playlist_title` | プレイリストタイトル |
| `playlist_count` | プレイリスト総件数（取得できた範囲ベースの場合あり） |
| `accepted_count` | ダウンロード候補として扱える件数 |
| `rejected_count` | 候補外件数 |
| `playlist_chunk` | 今回返した範囲情報 |
| `entries` | 今回返した動画一覧 |

#### `playlist_chunk`

| 項目 | 説明 |
|---|---|
| `start_index` | リクエスト開始位置 |
| `end_index` | リクエスト終了位置 |
| `page_size` | 実際に採用されたページサイズ |
| `returned_count` | 実際に返した件数 |
| `returned_start_index` | 実際に返した先頭 index |
| `returned_end_index` | 実際に返した末尾 index |
| `has_more` | 次ページがありそうか |
| `next_start_index` | 次回取得開始 index |
| `next_end_index` | 次回取得終了 index |

#### `entries[]`

| 項目 | 説明 |
|---|---|
| `index` | プレイリスト内位置（1 始まり） |
| `video_id` | 動画 ID |
| `title` | 動画タイトル |
| `duration_seconds` | 動画長さ（秒） |
| `thumbnail_url` | サムネイル URL |
| `uploader` | 投稿者 |
| `upload_date` | 投稿日 |
| `is_downloadable` | ダウンロード候補として扱えるか |
| `reject_reason` | 取得不可理由。通常は `null` |

---

## 3. `POST /download`

### 概要

ダウンロードジョブを投入します。  
**共通パラメータをリクエスト上位で指定**し、必要に応じて `items[]` 側で上書きします。

### リクエストパラメータ

#### リクエスト上位

| パラメータ | 型 | 必須 | 説明 |
|---|---|---:|---|
| `items` | array | 必須 | ダウンロード対象一覧 |
| `target_type` | string | 必須 | `video` / `playlist` |
| `media_type` | string | 必須 | `video` / `audio` |
| `quality` | string | 条件付き | `media_type=video` のとき必須。`2160p` / `1440p` / `1080p` / `720p` |
| `audio_format` | string | 条件付き | `media_type=audio` のとき必須。`m4a` / `mp3` |
| `fallback_policy` | string | 任意 | 現在は `nearest_lower` のみ |
| `preferred_container` | string | 条件付き | `media_type=video` のとき `mp4` |

#### `items[]`

| パラメータ | 型 | 必須 | 説明 |
|---|---|---:|---|
| `url` | string (URL) | 必須 | 動画 URL またはプレイリスト URL |
| `video_id` | string | 任意 | URL から取れない場合の補助指定 |
| `playlist_id` | string | 条件付き | `target_type=playlist` のとき必須 |
| `index` | integer | 条件付き | `target_type=playlist` のとき必須。プレイリスト内位置 |
| `quality` | string | 任意 | item 単位の動画画質上書き |
| `audio_format` | string | 任意 | item 単位の音声形式上書き |
| `fallback_policy` | string | 任意 | item 単位の fallback 上書き |
| `preferred_container` | string | 任意 | item 単位のコンテナ上書き |

### 指定ルール

- `target_type=video`  
  単体動画として扱います
- `target_type=playlist`  
  `playlist_id` と `index` が必要です
- `media_type=video`  
  `quality` が必要です。`audio_format` は実質 `m4a` 固定、`preferred_container` は `mp4` 固定です
- `media_type=audio`  
  `audio_format` が必要です。`quality` / `preferred_container` は無視されます
- item 側の値があれば、上位より item 側が優先されます

### Python サンプル（単体 mp3）

```python
import json
import requests

BASE_URL = "http://127.0.0.1:8000"

payload = {
    "items": [
        {
            "url": "https://www.youtube.com/watch?v=VIDEO_ID"
        }
    ],
    "target_type": "video",
    "media_type": "audio",
    "audio_format": "mp3",
    "fallback_policy": "nearest_lower"
}

resp = requests.post(f"{BASE_URL}/download", json=payload, timeout=120)
resp.raise_for_status()
print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

### レスポンス例（単体 queued）

```json
{
  "success": true,
  "accepted_count": 1,
  "queued_count": 1,
  "reused_count": 0,
  "failed_count": 0,
  "items": [
    {
      "video_id": "VIDEO_ID",
      "playlist_id": null,
      "index": null,
      "title": "サンプル動画",
      "media_type": "audio",
      "requested_quality": null,
      "requested_audio_format": "mp3",
      "fallback_policy": "nearest_lower",
      "preferred_container": null,
      "progress_key": "VIDEO_ID_audio_mp3",
      "status": "queued",
      "downloaded": false,
      "reused": false,
      "file_path": "VIDEO_ID/audio_mp3/サンプル動画.mp3",
      "expires_at": "2026-03-19T01:00:00+00:00",
      "error_code": null,
      "message": "ジョブを受け付けました",
      "quality_check_pending": false
    }
  ]
}
```

### Python サンプル（プレイリストを 2 件まとめて mp4）

```python
import json
import requests

BASE_URL = "http://127.0.0.1:8000"

payload = {
    "items": [
        {
            "url": "https://www.youtube.com/playlist?list=PLAYLIST_ID",
            "playlist_id": "PLAYLIST_ID",
            "index": 1
        },
        {
            "url": "https://www.youtube.com/playlist?list=PLAYLIST_ID",
            "playlist_id": "PLAYLIST_ID",
            "index": 2
        }
    ],
    "target_type": "playlist",
    "media_type": "video",
    "quality": "1080p",
    "audio_format": "m4a",
    "fallback_policy": "nearest_lower",
    "preferred_container": "mp4"
}

resp = requests.post(f"{BASE_URL}/download", json=payload, timeout=120)
resp.raise_for_status()
print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

### レスポンス例（既存ファイル再利用あり）

```json
{
  "success": true,
  "accepted_count": 2,
  "queued_count": 1,
  "reused_count": 1,
  "failed_count": 0,
  "items": [
    {
      "video_id": "VIDEO_ID_001",
      "playlist_id": "PLAYLIST_ID",
      "index": 1,
      "title": "1曲目",
      "media_type": "video",
      "requested_quality": "1080p",
      "requested_audio_format": "m4a",
      "fallback_policy": "nearest_lower",
      "preferred_container": "mp4",
      "progress_key": "VIDEO_ID_001_video_1080p_m4a",
      "status": "reused",
      "downloaded": true,
      "reused": true,
      "file_path": "playlists/PLAYLIST_ID/001_VIDEO_ID_001/video_1080p_mp4/1曲目.mp4",
      "expires_at": "2026-03-19T01:00:00+00:00",
      "error_code": null,
      "message": "既存ファイルを再利用しました",
      "quality_check_pending": false
    },
    {
      "video_id": "VIDEO_ID_002",
      "playlist_id": "PLAYLIST_ID",
      "index": 2,
      "title": "2曲目",
      "media_type": "video",
      "requested_quality": "1080p",
      "requested_audio_format": "m4a",
      "fallback_policy": "nearest_lower",
      "preferred_container": "mp4",
      "progress_key": "VIDEO_ID_002_video_1080p_m4a",
      "status": "queued",
      "downloaded": false,
      "reused": false,
      "file_path": "playlists/PLAYLIST_ID/002_VIDEO_ID_002/video_1080p_mp4/2曲目.mp4",
      "expires_at": "2026-03-19T01:00:00+00:00",
      "error_code": null,
      "message": "ジョブを受け付けました",
      "quality_check_pending": true
    }
  ]
}
```

### レスポンス項目の意味

| 項目 | 説明 |
|---|---|
| `success` | 全件成功なら `true`。一部失敗があると `false` |
| `accepted_count` | 受付できた件数 |
| `queued_count` | 新規ジョブとして投入した件数 |
| `reused_count` | 既存ファイル再利用件数 |
| `failed_count` | 受付失敗件数 |
| `items` | 各 item の結果 |

#### `items[]`

| 項目 | 説明 |
|---|---|
| `video_id` | 動画 ID |
| `playlist_id` | プレイリスト ID |
| `index` | プレイリスト内位置 |
| `title` | 動画タイトル |
| `media_type` | `video` または `audio` |
| `requested_quality` | 要求画質 |
| `requested_audio_format` | 要求音声形式 |
| `fallback_policy` | fallback 方針 |
| `preferred_container` | コンテナ指定 |
| `progress_key` | `/progress` や `/files/download` に使うキー |
| `status` | `queued` / `reused` / `failed` など |
| `downloaded` | 完了済みか |
| `reused` | 既存ファイル再利用か |
| `file_path` | 保存先相対パス |
| `expires_at` | 保存期限 |
| `error_code` | エラー時コード |
| `message` | 状態メッセージ |
| `quality_check_pending` | 動画の画質確認が未完了か |

---

## 4. `GET /progress`

### 概要

1 件の progress key について進捗を返します。

### クエリパラメータ

| パラメータ | 型 | 必須 | 説明 |
|---|---|---:|---|
| `key` | string | 必須 | `/download` で返された `progress_key` |

### Python サンプル

```python
import json
import requests

BASE_URL = "http://127.0.0.1:8000"
progress_key = "VIDEO_ID_video_1080p_m4a"

resp = requests.get(
    f"{BASE_URL}/progress",
    params={"key": progress_key},
    timeout=30,
)
resp.raise_for_status()
print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

### レスポンス例

```json
{
  "success": true,
  "key": "VIDEO_ID_video_1080p_m4a",
  "video_id": "VIDEO_ID",
  "playlist_id": null,
  "index": null,
  "media_type": "video",
  "format": "mp4",
  "requested_quality": "1080p",
  "requested_audio_format": "m4a",
  "fallback_policy": "nearest_lower",
  "preferred_container": "mp4",
  "status": "completed",
  "progress_percent": 100.0,
  "downloaded_bytes": 73400320,
  "total_bytes": 73400320,
  "message": "処理が完了しました",
  "file_path": "VIDEO_ID/video_1080p_mp4/サンプル動画.mp4",
  "error_code": null,
  "quality_check_status": "completed",
  "resolved_quality": "1080p",
  "quality_exact_match": true,
  "fallback_reason": null,
  "final_container": "mp4",
  "final_audio_format": "m4a"
}
```

### レスポンス項目の意味

| 項目 | 説明 |
|---|---|
| `key` | progress key |
| `video_id` | 動画 ID |
| `playlist_id` | プレイリスト ID |
| `index` | プレイリスト内位置 |
| `media_type` | `video` / `audio` |
| `format` | 内部的な出力形式表示 |
| `requested_quality` | 要求画質 |
| `requested_audio_format` | 要求音声形式 |
| `fallback_policy` | fallback 方針 |
| `preferred_container` | コンテナ指定 |
| `status` | `queued` / `downloading` / `postprocessing` / `quality_check` / `completed` / `reused` / `failed` |
| `progress_percent` | 進捗率 |
| `downloaded_bytes` | ダウンロード済みバイト数 |
| `total_bytes` | 総バイト数 |
| `message` | 状態メッセージ |
| `file_path` | 保存先相対パス |
| `error_code` | エラー時コード |
| `quality_check_status` | 動画時の画質確認状態。`pending` / `running` / `completed` / `skipped` |
| `resolved_quality` | 実ファイルから確認できた画質 |
| `quality_exact_match` | 要求画質と一致したか |
| `fallback_reason` | lower quality に落ちた理由 |
| `final_container` | 最終コンテナ |
| `final_audio_format` | 最終音声形式 |

---

## 5. `POST /progress`

### 概要

複数の progress key をまとめて取得します。

### リクエストパラメータ

| パラメータ | 型 | 必須 | 説明 |
|---|---|---:|---|
| `keys` | array[string] | 必須 | progress key 一覧（1〜200 件） |

### Python サンプル

```python
import json
import requests

BASE_URL = "http://127.0.0.1:8000"

payload = {
    "keys": [
        "VIDEO_ID_1_audio_mp3",
        "VIDEO_ID_2_video_1080p_m4a"
    ]
}

resp = requests.post(f"{BASE_URL}/progress", json=payload, timeout=30)
resp.raise_for_status()
print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

### レスポンス例

```json
{
  "success": true,
  "items": [
    {
      "success": true,
      "key": "VIDEO_ID_1_audio_mp3",
      "video_id": "VIDEO_ID_1",
      "playlist_id": null,
      "index": null,
      "media_type": "audio",
      "format": "mp3",
      "requested_quality": null,
      "requested_audio_format": "mp3",
      "fallback_policy": "nearest_lower",
      "preferred_container": null,
      "status": "downloading",
      "progress_percent": 54.23,
      "downloaded_bytes": 12345678,
      "total_bytes": 22876543,
      "message": "ダウンロード中",
      "file_path": "VIDEO_ID_1/audio_mp3/サンプル1.mp3",
      "error_code": null,
      "quality_check_status": "skipped",
      "resolved_quality": null,
      "quality_exact_match": null,
      "fallback_reason": null,
      "final_container": null,
      "final_audio_format": "mp3"
    },
    {
      "success": false,
      "key": "UNKNOWN_KEY",
      "status": "not_found",
      "progress_percent": 0,
      "error_code": "progress_key_not_found"
    }
  ]
}
```

---

## 6. `POST /files/download`

### 概要

完了済みファイルをダウンロードします。

- `keys` が 1 件ならそのファイルをそのまま返します
- `keys` が複数なら ZIP を返します
- 対象ジョブが `completed` または `reused` でない場合は `409 file_not_ready` です

### リクエストパラメータ

| パラメータ | 型 | 必須 | 説明 |
|---|---|---:|---|
| `keys` | array[string] | 必須 | progress key 一覧 |
| `archive_name` | string | 任意 | 複数指定時の ZIP 名 |

### Python サンプル（単体）

```python
import requests

BASE_URL = "http://127.0.0.1:8000"

payload = {
    "keys": ["VIDEO_ID_audio_mp3"]
}

with requests.post(f"{BASE_URL}/files/download", json=payload, stream=True, timeout=300) as resp:
    resp.raise_for_status()
    with open("output.mp3", "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 64):
            if chunk:
                f.write(chunk)

print("saved: output.mp3")
```

### Python サンプル（複数 ZIP）

```python
import requests

BASE_URL = "http://127.0.0.1:8000"

payload = {
    "keys": [
        "VIDEO_ID_1_audio_mp3",
        "VIDEO_ID_2_video_1080p_m4a"
    ],
    "archive_name": "download_bundle.zip"
}

with requests.post(f"{BASE_URL}/files/download", json=payload, stream=True, timeout=300) as resp:
    resp.raise_for_status()
    with open("download_bundle.zip", "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 64):
            if chunk:
                f.write(chunk)

print("saved: download_bundle.zip")
```

### 補足

- レスポンスは JSON ではなく **ファイル本体**です
- 同名ファイルが ZIP 内で衝突する場合は、progress key を付与して自動リネームされます

---

## 7. `GET /server/status`

### 概要

サーバー状態をまとめて確認します。

- `yt-dlp` / `ffmpeg` / `deno` のバージョン
- ダウンロードキュー状況
- YouTube 情報取得キュー状況
- 日次上限の使用数 / 残数 / リセット時刻

### Python サンプル

```python
import json
import requests

BASE_URL = "http://127.0.0.1:8000"

resp = requests.get(f"{BASE_URL}/server/status", timeout=30)
resp.raise_for_status()
print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

### レスポンス例

```json
{
  "versions": {
    "yt_dlp": {
      "ok": true,
      "version": "2026.03.17"
    },
    "deno": {
      "ok": true,
      "version": "deno 2.2.12"
    },
    "ffmpeg": {
      "ok": true,
      "version": "ffmpeg version 7.1-static"
    }
  },
  "queue": {
    "audio_processing_count": 3,
    "video_processing_count": 2,
    "total_processing_count": 5,
    "audio_running_count": 2,
    "video_running_count": 1,
    "total_running_count": 3,
    "audio_waiting_count": 1,
    "video_waiting_count": 1,
    "total_waiting_count": 2,
    "max_concurrent_audio_jobs": 10,
    "max_concurrent_video_jobs": 3
  },
  "youtube_info_queue": {
    "youtube_info_processing_count": 2,
    "youtube_info_running_count": 1,
    "youtube_info_waiting_count": 1,
    "max_concurrent_youtube_info_jobs": 2
  },
  "limits": {
    "timezone": "Asia/Tokyo",
    "reset_at": "2026-03-19T00:00:00+09:00",
    "formats": {
      "used_today": 12,
      "limit": 300,
      "remaining": 288
    },
    "conversions": {
      "used_today": 7,
      "limit": 300,
      "remaining": 293
    }
  }
}
```

### レスポンス項目の意味

#### `versions`

| 項目 | 説明 |
|---|---|
| `ok` | コマンド実行に成功したか |
| `version` | 取得できたバージョン文字列先頭行 |

#### `queue`

| 項目 | 説明 |
|---|---|
| `audio_processing_count` | 音声ジョブ総数（running + waiting） |
| `video_processing_count` | 動画ジョブ総数（running + waiting） |
| `total_processing_count` | 全ジョブ総数 |
| `audio_running_count` | 実行中の音声ジョブ数 |
| `video_running_count` | 実行中の動画ジョブ数 |
| `total_running_count` | 実行中ジョブ総数 |
| `audio_waiting_count` | 待機中の音声ジョブ数 |
| `video_waiting_count` | 待機中の動画ジョブ数 |
| `total_waiting_count` | 待機中ジョブ総数 |
| `max_concurrent_audio_jobs` | 音声最大並列数 |
| `max_concurrent_video_jobs` | 動画最大並列数 |

#### `youtube_info_queue`

| 項目 | 説明 |
|---|---|
| `youtube_info_processing_count` | YouTube 情報取得総数（running + waiting） |
| `youtube_info_running_count` | 実行中の情報取得数 |
| `youtube_info_waiting_count` | 待機中の情報取得数 |
| `max_concurrent_youtube_info_jobs` | 情報取得最大並列数 |

#### `limits`

| 項目 | 説明 |
|---|---|
| `timezone` | 上限カウント基準タイムゾーン |
| `reset_at` | 次回リセット時刻 |
| `formats.used_today` | 本日 `/formats` で消費した回数 |
| `formats.limit` | `/formats` 上限 |
| `formats.remaining` | `/formats` 残数 |
| `conversions.used_today` | 本日新規変換で消費した件数 |
| `conversions.limit` | 変換上限 |
| `conversions.remaining` | 変換残数 |

---

## 8. `GET /cleanup/{secret_path}`

### 概要

保存期限を過ぎたファイルを削除します。

- `FILE_TTL_HOURS` より古いファイルが対象です
- 実行中ジョブに関連するファイルはスキップされます
- `CLEANUP_REQUIRE_HEADER_TOKEN=true` の場合は `X-Cleanup-Token` が必要です

### パス / ヘッダ

| 項目 | 型 | 必須 | 説明 |
|---|---|---:|---|
| `secret_path` | string | 必須 | `CLEANUP_SECRET_PATH` と一致する必要があります |
| `X-Cleanup-Token` | string | 条件付き | `CLEANUP_REQUIRE_HEADER_TOKEN=true` のとき必須 |

### Python サンプル（ヘッダ不要）

```python
import json
import requests

BASE_URL = "http://127.0.0.1:8000"
SECRET_PATH = "cleanup-change-me"

resp = requests.get(f"{BASE_URL}/cleanup/{SECRET_PATH}", timeout=120)
resp.raise_for_status()
print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

### Python サンプル（ヘッダ必要）

```python
import json
import requests

BASE_URL = "http://127.0.0.1:8000"
SECRET_PATH = "cleanup-change-me"
TOKEN = "your-token"

resp = requests.get(
    f"{BASE_URL}/cleanup/{SECRET_PATH}",
    headers={"X-Cleanup-Token": TOKEN},
    timeout=120,
)
resp.raise_for_status()
print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

### レスポンス例

```json
{
  "success": true,
  "message": "cleanup completed",
  "deleted_files_count": 2,
  "deleted_dirs_count": 1,
  "skipped_files_count": 3,
  "skipped_dirs_count": 4,
  "errors_count": 0,
  "deleted_paths": [
    "VIDEO_ID/audio_mp3/old.mp3"
  ],
  "skipped_paths": [
    "VIDEO_ID/video_1080p_mp4/current.mp4"
  ]
}
```

### レスポンス項目の意味

| 項目 | 説明 |
|---|---|
| `deleted_files_count` | 削除したファイル数 |
| `deleted_dirs_count` | 削除した空ディレクトリ数 |
| `skipped_files_count` | スキップしたファイル数 |
| `skipped_dirs_count` | スキップしたディレクトリ数 |
| `errors_count` | 削除中エラー数 |
| `deleted_paths` | 削除した相対パス一覧 |
| `skipped_paths` | スキップした相対パス一覧 |

---

## progress key の考え方

progress key は API 内部で以下のように組み立てられます。

- 動画: `{video_id}_video_{quality}_{audio_format}`
- 音声: `{video_id}_audio_{audio_format}`

例:

- `VIDEO_ID_video_1080p_m4a`
- `VIDEO_ID_audio_mp3`

このキーを使って、次の操作を行います。

- `GET /progress`
- `POST /progress`
- `POST /files/download`

---

## ステータス値の見方

| status | 説明 |
|---|---|
| `queued` | 受付済み。まだ実行待ち |
| `downloading` | ダウンロード中 |
| `postprocessing` | ffmpeg 変換 / 後処理中 |
| `quality_check` | 動画の実ファイル画質を確認中 |
| `completed` | 完了 |
| `reused` | 既存ファイルを再利用 |
| `failed` | 失敗 |

---

## 日次上限の考え方

- `POST /formats` は呼び出しごとに **`FORMATS_DAILY_LIMIT`** を 1 消費します
- `POST /download` は **新規ジョブ投入時のみ** **`CONVERSIONS_DAILY_LIMIT`** を消費します
- `reused` は **消費しません**
- リセット時刻は `LIMIT_RESET_TIMEZONE` の翌日 00:00 基準です
- 現在の使用数 / 残数 / リセット時刻は `GET /server/status` で確認できます

---

## セキュリティ運用メモ

### 方針

- **API(8000)** は直接公開しない
  - `127.0.0.1:8000` に bind
  - Nginx / Northflank などのリバースプロキシ経由で公開
- **SSH(22)** は接続元 IP を固定
- **RDP(3389)** も接続元 IP を固定
- `RDP_PASSWORD` と `CLEANUP_SECRET_PATH` は必ず変更
- cleanup を使う場合は `CLEANUP_REQUIRE_HEADER_TOKEN=true` も推奨

---

## 逆プロキシ配下で `ALLOWED_IPS` を使う場合

アプリ側の IP 制限は接続元 IP ベースです。  
そのため、リバースプロキシ越しでは `TRUST_PROXY_HEADERS=true` と `TRUSTED_PROXY_IPS` の設定が必要です。

### 例

```dotenv
TRUST_PROXY_HEADERS=true
TRUSTED_PROXY_IPS=172.17.0.1/32
```

- `TRUST_PROXY_HEADERS=true`  
  entrypoint で uvicorn に `--proxy-headers` を付与します
- `TRUSTED_PROXY_IPS`  
  `--forwarded-allow-ips` に渡されます

---

## Northflank で使う場合の補足

`NORTHFLANK_PUBLIC_BASE_URL` という ENV は残っていますが、**現行コードでは直接参照されていません**。  
重要なのは公開 URL そのものより、**プロキシ配下での接続元 IP の扱い**です。

---

## 運用メモ

- RDP は必要時だけ開放し、可能なら普段は塞ぐ
- Cookie の鮮度切れ時は RDP で YouTube 表示 → `get_youtube_cookie.py` 実行を行う
- `GET /server/status` で、キュー詰まり・上限消費・バイナリ異常を定期確認すると運用しやすい
- API を直接公開する場合でも、アプリ内 `ALLOWED_IPS` だけに頼らず、VPS 側 FW や上位プロキシでも絞るのが推奨
