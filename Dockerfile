FROM python:3.12-slim-bookworm

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG DENO_VERSION=2.2.12

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/usr/local/bin:${PATH}" \
    DENO_BINARY=/usr/local/bin/deno \
    APP_MODULE=app.main:app \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    CHROME_BINARY=/usr/bin/google-chrome \
    CHROME_REMOTE_DEBUGGING_HOST=127.0.0.1 \
    CHROME_REMOTE_DEBUGGING_PORT=9222 \
    YOUTUBE_LOGIN_BASE_DIR=/app/youtube_login \
    YOUTUBE_PROFILE_DIR=/app/youtube_login/chrome_profile \
    YOUTUBE_COOKIE_FILE=/app/youtube_cookies.txt \
    RDP_USER=appuser \
    RDP_PASSWORD=change-me \
    RDP_START_URL=https://www.youtube.com/

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        dbus \
        dbus-x11 \
        fonts-liberation \
        jq \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libc6 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libexpat1 \
        libgbm1 \
        libgcc-s1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libstdc++6 \
        libu2f-udev \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxcursor1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxi6 \
        libxrandr2 \
        libxrender1 \
        libxss1 \
        libxtst6 \
        procps \
        python3 \
        supervisor \
        unzip \
        wget \
        xauth \
        xdg-utils \
        xz-utils \
        xfce4 \
        xfce4-terminal \
        xorg \
        xorgxrdp \
        xrdp \
    && wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y /tmp/google-chrome.deb \
    && rm -f /tmp/google-chrome.deb \
    && curl -fsSL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/deno \
    && rm -f /tmp/deno.zip \
    && deno --version \
    && google-chrome --version \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt

RUN groupadd --system appuser \
    && useradd --system --create-home --gid appuser --shell /bin/bash appuser \
    && mkdir -p \
        /app/asset \
        /app/dl \
        /app/dl/playlists \
        /app/logs \
        /app/tmp \
        /app/youtube_login/chrome_profile \
        /var/log/supervisor \
        /var/run/xrdp \
        /run/dbus \
    && chown -R appuser:appuser /home/appuser /app/youtube_login

RUN curl -L "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp" -o /app/asset/yt-dlp \
    && chmod +x /app/asset/yt-dlp \
    && curl -L "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" -o /tmp/ffmpeg.tar.xz \
    && mkdir -p /tmp/ffmpeg-extract \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg-extract --strip-components=1 \
    && cp /tmp/ffmpeg-extract/ffmpeg /app/asset/ffmpeg \
    && chmod +x /app/asset/ffmpeg \
    && rm -rf /tmp/ffmpeg.tar.xz /tmp/ffmpeg-extract \
    && /app/asset/yt-dlp --version \
    && /app/asset/ffmpeg -version | head -n 1

COPY app /app/app
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
COPY get_youtube_cookie.py /app/get_youtube_cookie.py

RUN sed -i 's/\r$//' /app/docker-entrypoint.sh /app/get_youtube_cookie.py
RUN python - <<'PY'
from pathlib import Path
for path_str in ["/app/docker-entrypoint.sh", "/app/get_youtube_cookie.py"]:
    p = Path(path_str)
    data = p.read_bytes()
    if data.startswith(b'\xef\xbb\xbf'):
        p.write_bytes(data[3:])
PY
RUN chmod +x /app/docker-entrypoint.sh /app/get_youtube_cookie.py \
    && head -n 1 /app/docker-entrypoint.sh \
    && bash -n /app/docker-entrypoint.sh

EXPOSE 8000 3389
ENTRYPOINT ["/app/docker-entrypoint.sh"]
