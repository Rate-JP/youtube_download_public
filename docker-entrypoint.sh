#!/usr/bin/env bash
set -Eeuo pipefail

RDP_USER="${RDP_USER:-appuser}"
RDP_PASSWORD="${RDP_PASSWORD:-change-me}"
APP_MODULE="${APP_MODULE:-app.main:app}"
APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-${PORT:-8000}}"
APP_CMD="${APP_CMD:-}"
CHROME_BINARY="${CHROME_BINARY:-/usr/bin/google-chrome}"
DENO_BINARY="${DENO_BINARY:-/usr/local/bin/deno}"
CHROME_REMOTE_DEBUGGING_HOST="${CHROME_REMOTE_DEBUGGING_HOST:-127.0.0.1}"
CHROME_REMOTE_DEBUGGING_PORT="${CHROME_REMOTE_DEBUGGING_PORT:-9222}"
YOUTUBE_LOGIN_BASE_DIR="${YOUTUBE_LOGIN_BASE_DIR:-/app/youtube_login}"
YOUTUBE_PROFILE_DIR="${YOUTUBE_PROFILE_DIR:-${YOUTUBE_LOGIN_BASE_DIR}/chrome_profile}"
YOUTUBE_COOKIE_FILE="${YOUTUBE_COOKIE_FILE:-/app/youtube_cookies.txt}"
RDP_START_URL="${RDP_START_URL:-https://www.youtube.com/}"

if ! id -u "${RDP_USER}" >/dev/null 2>&1; then
  echo "[entrypoint] user not found: ${RDP_USER}" >&2
  exit 1
fi

if [[ ! -x "${CHROME_BINARY}" ]]; then
  echo "[entrypoint] chrome binary not found: ${CHROME_BINARY}" >&2
  exit 1
fi

if [[ ! -x "${DENO_BINARY}" ]]; then
  echo "[entrypoint] deno binary not found: ${DENO_BINARY}" >&2
  exit 1
fi

if [[ ! -x /app/asset/yt-dlp ]]; then
  echo "[entrypoint] yt-dlp not found: /app/asset/yt-dlp" >&2
  exit 1
fi

if [[ ! -x /app/asset/ffmpeg ]]; then
  echo "[entrypoint] ffmpeg not found: /app/asset/ffmpeg" >&2
  exit 1
fi

mkdir -p \
  /run/dbus \
  /var/run/xrdp \
  /var/log/supervisor \
  /var/lib/dbus \
  "${YOUTUBE_LOGIN_BASE_DIR}" \
  "${YOUTUBE_PROFILE_DIR}" \
  "$(dirname "${YOUTUBE_COOKIE_FILE}")" \
  "/home/${RDP_USER}/.config/autostart" \
  "/home/${RDP_USER}/.local/bin"

chown -R "${RDP_USER}:${RDP_USER}" \
  "${YOUTUBE_LOGIN_BASE_DIR}" \
  "/home/${RDP_USER}/.config" \
  "/home/${RDP_USER}/.local"

chmod 700 "${YOUTUBE_PROFILE_DIR}"

echo "${RDP_USER}:${RDP_PASSWORD}" | chpasswd

cat > /etc/X11/Xwrapper.config <<'XWRAP'
allowed_users=anybody
needs_root_rights=yes
XWRAP

cat > /etc/xrdp/startwm.sh <<XRDPWM
#!/bin/sh
export XDG_SESSION_DESKTOP=xfce
export XDG_CURRENT_DESKTOP=XFCE
export DESKTOP_SESSION=xfce
unset DBUS_SESSION_BUS_ADDRESS
unset XDG_RUNTIME_DIR
if [ -x "/home/${RDP_USER}/.local/bin/start-youtube-chrome.sh" ]; then
  (
    sleep 2
    /home/${RDP_USER}/.local/bin/start-youtube-chrome.sh >/tmp/start-youtube-chrome.log 2>&1 &
  ) &
fi
exec startxfce4
XRDPWM
chmod +x /etc/xrdp/startwm.sh

cat > "/home/${RDP_USER}/.local/bin/start-youtube-chrome.sh" <<EOS
#!/usr/bin/env bash
set -Eeuo pipefail
PROFILE_DIR="${YOUTUBE_PROFILE_DIR}"
CHROME_BINARY="${CHROME_BINARY}"
REMOTE_HOST="${CHROME_REMOTE_DEBUGGING_HOST}"
REMOTE_PORT="${CHROME_REMOTE_DEBUGGING_PORT}"
START_URL="${RDP_START_URL}"

mkdir -p "\${PROFILE_DIR}"

if pgrep -u "\$(id -un)" -f -- "--user-data-dir=\${PROFILE_DIR}" >/dev/null 2>&1; then
  exit 0
fi

rm -f "\${PROFILE_DIR}/SingletonLock" "\${PROFILE_DIR}/SingletonCookie" "\${PROFILE_DIR}/SingletonSocket" 2>/dev/null || true

exec "\${CHROME_BINARY}" \
  --user-data-dir="\${PROFILE_DIR}" \
  --password-store=basic \
  --remote-debugging-address="\${REMOTE_HOST}" \
  --remote-debugging-port="\${REMOTE_PORT}" \
  --remote-allow-origins="http://\${REMOTE_HOST}:\${REMOTE_PORT}" \
  --no-first-run \
  --no-default-browser-check \
  --disable-dev-shm-usage \
  --no-sandbox \
  --disable-setuid-sandbox \
  --new-window "\${START_URL}"
EOS
chmod 755 "/home/${RDP_USER}/.local/bin/start-youtube-chrome.sh"
chown "${RDP_USER}:${RDP_USER}" "/home/${RDP_USER}/.local/bin/start-youtube-chrome.sh"

cat > "/home/${RDP_USER}/.config/autostart/youtube-chrome.desktop" <<EOS
[Desktop Entry]
Type=Application
Version=1.0
Name=YouTube Chrome
Comment=Start Google Chrome for manual YouTube login
Exec=/home/${RDP_USER}/.local/bin/start-youtube-chrome.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOS
chown "${RDP_USER}:${RDP_USER}" "/home/${RDP_USER}/.config/autostart/youtube-chrome.desktop"
chmod 644 "/home/${RDP_USER}/.config/autostart/youtube-chrome.desktop"

cat > /usr/local/bin/start-app.sh <<'EOS'
#!/usr/bin/env bash
set -Eeuo pipefail

APP_PORT="${APP_PORT:-${PORT:-8000}}"
FORWARDED_ARGS=()

if [[ "${TRUST_PROXY_HEADERS:-false}" == "true" ]]; then
  if [[ -n "${TRUSTED_PROXY_IPS:-}" ]]; then
    FORWARDED_ARGS=(--proxy-headers --forwarded-allow-ips "${TRUSTED_PROXY_IPS}")
  else
    echo "[app] WARN: TRUST_PROXY_HEADERS=true but TRUSTED_PROXY_IPS is empty. Proxy headers will not be enabled." >&2
  fi
fi

if [[ -n "${APP_CMD:-}" ]]; then
  exec bash -lc "${APP_CMD}"
fi

if [[ -d /app/app ]]; then
  exec uvicorn "${APP_MODULE:-app.main:app}" \
    --host "${APP_HOST:-0.0.0.0}" \
    --port "${APP_PORT}" \
    "${FORWARDED_ARGS[@]}"
fi

echo "[app] /app/app not found, API startup skipped"
exec tail -f /dev/null
EOS
chmod 755 /usr/local/bin/start-app.sh

cat > /etc/supervisor/conf.d/ydl.conf <<'EOS'
[supervisord]
nodaemon=true
user=root
logfile=/var/log/supervisor/supervisord.log
pidfile=/var/run/supervisord.pid

[program:xrdp-sesman]
command=/usr/sbin/xrdp-sesman --nodaemon
priority=10
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0

[program:xrdp]
command=/usr/sbin/xrdp --nodaemon
priority=20
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0

[program:app]
command=/usr/local/bin/start-app.sh
priority=30
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
EOS

dbus-uuidgen --ensure=/etc/machine-id
dbus-uuidgen --ensure=/var/lib/dbus/machine-id
if [[ ! -S /run/dbus/system_bus_socket ]]; then
  mkdir -p /run/dbus
  dbus-daemon --system --fork
fi

masked_password="(empty)"
if [[ -n "${RDP_PASSWORD}" ]]; then
  masked_password="********"
fi

echo "[entrypoint] RDP user              : ${RDP_USER}"
echo "[entrypoint] RDP password          : ${masked_password}"
echo "[entrypoint] Chrome binary         : ${CHROME_BINARY}"
echo "[entrypoint] Chrome version        : $(${CHROME_BINARY} --version 2>/dev/null || true)"
echo "[entrypoint] Deno binary           : ${DENO_BINARY}"
echo "[entrypoint] Deno version          : $(${DENO_BINARY} --version 2>/dev/null | head -n 1 || true)"
echo "[entrypoint] yt-dlp version        : $(/app/asset/yt-dlp --version 2>/dev/null || true)"
echo "[entrypoint] ffmpeg version        : $(/app/asset/ffmpeg -version 2>/dev/null | head -n 1 || true)"
echo "[entrypoint] Chrome profile        : ${YOUTUBE_PROFILE_DIR}"
echo "[entrypoint] Cookie export target  : ${YOUTUBE_COOKIE_FILE}"
echo "[entrypoint] Remote debugging      : ${CHROME_REMOTE_DEBUGGING_HOST}:${CHROME_REMOTE_DEBUGGING_PORT}"
echo "[entrypoint] API                   : ${APP_HOST}:${APP_PORT} (${APP_CMD:-${APP_MODULE}})"
echo "[entrypoint] RDP usage             : connect with Windows Remote Desktop to port 3389 and log in as ${RDP_USER}"

echo "[entrypoint] starting supervisord"
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/ydl.conf
