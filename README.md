# YTDownload (YouTube Downloader API)

`yt-dlp` + `ffmpeg` をバックエンドに、YouTube の動画/プレイリストを **mp4 / m4a / mp3** で取得するための FastAPI サービスです。  
Docker イメージ内に **XRDP + GUI Chrome** を同梱しており、RDP でログインして YouTube にサインイン → Chrome の Cookie をエクスポートして `yt-dlp` に渡す構成にしています。

> ⚠️ 注意  
> 本ツールの利用は、YouTube 等の利用規約・法令・権利者の権利を遵守した範囲で行ってください。  
> 本リポジトリは技術検証/個人運用を想定しています。

---

## 今回の更新内容

- **`GET /server/status` を追加**し、`yt-dlp` / `ffmpeg` / `deno` のバージョン、現在の処理キュー数、日次上限の使用状況をまとめて確認できるようにしました。
- **日次上限制御を追加**し、`/formats` は `FORMATS_DAILY_LIMIT`、変換処理は `CONVERSIONS_DAILY_LIMIT` で制御できるようにしました。
- **既存ファイル再利用（`reused`）時は変換上限を消費しない**ようにしました。つまり、再ダウンロード要求でも実ファイルを再利用できる場合は `CONVERSIONS_DAILY_LIMIT` を増やしません。
- **旧 ENV 名 `FILE_DOWNLOADS_DAILY_LIMIT` を互換 alias として継続サポート**しています。
- **`LIMIT_RESET_TIMEZONE` / `SERVER_STATUS_COMMAND_TIMEOUT_SECONDS`** を追加し、日次上限リセット基準のタイムゾーンと `/server/status` のコマンド実行タイムアウトを調整できるようにしました。

---

## できること

- **/formats**: URL を解析して選択可能なフォーマット一覧を取得
- **/download**: ダウンロードジョブを投入（既存ファイルがあれば `reused` として再利用）
- **/progress**: 進捗確認
- **/files/download**: 完了ファイルを取得（複数指定時は ZIP）
- **/server/status**: バージョン情報、処理キュー数、日次上限の使用状況を取得
- **/cleanup/{secret}**: 期限切れファイルのクリーンアップ
- **Cookie 更新**:
  - Chrome DevTools (remote debugging) 経由で Cookie を取得
  - 必要に応じて `get_youtube_cookie.py` を実行して最新 Cookie を保存
  - 更新失敗時でも、状況によっては Cookie なしで継続可能

---

## 動作要件（Contabo VPS 想定）

- Ubuntu 22.04 / 24.04 系
- Docker / Docker Compose Plugin
- RDP クライアント（Windows の「リモート デスクトップ接続」等）
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

### 2) フル環境変数例（`.env`）

最初は必要なものだけ変更すれば動きますが、以下は **このリポジトリで認識している ENV をまとめた完全例** です。  
`RDP_PASSWORD`、`CLEANUP_SECRET_PATH`、`ALLOWED_IPS` などは必ず環境に合わせて変更してください。

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
| `ALLOWED_IPS` | `127.0.0.1/32,::1/128` | API の許可元 IP/CIDR（CSV） |
| `TRUST_PROXY_HEADERS` | `false` | `X-Forwarded-*` を信頼するか |
| `TRUSTED_PROXY_IPS` | 空 | 信頼するプロキシ IP/CIDR（CSV） |
| `PORT` | `8000` | API ポート |
| `DOWNLOAD_ROOT` | `dl` | ダウンロード保存先 |
| `REQUEST_TIMEOUT` | `300` | 処理タイムアウト秒 |
| `FILE_TTL_HOURS` | `12` | cleanup 対象になるまでの保存時間 |
| `MAX_DURATION_SECONDS_MP3` | `1800` | mp3 の最大長さ（秒） |
| `MAX_DURATION_SECONDS_MP4` | `1800` | mp4 の最大長さ（秒） |
| `MAX_FILE_SIZE_MB_MP3` | `512` | mp3 の最大ファイルサイズ（MB） |
| `MAX_FILE_SIZE_MB_MP4` | `2048` | mp4 の最大ファイルサイズ（MB） |
| `YOUTUBE_COOKIES_ENABLED` | `true` | Cookie 利用の有効/無効 |
| `YOUTUBE_COOKIE_FILE` | `/app/youtube_cookies.txt` | Cookie 保存先 |
| `YOUTUBE_COOKIE_REFRESH_MINUTES` | `30` | Cookie 再取得までの目安分数 |
| `YOUTUBE_COOKIE_REFRESH_SCRIPT` | `get_youtube_cookie.py` | Cookie 更新スクリプト |
| `YOUTUBE_COOKIE_REFRESH_TIMEOUT_SECONDS` | `120` | Cookie 更新スクリプトのタイムアウト |
| `MAX_CONCURRENT_VIDEO_JOBS` | `3` | 動画ジョブの最大並列数 |
| `MAX_CONCURRENT_AUDIO_JOBS` | `10` | 音声ジョブの最大並列数 |
| `FORMATS_DAILY_LIMIT` | `300` | `/formats` の1日あたり上限。`0` 以下で実質無制限 |
| `CONVERSIONS_DAILY_LIMIT` | `300` | 新規の動画/音声変換ジョブの1日あたり上限。**`reused` 時は消費しない**。旧 `FILE_DOWNLOADS_DAILY_LIMIT` も互換利用可 |
| `LIMIT_RESET_TIMEZONE` | `Asia/Tokyo` | 日次上限を何時区切りでリセットするかのタイムゾーン |
| `SERVER_STATUS_COMMAND_TIMEOUT_SECONDS` | `5` | `/server/status` で `yt-dlp` / `ffmpeg` / `deno` のバージョン確認コマンドを待つ秒数 |
| `CLEANUP_SECRET_PATH` | `cleanup-change-me` | cleanup の URL シークレット |
| `PROGRESS_RETENTION_MINUTES` | `1440` | 進捗保持分数 |
| `LOCK_TIMEOUT_SECONDS` | `900` | ロックのタイムアウト秒 |
| `PLAYLIST_MAX_ITEMS` | `100` | プレイリスト最大件数 |
| `PLAYLIST_CONTINUE_ON_ERROR` | `true` | プレイリスト途中失敗時に継続するか |
| `PLAYLIST_SAVE_ROOT` | `dl/playlists` | プレイリスト保存先 |
| `CLEANUP_REQUIRE_HEADER_TOKEN` | `false` | cleanup に追加ヘッダを要求するか |
| `CLEANUP_HEADER_TOKEN` | 空 | cleanup 用ヘッダトークン |
| `NORTHFLANK_PUBLIC_BASE_URL` | 空 | 旧 Northflank 用の互換 ENV。現行コードでは直接参照されていません |
| `UVICORN_WORKERS` | `1` | 将来用/互換用。現状 entrypoint では固定起動 |
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

### Cookie 取得スクリプトの補助 ENV

| ENV | 既定値 | 説明 |
|---|---:|---|
| `COOKIE_OUTPUT_BASE_DIR` | `/app` | 相対パス解決ベース |
| `CHROME_DEBUG_WAIT_SECONDS` | `15` | DevTools 起動待機秒 |
| `YOUTUBE_NAVIGATION_WAIT_SECONDS` | `5` | YouTube 遷移後の待機秒 |
| `COOKIE_DOMAIN_REGEX` | 空 | Cookie 抽出対象のドメイン絞り込み |
| `YOUTUBE_COOKIE_REFRESH_URL` | `https://www.youtube.com/` | Cookie 更新時に遷移する URL |

---

## 起動例（Docker）

**推奨:** API はローカルバインド（`127.0.0.1:8000`）し、外部公開は Nginx / Northflank などのプロキシ越しに行います。  
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
4. ログイン情報は `youtube_login` を bind mount しているため、**コンテナ再起動後も Chrome プロファイルとして保持されます**
5. ただし、**再起動後も運用上は一度リモートデスクトップへアクセスし、YouTube に遷移した状態で `get_youtube_cookie.py` を手動実行する前提**です
6. 手動実行例:

```bash
python /app/get_youtube_cookie.py
```

> `youtube_login` と `youtube_cookies.txt` を bind mount していれば、ログイン情報や出力 Cookie 自体はホスト側に保存されます。  
> ただし Cookie の鮮度や Chrome 側の状態によっては、再起動後に **RDP 接続 → YouTube 表示 → `get_youtube_cookie.py` 手動実行** が必要です。

---

## API エンドポイント

起動後、Swagger UI は以下です。

- `http://127.0.0.1:8000/docs`

### Health
- `GET /healthz`

### Formats
- `POST /formats`

### Download
- `POST /download`

### Progress
- `GET /progress?key=...`
- `POST /progress`

### Files download
- `POST /files/download`

### Server status
- `GET /server/status`

### Cleanup
- `GET /cleanup/{secret_path}`

---

## 日次上限と `reused` の扱い

- `POST /formats` は呼び出しごとに **`FORMATS_DAILY_LIMIT`** を 1 消費します。
- `POST /download` は **実際に新規ジョブを投入したときだけ** **`CONVERSIONS_DAILY_LIMIT`** を消費します。
- 同じ動画・同じ形式ですでにファイルが存在する場合は **`status=reused`** で返し、**変換上限は消費しません**。
- すでに同じ progress key のジョブが実行中で、そのジョブを参照した場合も新規変換としては数えません。
- 上限のリセット時刻は **`LIMIT_RESET_TIMEZONE` の翌日 00:00** 基準です。
- 現在の使用数・残数・リセット時刻は **`GET /server/status`** で確認できます。

---

## Python サンプル

以下は `requests` を使った例です。

### 1) healthz

```python
import requests

BASE_URL = "http://127.0.0.1:8000"

resp = requests.get(f"{BASE_URL}/healthz", timeout=30)
resp.raise_for_status()
print(resp.json())
```

### 2) server/status

```python
import requests
import json

BASE_URL = "http://127.0.0.1:8000"

resp = requests.get(f"{BASE_URL}/server/status", timeout=30)
resp.raise_for_status()
print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

### 3) formats

```python
import requests
import json

BASE_URL = "http://127.0.0.1:8000"

payload = {
    "url": "https://www.youtube.com/watch?v=VIDEO_ID",
    "target_type": "auto"
}

resp = requests.post(f"{BASE_URL}/formats", json=payload, timeout=120)
resp.raise_for_status()

data = resp.json()
print(json.dumps(data, ensure_ascii=False, indent=2))
```

### 4) download

```python
import requests
import json

BASE_URL = "http://127.0.0.1:8000"

payload = {
    "items": [
        {
            "url": "https://www.youtube.com/watch?v=VIDEO_ID",
            "target_type": "video",
            "format": "mp3"
        }
    ]
}

resp = requests.post(f"{BASE_URL}/download", json=payload, timeout=120)
resp.raise_for_status()

data = resp.json()
print(json.dumps(data, ensure_ascii=False, indent=2))

progress_key = data["items"][0]["progress_key"]
print("progress_key =", progress_key)

# 既存ファイル再利用時は items[0]["status"] == "reused" になり、
# CONVERSIONS_DAILY_LIMIT は消費されません。
```

### 5) progress（単体）

```python
import requests
import json

BASE_URL = "http://127.0.0.1:8000"
progress_key = "VIDEO_ID:mp3:audio"

resp = requests.get(
    f"{BASE_URL}/progress",
    params={"key": progress_key},
    timeout=30,
)
resp.raise_for_status()

print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

### 6) progress（複数）

```python
import requests
import json

BASE_URL = "http://127.0.0.1:8000"

payload = {
    "keys": [
        "VIDEO_ID_1:mp3:audio",
        "VIDEO_ID_2:mp4:398+140"
    ]
}

resp = requests.post(f"{BASE_URL}/progress", json=payload, timeout=30)
resp.raise_for_status()

print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

### 7) files/download（単体保存）

```python
import requests

BASE_URL = "http://127.0.0.1:8000"

payload = {
    "keys": ["VIDEO_ID:mp3:audio"]
}

with requests.post(f"{BASE_URL}/files/download", json=payload, stream=True, timeout=300) as resp:
    resp.raise_for_status()
    with open("output.mp3", "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 64):
            if chunk:
                f.write(chunk)

print("saved: output.mp3")
```

### 8) files/download（複数を ZIP で保存）

```python
import requests

BASE_URL = "http://127.0.0.1:8000"

payload = {
    "keys": [
        "VIDEO_ID_1:mp3:audio",
        "VIDEO_ID_2:mp4:398+140"
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

### 9) cleanup

```python
import requests
import json

BASE_URL = "http://127.0.0.1:8000"
SECRET_PATH = "cleanup-change-me"

resp = requests.get(f"{BASE_URL}/cleanup/{SECRET_PATH}", timeout=120)
resp.raise_for_status()

print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

### 10) cleanup（ヘッダトークン必須の場合）

```python
import requests
import json

BASE_URL = "http://127.0.0.1:8000"
SECRET_PATH = "cleanup-change-me"
TOKEN = "your-token"

headers = {
    "X-Cleanup-Token": TOKEN
}

resp = requests.get(
    f"{BASE_URL}/cleanup/{SECRET_PATH}",
    headers=headers,
    timeout=120,
)
resp.raise_for_status()

print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
```

---

## セキュリティ（Contabo VPS 運用）

### 方針

- **API(8000)** は直接公開しない
  - `127.0.0.1:8000` に bind
  - Nginx / Northflank などのリバースプロキシ経由で公開
- **SSH(22)** は接続元 IP を固定
- **RDP(3389)** も接続元 IP を固定
- `RDP_PASSWORD` と `CLEANUP_SECRET_PATH` は必ず変更
- cleanup を使う場合は `CLEANUP_REQUIRE_HEADER_TOKEN=true` も推奨

> ⚠️ iptables の設定ミスで SSH から締め出される可能性があります。  
> Contabo のコンソールに入れる状態で作業するか、SSH セッションを複数開いたまま適用してください。

---

## netfilter-persistent インストール

```bash
ALLOW_IP="XXX"
EXT_IF="$(ip route get 1.1.1.1 | awk '{print $5; exit}')"
echo "$ALLOW_IP / $EXT_IF"
sudo apt update && sudo apt install -y iptables-persistent
sudo sh -c 'iptables-save > /etc/iptables/rules.v4'
sudo sh -c 'ip6tables-save > /etc/iptables/rules.v6'
sudo systemctl enable netfilter-persistent
sudo systemctl restart netfilter-persistent
sudo systemctl status netfilter-persistent --no-pager
```

> `iptables-persistent` 導入後、`netfilter-persistent` サービスとして永続化されます。

---

## SSH 22 の IP 制限

以下は **SSH(22) を `ALLOW_IP` のみ許可**する例です。

```bash
ALLOW_IP="XXX"  # 例: 203.0.113.10/32

sudo iptables -C INPUT -i lo -j ACCEPT 2>/dev/null || sudo iptables -A INPUT -i lo -j ACCEPT
sudo iptables -C INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || sudo iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

sudo iptables -C INPUT -p tcp --dport 22 -s "$ALLOW_IP" -j ACCEPT 2>/dev/null || sudo iptables -A INPUT -p tcp --dport 22 -s "$ALLOW_IP" -j ACCEPT
sudo iptables -C INPUT -p tcp --dport 22 -j DROP 2>/dev/null || sudo iptables -A INPUT -p tcp --dport 22 -j DROP
```

---

## RDP 3389 の IP 制限（Docker 公開ポート）

Docker で `-p 3389:3389` を使う場合、`DOCKER-USER` チェーンで制御するのが安全です。

```bash
ALLOW_IP="XXX"  # 例: 203.0.113.10/32
EXT_IF="$(ip route get 1.1.1.1 | awk '{print $5; exit}')"

sudo iptables -C DOCKER-USER -i "$EXT_IF" -p tcp --dport 3389 -s "$ALLOW_IP" -j ACCEPT 2>/dev/null || sudo iptables -I DOCKER-USER 1 -i "$EXT_IF" -p tcp --dport 3389 -s "$ALLOW_IP" -j ACCEPT
sudo iptables -C DOCKER-USER -i "$EXT_IF" -p tcp --dport 3389 -j DROP 2>/dev/null || sudo iptables -I DOCKER-USER 2 -i "$EXT_IF" -p tcp --dport 3389 -j DROP
```

---

## ルール保存

```bash
sudo sh -c 'iptables-save > /etc/iptables/rules.v4'
sudo sh -c 'ip6tables-save > /etc/iptables/rules.v6'
sudo systemctl enable netfilter-persistent
sudo systemctl restart netfilter-persistent
sudo systemctl status netfilter-persistent --no-pager
```

確認:

```bash
sudo iptables -S INPUT | sed -n '1,120p'
sudo iptables -S DOCKER-USER | sed -n '1,120p'
```

---

## 逆プロキシ配下（Nginx など）で `ALLOWED_IPS` を使う場合

アプリ側の IP 制限は `request.client.host` ベースです。  
そのため、リバースプロキシ越しでは **プロキシ IP が見えてしまう**ため、`X-Forwarded-For` を uvicorn に正しく反映させる必要があります。

### 例

```dotenv
TRUST_PROXY_HEADERS=true
TRUSTED_PROXY_IPS=172.17.0.1/32
```

- `TRUST_PROXY_HEADERS=true`
  - entrypoint で uvicorn に `--proxy-headers` を付与
- `TRUSTED_PROXY_IPS`
  - `--forwarded-allow-ips` に渡される値
  - **実際のプロキシ IP / CIDR を設定してください**

> `TRUSTED_PROXY_IPS` は IP/CIDR の CSV です。  
> ワイルドカード文字列はこの実装では使えません。

---

## Northflank で実装する場合の補足

以前の実装名残として `NORTHFLANK_PUBLIC_BASE_URL` という ENV が残っていますが、**現行コードでは直接参照されていません**。  
そのため、Northflank で動かす場合に重要なのは `NORTHFLANK_PUBLIC_BASE_URL` そのものより、**プロキシ配下としての IP 取り扱い**です。

### Northflank 配下で考える点

1. **Northflank の外側公開 URL**
   - 公開 URL 自体は Northflank 側で管理される
2. **アプリが見る接続元 IP**
   - アプリからは Northflank プロキシの IP に見える場合がある
3. **`ALLOWED_IPS` をアプリ側で厳密に使いたい場合**
   - `TRUST_PROXY_HEADERS=true`
   - `TRUSTED_PROXY_IPS=<Northflank 側の実際のプロキシ IP/CIDR>`
4. **Northflank 側のプロキシ IP を固定で把握しにくい場合**
   - アプリ側の `ALLOWED_IPS` より、Northflank 側の公開制御 / Access 制御 / 上位 WAF 側で絞るほうが安全なことがあります

### 例

```dotenv
TRUST_PROXY_HEADERS=true
TRUSTED_PROXY_IPS=10.0.0.0/8,172.16.0.0/12
NORTHFLANK_PUBLIC_BASE_URL=https://example.your-domain.com
```

> ただし `NORTHFLANK_PUBLIC_BASE_URL` は現状コード未使用です。  
> 互換・メモ用途として残っている値なので、設定しても `ALLOWED_IPS` の挙動には直接影響しません。

---

## 運用メモ

- RDP は必要時だけ開放し、可能なら普段は塞ぐ
- `youtube_login` を bind mount していても、再起動後は Chrome / YouTube / Cookie の状態確認を推奨
- `get_youtube_cookie.py` の手動実行手順は運用手順書化しておくと安全
- API を直接インターネットへ公開する場合でも、アプリ内 `ALLOWED_IPS` だけに頼らず、VPS 側 FW や上位プロキシ側でも絞るのが推奨
