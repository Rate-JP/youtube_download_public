from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import BASE_DIR, Settings
from app.core.exceptions import AppError
from app.services.job_manager import JobManager
from app.services.ytdlp_service import YtDlpService
from app.utils.files import ensure_directory


@dataclass
class DailyCounter:
    name: str
    count: int = 0
    day_key: str = ""


class RuntimeStatusService:
    def __init__(self, settings: Settings, ytdlp_service: YtDlpService, job_manager: JobManager):
        self.settings = settings
        self.ytdlp_service = ytdlp_service
        self.job_manager = job_manager
        self._tz = ZoneInfo(settings.limit_reset_timezone)
        self._lock = threading.Lock()
        self._state_path = (BASE_DIR / "tmp" / "runtime_status_counters.json").resolve()
        ensure_directory(self._state_path.parent)
        self._formats_counter = DailyCounter(name="formats")
        self._conversions_counter = DailyCounter(name="conversions")
        self._load_state()

    def _now(self) -> datetime:
        return datetime.now(self._tz)

    def _current_day_key(self) -> str:
        return self._now().date().isoformat()

    def _next_reset_at(self) -> datetime:
        now = self._now()
        tomorrow = (now + timedelta(days=1)).date()
        return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=self._tz)

    def _reset_if_needed(self, counter: DailyCounter) -> None:
        day_key = self._current_day_key()
        if counter.day_key != day_key:
            counter.day_key = day_key
            counter.count = 0

    def _load_state(self) -> None:
        try:
            if not self._state_path.exists():
                return

            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            formats = data.get("formats", {})
            conversions = data.get("conversions", data.get("file_downloads", {}))

            self._formats_counter.count = int(formats.get("count", 0) or 0)
            self._formats_counter.day_key = str(formats.get("day_key", "") or "")
            self._conversions_counter.count = int(conversions.get("count", 0) or 0)
            self._conversions_counter.day_key = str(conversions.get("day_key", "") or "")
        except Exception:
            self._formats_counter = DailyCounter(name="formats")
            self._conversions_counter = DailyCounter(name="conversions")

    def _save_state(self) -> None:
        payload = {
            "formats": {
                "count": self._formats_counter.count,
                "day_key": self._formats_counter.day_key,
            },
            "conversions": {
                "count": self._conversions_counter.count,
                "day_key": self._conversions_counter.day_key,
            },
        }
        tmp_path = self._state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self._state_path)

    def _resolve_formats_limit(self) -> int:
        resolved, _meta = self.settings.resolve_int_from_env(
            "FORMATS_DAILY_LIMIT",
            default=self.settings.formats_daily_limit,
        )
        return resolved

    def _resolve_conversions_limit(self) -> int:
        resolved, _meta = self.settings.resolve_int_from_env(
            "CONVERSIONS_DAILY_LIMIT",
            "FILE_DOWNLOADS_DAILY_LIMIT",
            default=self.settings.conversions_daily_limit,
        )
        return resolved

    def _counter_snapshot(self, counter: DailyCounter, limit: int) -> dict[str, Any]:
        remaining = -1 if limit <= 0 else max(limit - counter.count, 0)
        return {
            "used_today": counter.count,
            "limit": limit,
            "remaining": remaining,
        }

    def _limit_error_extras(
        self,
        *,
        limit: int,
        used_today: int,
        requested: int | None = None,
    ) -> dict[str, Any]:
        extras: dict[str, Any] = {
            "limit": limit,
            "used_today": used_today,
            "reset_at": self._next_reset_at().isoformat(),
            "timezone": self.settings.limit_reset_timezone,
        }
        if requested is not None:
            extras["requested"] = requested
        return extras

    def _enforce_and_increment(
        self,
        *,
        counter: DailyCounter,
        limit: int,
        error_code: str,
        message: str,
        increment_by: int = 1,
    ) -> dict[str, Any]:
        if increment_by <= 0:
            raise ValueError("increment_by must be >= 1")

        with self._lock:
            self._reset_if_needed(counter)

            if limit > 0 and counter.count + increment_by > limit:
                raise AppError(
                    status_code=429,
                    error_code=error_code,
                    message=message,
                    extras=self._limit_error_extras(
                        limit=limit,
                        used_today=counter.count,
                        requested=increment_by,
                    ),
                )

            counter.count += increment_by
            self._save_state()
            snapshot = self._counter_snapshot(counter, limit)
            snapshot["incremented_by"] = increment_by
            return snapshot

    def enforce_and_increment_formats(self) -> dict[str, Any]:
        limit = self._resolve_formats_limit()
        return self._enforce_and_increment(
            counter=self._formats_counter,
            limit=limit,
            error_code="formats_daily_limit_exceeded",
            message="/formats の1日あたりの呼び出し上限に達しました",
        )

    def enforce_and_increment_conversions(self, increment_by: int = 1) -> dict[str, Any]:
        limit = self._resolve_conversions_limit()
        return self._enforce_and_increment(
            counter=self._conversions_counter,
            limit=limit,
            error_code="conversions_daily_limit_exceeded",
            message="動画/音声変換の1日あたりの上限に達しました",
            increment_by=increment_by,
        )

    def get_limits_snapshot(self) -> dict[str, Any]:
        formats_limit = self._resolve_formats_limit()
        conversions_limit = self._resolve_conversions_limit()
        with self._lock:
            self._reset_if_needed(self._formats_counter)
            self._reset_if_needed(self._conversions_counter)
            self._save_state()
            return {
                "timezone": self.settings.limit_reset_timezone,
                "reset_at": self._next_reset_at().isoformat(),
                "formats": self._counter_snapshot(self._formats_counter, formats_limit),
                "conversions": self._counter_snapshot(self._conversions_counter, conversions_limit),
            }

    def _run_version_command(self, cmd: list[str]) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.settings.server_status_command_timeout_seconds,
                check=False,
            )
            raw = (completed.stdout or completed.stderr or "").strip()
            first_line = raw.splitlines()[0].strip() if raw else None
            return {
                "ok": completed.returncode == 0,
                "version": first_line,
            }
        except Exception as exc:  # pragma: no cover
            return {
                "ok": False,
                "version": f"error: {exc}",
            }

    def get_versions(self) -> dict[str, dict[str, Any]]:
        deno_binary = os.getenv("DENO_BINARY", "deno")
        return {
            "yt_dlp": self._run_version_command([str(self.ytdlp_service.yt_dlp_path), "--version"]),
            "deno": self._run_version_command([deno_binary, "--version"]),
            "ffmpeg": self._run_version_command([str(self.ytdlp_service.ffmpeg_path), "-version"]),
        }

    def get_server_status(self) -> dict[str, Any]:
        return {
            "versions": self.get_versions(),
            "queue": self.job_manager.get_active_queue_counts(),
            "youtube_info_queue": self.ytdlp_service.get_youtube_info_queue_counts(),
            "limits": self.get_limits_snapshot(),
        }
