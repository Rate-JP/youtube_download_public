from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, List

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    allowed_ips: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["127.0.0.1/32", "::1/128"],
        alias="ALLOWED_IPS",
    )
    trust_proxy_headers: bool = Field(default=False, alias="TRUST_PROXY_HEADERS")
    trusted_proxy_ips: Annotated[List[str], NoDecode] = Field(
        default_factory=list,
        alias="TRUSTED_PROXY_IPS",
    )

    port: int = Field(default=8000, alias="PORT")
    download_root: str = Field(default="dl", alias="DOWNLOAD_ROOT")
    request_timeout: int = Field(default=300, alias="REQUEST_TIMEOUT")
    file_ttl_hours: int = Field(default=12, alias="FILE_TTL_HOURS")

    max_duration_seconds_mp3: int = Field(default=1800, alias="MAX_DURATION_SECONDS_MP3")
    max_duration_seconds_mp4: int = Field(default=1800, alias="MAX_DURATION_SECONDS_MP4")
    max_file_size_mb_mp3: int = Field(default=512, alias="MAX_FILE_SIZE_MB_MP3")
    max_file_size_mb_mp4: int = Field(default=2048, alias="MAX_FILE_SIZE_MB_MP4")

    youtube_cookies_enabled: bool = Field(default=True, alias="YOUTUBE_COOKIES_ENABLED")
    youtube_cookie_file: str = Field(default="youtube_cookies.txt", alias="YOUTUBE_COOKIE_FILE")
    youtube_cookie_refresh_minutes: int = Field(default=30, alias="YOUTUBE_COOKIE_REFRESH_MINUTES")
    youtube_cookie_refresh_script: str = Field(default="get_youtube_cookie.py", alias="YOUTUBE_COOKIE_REFRESH_SCRIPT")
    youtube_cookie_refresh_timeout_seconds: int = Field(default=120, alias="YOUTUBE_COOKIE_REFRESH_TIMEOUT_SECONDS")

    max_concurrent_video_jobs: int = Field(default=3, alias="MAX_CONCURRENT_VIDEO_JOBS")
    max_concurrent_audio_jobs: int = Field(default=10, alias="MAX_CONCURRENT_AUDIO_JOBS")
    max_concurrent_youtube_info_jobs: int = Field(
        default=2,
        alias="MAX_CONCURRENT_YOUTUBE_INFO_JOBS",
        validation_alias=AliasChoices(
            "MAX_CONCURRENT_YOUTUBE_INFO_JOBS",
            "MAX_CONCURRENT_VIDEO_FETCH_JOBS",
        ),
        ge=1,
    )
    max_download_items_per_request: int = Field(
        default=10,
        alias="MAX_DOWNLOAD_ITEMS_PER_REQUEST",
        validation_alias=AliasChoices("MAX_DOWNLOAD_ITEMS_PER_REQUEST"),
        ge=1,
    )

    formats_daily_limit: int = Field(
        default=300,
        alias="FORMATS_DAILY_LIMIT",
        validation_alias=AliasChoices("FORMATS_DAILY_LIMIT"),
    )
    conversions_daily_limit: int = Field(
        default=300,
        alias="CONVERSIONS_DAILY_LIMIT",
        validation_alias=AliasChoices("CONVERSIONS_DAILY_LIMIT", "FILE_DOWNLOADS_DAILY_LIMIT"),
    )
    limit_reset_timezone: str = Field(default="Asia/Tokyo", alias="LIMIT_RESET_TIMEZONE")
    server_status_command_timeout_seconds: int = Field(default=5, alias="SERVER_STATUS_COMMAND_TIMEOUT_SECONDS")

    cleanup_secret_path: str = Field(default="cleanup-change-me", alias="CLEANUP_SECRET_PATH")
    progress_retention_minutes: int = Field(default=1440, alias="PROGRESS_RETENTION_MINUTES")
    lock_timeout_seconds: int = Field(default=900, alias="LOCK_TIMEOUT_SECONDS")
    playlist_max_items: int = Field(default=100, alias="PLAYLIST_MAX_ITEMS")
    playlist_continue_on_error: bool = Field(default=True, alias="PLAYLIST_CONTINUE_ON_ERROR")
    playlist_save_root: str = Field(default="dl/playlists", alias="PLAYLIST_SAVE_ROOT")
    cleanup_require_header_token: bool = Field(default=False, alias="CLEANUP_REQUIRE_HEADER_TOKEN")
    cleanup_header_token: str | None = Field(default=None, alias="CLEANUP_HEADER_TOKEN")
    northflank_public_base_url: str | None = Field(default=None, alias="NORTHFLANK_PUBLIC_BASE_URL")
    uvicorn_workers: int = Field(default=1, alias="UVICORN_WORKERS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("allowed_ips", "trusted_proxy_ips", mode="before")
    @classmethod
    def _split_csv(cls, value: str | list[str] | None) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def env_file_path(self) -> Path:
        env_file = self.model_config.get("env_file")
        if isinstance(env_file, (list, tuple)):
            env_file = env_file[0] if env_file else BASE_DIR / ".env"
        if env_file is None:
            return (BASE_DIR / ".env").resolve()
        return Path(env_file).resolve()

    def _read_env_file_values(self) -> dict[str, str]:
        path = self.env_file_path
        if not path.exists():
            return {}

        result: dict[str, str] = {}
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue

            value = value.strip()
            if value and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()

            result[key] = value
        return result

    def resolve_int_from_env(self, *env_names: str, default: int) -> tuple[int, dict[str, Any]]:
        env_file_values = self._read_env_file_values()
        env_file_found = self.env_file_path.exists()
        env_file_path = str(self.env_file_path)

        for env_name in env_names:
            raw = os.getenv(env_name)
            if raw is not None:
                return self._parse_int_env_value(
                    env_name=env_name,
                    raw=raw,
                    default=default,
                    source="process_env",
                    env_file_found=env_file_found,
                    env_file_path=env_file_path,
                )

        for env_name in env_names:
            raw = env_file_values.get(env_name)
            if raw is not None:
                return self._parse_int_env_value(
                    env_name=env_name,
                    raw=raw,
                    default=default,
                    source="env_file",
                    env_file_found=env_file_found,
                    env_file_path=env_file_path,
                )

        return default, {
            "env_name": env_names[0] if env_names else None,
            "env_raw_value": None,
            "env_parse_ok": True,
            "default_value": default,
            "resolved_from": "default",
            "env_file_found": env_file_found,
            "env_file_path": env_file_path,
        }

    def _parse_int_env_value(
        self,
        *,
        env_name: str,
        raw: str,
        default: int,
        source: str,
        env_file_found: bool,
        env_file_path: str,
    ) -> tuple[int, dict[str, Any]]:
        value = raw.strip()
        if not value:
            return default, {
                "env_name": env_name,
                "env_raw_value": raw,
                "env_parse_ok": True,
                "default_value": default,
                "resolved_from": source,
                "env_file_found": env_file_found,
                "env_file_path": env_file_path,
            }

        try:
            parsed = int(value)
            return parsed, {
                "env_name": env_name,
                "env_raw_value": raw,
                "env_parse_ok": True,
                "default_value": default,
                "resolved_from": source,
                "env_file_found": env_file_found,
                "env_file_path": env_file_path,
            }
        except ValueError:
            return default, {
                "env_name": env_name,
                "env_raw_value": raw,
                "env_parse_ok": False,
                "default_value": default,
                "resolved_from": f"{source}_invalid",
                "env_file_found": env_file_found,
                "env_file_path": env_file_path,
            }

    @property
    def download_root_path(self) -> Path:
        return (BASE_DIR / self.download_root).resolve()

    @property
    def playlist_save_root_path(self) -> Path:
        return (BASE_DIR / self.playlist_save_root).resolve()

    @property
    def cookie_file_path(self) -> Path:
        return self._safe_project_path(self.youtube_cookie_file)

    @property
    def cookie_refresh_script_path(self) -> Path:
        return self._safe_project_path(self.youtube_cookie_refresh_script)

    @property
    def asset_dir_path(self) -> Path:
        return self._safe_project_path("asset")

    @property
    def yt_dlp_path(self) -> Path:
        return self.asset_dir_path / "yt-dlp"

    @property
    def ffmpeg_path(self) -> Path:
        return self.asset_dir_path / "ffmpeg"

    @property
    def ffprobe_path(self) -> Path:
        return self.asset_dir_path / "ffprobe"

    @property
    def allowed_ip_networks(self) -> list[ipaddress._BaseNetwork]:
        return [ipaddress.ip_network(item, strict=False) for item in self.allowed_ips]

    @property
    def trusted_proxy_networks(self) -> list[ipaddress._BaseNetwork]:
        return [ipaddress.ip_network(item, strict=False) for item in self.trusted_proxy_ips]

    def _safe_project_path(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path.resolve()
        return (BASE_DIR / path).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
