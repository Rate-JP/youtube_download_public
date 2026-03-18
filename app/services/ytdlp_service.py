from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.core.config import BASE_DIR, Settings
from app.core.exceptions import AppError
from app.models.schemas import DownloadItemRequest
from app.utils.files import ensure_directory, sanitize_component, sanitize_filename
from app.utils.platform import resolve_binary

logger = logging.getLogger(__name__)

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

PROGRESS_LINE_RE = re.compile(
    r"^(?P<downloaded>[^|]*)\|(?P<total>[^|]*)\|(?P<estimated>[^|]*)\|(?P<percent>[^|]*)\|(?P<status>.*)$"
)


@dataclass(slots=True)
class VideoContext:
    video_id: str
    playlist_id: str | None
    index: int | None
    title: str
    sanitized_title: str
    duration_seconds: int | None
    thumbnail_url: str | None
    uploader: str | None
    upload_date: str | None
    source_url: str
    resource_type: str
    raw_info: dict[str, Any]


class CookieManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[bool] | None = None
        self._last_warning: str | None = None

    @property
    def last_warning(self) -> str | None:
        return self._last_warning

    def _cookie_file_exists(self) -> bool:
        return self.settings.cookie_file_path.exists() and self.settings.cookie_file_path.is_file()

    def _cookie_file_is_valid(self) -> bool:
        path = self.settings.cookie_file_path
        if not path.exists() or not path.is_file():
            return False
        try:
            if path.stat().st_size <= 0:
                return False
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                first_line = f.readline().strip()
            return first_line.startswith("# Netscape HTTP Cookie File")
        except Exception:
            return False

    def _cookie_file_age_minutes(self) -> float | None:
        path = self.settings.cookie_file_path
        if not path.exists():
            return None
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            return (datetime.now(UTC) - mtime).total_seconds() / 60.0
        except Exception:
            return None

    def _cookie_file_is_fresh(self) -> bool:
        age = self._cookie_file_age_minutes()
        if age is None:
            return False
        return age <= self.settings.youtube_cookie_refresh_minutes

    def _cookie_file_is_ready(self) -> bool:
        return self._cookie_file_is_valid() and self._cookie_file_is_fresh()

    async def ensure_ready(self, *, force_refresh: bool = False) -> bool:
        if not self.settings.youtube_cookies_enabled:
            self._last_warning = "youtube cookies disabled by configuration"
            return False

        # まず軽くチェック
        if not force_refresh and self._cookie_file_is_ready():
            self._last_warning = None
            return True

        # ここから先は、同時に複数リクエストが来ても
        # refresh task を 1 本だけ作る
        async with self._lock:
            # 他リクエストが先に更新済みかもしれないので再チェック
            if not force_refresh and self._cookie_file_is_ready():
                self._last_warning = None
                return True

            refresh_task = self._refresh_task
            if refresh_task is None or refresh_task.done():
                refresh_task = asyncio.create_task(self._refresh_cookie_file())
                self._refresh_task = refresh_task

        # lock 外で待つ。他ユーザも同じ task を await する
        wait_timeout = self.settings.youtube_cookie_refresh_timeout_seconds + 30
        try:
            ok = await asyncio.wait_for(refresh_task, timeout=wait_timeout)
        except asyncio.TimeoutError:
            self._last_warning = "cookie refresh waiter timed out; continue without cookies"
            logger.warning(self._last_warning)
            ok = False

        # task 完了後の後始末
        async with self._lock:
            if self._refresh_task is refresh_task and refresh_task.done():
                self._refresh_task = None

        # refresh 後に再チェック
        if self._cookie_file_is_ready():
            self._last_warning = None
            return True

        if not ok:
            return False

        # refresh は成功扱いでも、ファイルが無効/古いなら cookies は使わない
        self._last_warning = "cookie refresh finished but cookie file is still not ready; continue without cookies"
        logger.warning(self._last_warning)
        return False

    async def _refresh_cookie_file(self) -> bool:
        cookie_file = self.settings.cookie_file_path
        script_file = self.settings.cookie_refresh_script_path

        age = self._cookie_file_age_minutes()
        logger.info(
            "cookie refresh started: exists=%s valid=%s age_minutes=%s threshold=%s",
            self._cookie_file_exists(),
            self._cookie_file_is_valid(),
            None if age is None else round(age, 2),
            self.settings.youtube_cookie_refresh_minutes,
        )

        if not script_file.exists():
            self._last_warning = "cookie refresh script not found; continue without cookies"
            logger.warning(self._last_warning)
            return False

        cmd = ["python", str(script_file)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(BASE_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.settings.youtube_cookie_refresh_timeout_seconds,
            )
            output = stdout.decode("utf-8", errors="ignore")

            if proc.returncode != 0:
                self._last_warning = (
                    "cookie refresh failed; continue without cookies: "
                    + output[:1500]
                )
                logger.warning(self._last_warning)
                return False

            if not cookie_file.exists():
                self._last_warning = (
                    "cookie refresh finished but cookie file was not created; continue without cookies"
                )
                logger.warning(self._last_warning)
                return False

            if not self._cookie_file_is_valid():
                self._last_warning = (
                    "cookie refresh finished but cookie file is invalid; continue without cookies"
                )
                logger.warning(self._last_warning)
                return False

            age_after = self._cookie_file_age_minutes()
            logger.info(
                "cookie refresh completed: age_minutes=%s file=%s",
                None if age_after is None else round(age_after, 2),
                cookie_file,
            )
            self._last_warning = None
            return True

        except asyncio.TimeoutError:
            self._last_warning = "cookie refresh timed out; continue without cookies"
            logger.warning(self._last_warning)
            return False
        except Exception as exc:  # pragma: no cover
            self._last_warning = f"cookie refresh error; continue without cookies: {exc}"
            logger.warning(self._last_warning)
            return False


class YtDlpService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cookie_manager = CookieManager(settings)
        self.youtube_info_semaphore = asyncio.Semaphore(settings.max_concurrent_youtube_info_jobs)
        self._youtube_info_counts_lock = threading.Lock()
        self._youtube_info_waiting_count = 0
        self._youtube_info_running_count = 0

    @asynccontextmanager
    async def acquire_youtube_info_slot(self):
        with self._youtube_info_counts_lock:
            self._youtube_info_waiting_count += 1

        try:
            async with self.youtube_info_semaphore:
                with self._youtube_info_counts_lock:
                    self._youtube_info_waiting_count = max(0, self._youtube_info_waiting_count - 1)
                    self._youtube_info_running_count += 1
                try:
                    yield
                finally:
                    with self._youtube_info_counts_lock:
                        self._youtube_info_running_count = max(0, self._youtube_info_running_count - 1)
        except Exception:
            with self._youtube_info_counts_lock:
                self._youtube_info_waiting_count = max(0, self._youtube_info_waiting_count - 1)
            raise

    def get_youtube_info_queue_counts(self) -> dict[str, int]:
        with self._youtube_info_counts_lock:
            waiting = self._youtube_info_waiting_count
            running = self._youtube_info_running_count

        return {
            "youtube_info_processing_count": waiting + running,
            "youtube_info_running_count": running,
            "youtube_info_waiting_count": waiting,
            "max_concurrent_youtube_info_jobs": self.settings.max_concurrent_youtube_info_jobs,
        }

    @property
    def yt_dlp_path(self) -> Path:
        path = resolve_binary(self.settings.asset_dir_path, "yt-dlp", "yt-dlp.exe")
        if not path.exists():
            raise AppError(
                status_code=500,
                error_code="yt_dlp_binary_not_found",
                message=f"yt-dlp executable not found: {path}",
            )
        return path

    @property
    def ffmpeg_path(self) -> Path:
        path = resolve_binary(self.settings.asset_dir_path, "ffmpeg", "ffmpeg.exe")
        if not path.exists():
            raise AppError(
                status_code=500,
                error_code="ffmpeg_binary_not_found",
                message=f"ffmpeg executable not found: {path}",
            )
        return path

    @property
    def ffprobe_path(self) -> Path:
        path = resolve_binary(self.settings.asset_dir_path, "ffprobe", "ffprobe.exe")
        if not path.exists():
            raise AppError(
                status_code=500,
                error_code="ffprobe_binary_not_found",
                message=f"ffprobe executable not found: {path}",
            )
        return path

    def validate_youtube_url(self, url: str) -> None:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if hostname not in YOUTUBE_HOSTS:
            raise AppError(
                status_code=400,
                error_code="invalid_youtube_url",
                message="YouTube URL を指定してください",
            )

    def resolve_target_type(self, url: str, target_type: str) -> str:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        has_video = bool(query.get("v")) or parsed.netloc.endswith("youtu.be")
        has_playlist = bool(query.get("list")) or parsed.path.rstrip("/") == "/playlist"

        if target_type == "video":
            return "video"
        if target_type == "playlist":
            return "playlist"
        if has_playlist and not has_video:
            return "playlist"
        return "video"

    def extract_ids_from_url(self, url: str) -> tuple[str | None, str | None]:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        video_id: str | None = None
        playlist_id: str | None = None

        if parsed.netloc.endswith("youtu.be"):
            video_id = parsed.path.lstrip("/").split("/")[0] or None
            playlist_id = query.get("list", [None])[0]
        else:
            video_id = query.get("v", [None])[0]
            playlist_id = query.get("list", [None])[0]
            if parsed.path.rstrip("/") == "/playlist" and not playlist_id:
                playlist_id = query.get("list", [None])[0]

        return video_id, playlist_id

    def _is_valid_netscape_cookie_file(self, path: Path) -> bool:
        if not self.settings.youtube_cookies_enabled:
            return False
        if not path.exists() or not path.is_file():
            return False
        try:
            if path.stat().st_size <= 0:
                return False
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                first_line = f.readline().strip()
            return first_line.startswith("# Netscape HTTP Cookie File")
        except Exception:
            return False

    def _sanitize_cookie_file_for_ytdlp(self, path: Path) -> Path | None:
        if not self._is_valid_netscape_cookie_file(path):
            return None

        tmp_dir = (BASE_DIR / "tmp").resolve()
        ensure_directory(tmp_dir)
        sanitized = tmp_dir / "youtube_cookies.ytdlp.txt"

        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            out_lines: list[str] = []

            for i, line in enumerate(lines):
                if i == 0:
                    out_lines.append("# Netscape HTTP Cookie File")
                    continue

                if not line or line.startswith("#"):
                    out_lines.append(line)
                    continue

                parts = line.split("\t")
                if len(parts) < 7:
                    continue

                expires = parts[4].strip()
                try:
                    exp_val = int(float(expires))
                except Exception:
                    exp_val = 0
                if exp_val < 0:
                    exp_val = 0

                parts[4] = str(exp_val)
                out_lines.append("\t".join(parts[:7]))

            sanitized.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
            try:
                sanitized.chmod(0o600)
            except OSError:
                pass
            return sanitized
        except Exception as exc:  # pragma: no cover
            logger.warning("cookie sanitize failed, continue without cookies: %s", exc)
            return None

    def _build_cookie_args(self) -> list[str]:
        if not self.settings.youtube_cookies_enabled:
            return []

        cookie_path = self.settings.cookie_file_path
        sanitized = self._sanitize_cookie_file_for_ytdlp(cookie_path)
        if sanitized:
            return ["--cookies", str(sanitized)]
        return []

    def _youtube_extractor_args(self, player_client: str = "default") -> list[str]:
        return ["--extractor-args", f"youtube:player_client={player_client}"]

    @staticmethod
    def _is_cookie_auth_error(detail: str) -> bool:
        text = (detail or "").lower()
        markers = (
            "provided youtube account cookies are no longer valid",
            "sign in to confirm you’re not a bot",
            "sign in to confirm you're not a bot",
            "use --cookies-from-browser or --cookies for the authentication",
            "how do-i-pass-cookies-to-yt-dlp",
            "how-do-i-pass-cookies-to-yt-dlp",
        )
        return any(marker in text for marker in markers)

    async def _run_json_command(self, args: list[str], *, player_client: str = "default") -> dict[str, Any]:
        async def run_once(*, force_refresh: bool = False) -> tuple[int, str, str]:
            await self.cookie_manager.ensure_ready(force_refresh=force_refresh)

            cmd = [
                str(self.yt_dlp_path),
                "--ignore-config",
                *self._youtube_extractor_args(player_client),
                *args,
                *self._build_cookie_args(),
            ]

            logger.info("running yt-dlp metadata command: %s", cmd)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(BASE_DIR),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.settings.request_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise AppError(
                    status_code=504,
                    error_code="yt_dlp_timeout",
                    message="yt-dlp がタイムアウトしました",
                ) from exc

            stdout_text = stdout.decode("utf-8", errors="ignore").strip()
            stderr_text = stderr.decode("utf-8", errors="ignore").strip()
            return proc.returncode, stdout_text, stderr_text

        def extract_json_payload(text: str) -> dict[str, Any] | None:
            if not text:
                return None
            decoder = json.JSONDecoder()
            for start in range(len(text)):
                if text[start] not in "[{":
                    continue
                try:
                    value, end = decoder.raw_decode(text[start:])
                except json.JSONDecodeError:
                    continue
                trailing = text[start + end :].strip()
                if trailing:
                    continue
                if isinstance(value, dict):
                    return value
            return None

        returncode, stdout_text, stderr_text = await run_once()
        combined_error_text = "\n".join(part for part in [stderr_text, stdout_text] if part).strip()
        if returncode != 0 and self.settings.youtube_cookies_enabled and self._is_cookie_auth_error(combined_error_text):
            logger.warning("cookie-related yt-dlp metadata failure detected; forcing cookie refresh and retry")
            returncode, stdout_text, stderr_text = await run_once(force_refresh=True)
            combined_error_text = "\n".join(part for part in [stderr_text, stdout_text] if part).strip()

        if returncode != 0:
            raise AppError(
                status_code=502,
                error_code="yt_dlp_failed",
                message="yt-dlp の実行に失敗しました",
                extras={"detail": combined_error_text[-4000:]},
            )

        payload = extract_json_payload(stdout_text)
        if payload is not None:
            return payload

        raise AppError(
            status_code=502,
            error_code="yt_dlp_invalid_json",
            message="yt-dlp のメタデータ解析に失敗しました",
            extras={"detail": (stdout_text or stderr_text)[-4000:]},
        )

    def _base_metadata_args(self) -> list[str]:
        return [
            "-J",
            "--skip-download",
            "--ffmpeg-location",
            str(self.ffmpeg_path),
        ]

    async def fetch_video_info(self, url: str) -> dict[str, Any]:
        self.validate_youtube_url(url)
        args = [*self._base_metadata_args(), "--no-playlist", url]

        clients_to_try = [
            "default",
            "tv,web_safari",
            "web_safari",
        ]

        async with self.acquire_youtube_info_slot():
            last_exc: AppError | None = None
            for client in clients_to_try:
                try:
                    logger.info("fetch_video_info retrying with player_client=%s", client)
                    return await self._run_json_command(args, player_client=client)
                except AppError as exc:
                    last_exc = exc
                    detail = str(exc.extras.get("detail", "")) if exc.extras else ""
                    if exc.error_code == "yt_dlp_failed" and "Requested format is not available" in detail:
                        logger.warning("metadata fetch failed with player_client=%s: %s", client, detail[-1000:])
                        continue
                    raise

        assert last_exc is not None
        raise last_exc

    async def fetch_playlist_overview(
        self,
        url: str,
        *,
        playlist_start_index: int | None = None,
        playlist_end_index: int | None = None,
    ) -> dict[str, Any]:
        self.validate_youtube_url(url)
        args = [*self._base_metadata_args(), "--yes-playlist", "--flat-playlist"]
        if playlist_start_index is not None:
            args.extend(["--playlist-start", str(playlist_start_index)])
        if playlist_end_index is not None:
            args.extend(["--playlist-end", str(playlist_end_index)])
        args.append(url)
        async with self.acquire_youtube_info_slot():
            return await self._run_json_command(args, player_client="default")

    def _format_upload_date(self, value: str | None) -> str | None:
        if not value or len(value) != 8 or not value.isdigit():
            return value
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"

    def _find_best_m4a_audio(self, info: dict[str, Any]) -> dict[str, Any] | None:
        formats = info.get("formats") or []
        candidates: list[dict[str, Any]] = []

        for fmt in formats:
            if fmt.get("vcodec") != "none":
                continue
            ext = fmt.get("ext")
            acodec = fmt.get("acodec") or ""
            if ext == "m4a" or acodec.startswith("mp4a"):
                candidates.append(fmt)

        if not candidates:
            logger.warning("no m4a/mp4a audio candidate found for video_id=%s", info.get("id"))
            return None

        candidates.sort(
            key=lambda f: (
                -(f.get("abr") or 0),
                -(f.get("asr") or 0),
                -(f.get("filesize") or f.get("filesize_approx") or 0),
                str(f.get("format_id") or ""),
            )
        )
        return candidates[0]

    def _duration_limit_reason(self, seconds: int | None, max_seconds: int) -> str | None:
        if seconds and seconds > max_seconds:
            return "duration_limit_exceeded"
        return None

    def _size_limit_reason(self, size_bytes: int | None, limit_mb: int) -> str | None:
        if size_bytes and size_bytes > limit_mb * 1024 * 1024:
            return "filesize_limit_exceeded"
        return None

    def _mp4_selector_for_height(self, height: int) -> str:
        return (
            f"bestvideo[ext=mp4][height={height}]+bestaudio[ext=m4a]/"
            f"bestvideo[height={height}]+bestaudio/"
            f"best[ext=mp4][height={height}]/"
            f"best[height={height}]"
        )

    def _mp4_fallback_selector(self) -> str:
        return "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"

    def _m4a_selector(self) -> str:
        return "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio"

    def _build_video_formats(self, info: dict[str, Any]) -> list[dict[str, Any]]:
        formats = info.get("formats") or []
        best_audio = self._find_best_m4a_audio(info)
        best_audio_size = (
            (best_audio.get("filesize") or best_audio.get("filesize_approx") or 0)
            if best_audio
            else 0
        )

        grouped: dict[str, list[dict[str, Any]]] = {}
        duration = info.get("duration")

        for fmt in formats:
            height = fmt.get("height") or 0
            if not height:
                continue

            vcodec = fmt.get("vcodec") or "none"
            if vcodec == "none":
                continue

            ext = fmt.get("ext")
            acodec = fmt.get("acodec") or "none"
            resolution = fmt.get("resolution") or (
                f"{fmt.get('width')}x{height}" if fmt.get("width") else None
            )
            label = f"{height}p"

            if acodec != "none" and ext == "mp4":
                size_bytes = fmt.get("filesize") or fmt.get("filesize_approx")
                candidate = {
                    "label": label,
                    "format": "mp4",
                    "format_id": self._mp4_selector_for_height(height),
                    "ext": "mp4",
                    "resolution": resolution,
                    "height": height,
                    "fps": fmt.get("fps"),
                    "vcodec": vcodec,
                    "acodec": acodec,
                    "filesize": fmt.get("filesize"),
                    "filesize_approx": fmt.get("filesize_approx"),
                }
            elif acodec == "none" and ext == "mp4" and best_audio:
                raw_size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
                size_bytes = raw_size + best_audio_size if raw_size or best_audio_size else None
                candidate = {
                    "label": label,
                    "format": "mp4",
                    "format_id": self._mp4_selector_for_height(height),
                    "ext": "mp4",
                    "resolution": resolution,
                    "height": height,
                    "fps": fmt.get("fps"),
                    "vcodec": vcodec,
                    "acodec": best_audio.get("acodec") if best_audio else None,
                    "filesize": size_bytes,
                    "filesize_approx": size_bytes,
                }
            else:
                continue

            reject_reason = self._duration_limit_reason(duration, self.settings.max_duration_seconds_mp4)
            if not reject_reason:
                reject_reason = self._size_limit_reason(size_bytes, self.settings.max_file_size_mb_mp4)

            candidate["selectable"] = reject_reason is None
            candidate["reject_reason"] = reject_reason
            candidate["_sort_size"] = size_bytes if size_bytes is not None else math.inf
            candidate["_sort_fps"] = candidate.get("fps") or 0
            grouped.setdefault(label, []).append(candidate)

        selected: list[dict[str, Any]] = []
        for _, candidates in grouped.items():
            candidates.sort(
                key=lambda item: (
                    item["_sort_size"],
                    -int(item.get("height") or 0),
                    -(item.get("_sort_fps") or 0),
                    str(item.get("format_id") or ""),
                )
            )
            chosen = candidates[0]
            chosen.pop("_sort_size", None)
            chosen.pop("_sort_fps", None)
            selected.append(chosen)

        selected.sort(key=lambda item: (-(item.get("height") or 0), -(item.get("fps") or 0)))
        return selected

    def _build_audio_formats(self, info: dict[str, Any]) -> list[dict[str, Any]]:
        duration = info.get("duration")
        best_audio = self._find_best_m4a_audio(info)
        source_size = None

        if best_audio:
            source_size = best_audio.get("filesize") or best_audio.get("filesize_approx")

        mp3_estimated_size = None
        if duration:
            mp3_estimated_size = int(duration * 320_000 / 8)

        m4a_reject = self._duration_limit_reason(duration, self.settings.max_duration_seconds_mp3)
        mp3_reject = self._duration_limit_reason(duration, self.settings.max_duration_seconds_mp3)
        if not mp3_reject:
            mp3_reject = self._size_limit_reason(mp3_estimated_size, self.settings.max_file_size_mb_mp3)

        if not best_audio:
            m4a_reject = m4a_reject or "audio_source_not_found"
            mp3_reject = mp3_reject or "audio_source_not_found"

        return [
            {
                "label": "m4a",
                "format": "m4a",
                "source_audio": "m4a",
                "bitrate": best_audio.get("abr") if best_audio else None,
                "filesize": source_size,
                "selectable": m4a_reject is None,
                "reject_reason": m4a_reject,
                "note": "m4aをそのまま保存",
            },
            {
                "label": "mp3 320kbps",
                "format": "mp3",
                "source_audio": "m4a",
                "bitrate": 320,
                "filesize": mp3_estimated_size,
                "selectable": mp3_reject is None,
                "reject_reason": mp3_reject,
                "note": "m4aから320kbpsのmp3へ変換",
            },
        ]

    def _build_available_presets(self) -> dict[str, Any]:
        return {
            "video_qualities": ["2160p", "1440p", "1080p", "720p"],
            "audio_formats": ["m4a", "mp3"],
            "fallback_policy": ["nearest_lower"],
            "preferred_container": ["mp4"],
        }

    def _build_video_response(self, info: dict[str, Any]) -> dict[str, Any]:
        video_id = info.get("id")
        if not video_id:
            raise AppError(
                status_code=502,
                error_code="video_id_not_found",
                message="YouTube ID を取得できませんでした",
            )

        return {
            "success": True,
            "resource_type": "video",
            "video_id": video_id,
            "title": info.get("title"),
            "sanitized_title": sanitize_filename(info.get("title") or video_id),
            "duration_seconds": info.get("duration"),
            "thumbnail_url": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            "upload_date": self._format_upload_date(info.get("upload_date")),
            "available_presets": self._build_available_presets(),
            "cookie_warning": self.cookie_manager.last_warning,
        }

    def _build_playlist_entry_from_overview(
        self,
        entry: dict[str, Any],
        *,
        index: int,
        default_uploader: str | None,
    ) -> dict[str, Any]:
        entry_id = entry.get("id")
        return {
            "index": index,
            "video_id": entry_id,
            "title": entry.get("title"),
            "duration_seconds": entry.get("duration"),
            "thumbnail_url": entry.get("thumbnails", [{}])[0].get("url") if isinstance(entry.get("thumbnails"), list) and entry.get("thumbnails") else None,
            "uploader": entry.get("uploader") or entry.get("channel") or default_uploader,
            "upload_date": self._format_upload_date(entry.get("upload_date")),
            "is_downloadable": bool(entry_id),
            "reject_reason": None if entry_id else "playlist_entry_id_not_found",
        }

    async def get_formats(
        self,
        url: str,
        target_type: str,
        *,
        playlist_start_index: int | None = None,
        playlist_end_index: int | None = None,
    ) -> dict[str, Any]:
        resolved = self.resolve_target_type(url, target_type)

        if resolved == "video":
            info = await self.fetch_video_info(url)
            return self._build_video_response(info)

        page_size = max(1, self.settings.playlist_max_items)
        requested_start_index = playlist_start_index or 1

        if playlist_end_index is not None and playlist_end_index < requested_start_index:
            raise AppError(
                status_code=400,
                error_code="invalid_playlist_range",
                message="playlist_end_index は playlist_start_index 以上で指定してください",
            )

        requested_end_index = playlist_end_index
        if requested_end_index is None:
            requested_end_index = requested_start_index + page_size - 1
        else:
            requested_end_index = min(requested_end_index, requested_start_index + page_size - 1)

        overview = await self.fetch_playlist_overview(
            url,
            playlist_start_index=requested_start_index,
            playlist_end_index=requested_end_index,
        )
        playlist_id = overview.get("id") or self.extract_ids_from_url(url)[1]
        if not playlist_id:
            raise AppError(
                status_code=400,
                error_code="playlist_id_not_found",
                message="Playlist ID を取得できませんでした",
            )

        entries = overview.get("entries") or []
        processed_entries = [
            self._build_playlist_entry_from_overview(entry, index=pos, default_uploader=overview.get("uploader"))
            for pos, entry in enumerate(entries, start=requested_start_index)
        ]
        accepted_count = sum(1 for item in processed_entries if item["is_downloadable"])
        rejected_count = len(processed_entries) - accepted_count

        returned_count = len(processed_entries)
        returned_start_index = processed_entries[0]["index"] if processed_entries else None
        returned_end_index = processed_entries[-1]["index"] if processed_entries else None
        chunk_limit = requested_end_index - requested_start_index + 1
        has_more = returned_count == chunk_limit and chunk_limit == page_size

        return {
            "success": True,
            "resource_type": "playlist",
            "playlist_id": playlist_id,
            "playlist_title": overview.get("title"),
            "playlist_count": overview.get("playlist_count") or len(entries),
            "uploader": overview.get("uploader"),
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            "available_presets": self._build_available_presets(),
            "playlist_chunk": {
                "start_index": requested_start_index,
                "end_index": requested_end_index,
                "page_size": page_size,
                "returned_count": returned_count,
                "returned_start_index": returned_start_index,
                "returned_end_index": returned_end_index,
                "has_more": has_more,
                "next_start_index": (requested_end_index + 1) if has_more else None,
                "next_end_index": (requested_end_index + page_size) if has_more else None,
            },
            "entries": processed_entries,
            "cookie_warning": self.cookie_manager.last_warning,
        }

    async def build_video_context(self, item: DownloadItemRequest) -> VideoContext:
        video_id = item.video_id or self.extract_ids_from_url(str(item.url))[0]
        if not video_id:
            raise AppError(
                status_code=400,
                error_code="video_id_not_found",
                message="YouTube ID を取得できませんでした",
            )

        info = await self.fetch_video_info(f"https://www.youtube.com/watch?v={video_id}")
        resource = self._build_video_response(info)

        if resource["duration_seconds"]:
            if item.media_type == "video" and resource["duration_seconds"] > self.settings.max_duration_seconds_mp4:
                raise AppError(
                    status_code=400,
                    error_code="duration_limit_exceeded",
                    message="動画時間が mp4 の上限を超えています",
                    extras={
                        "limit_type": "duration_seconds",
                        "actual_value": resource["duration_seconds"],
                        "limit_value": self.settings.max_duration_seconds_mp4,
                    },
                )

            if item.media_type == "audio" and resource["duration_seconds"] > self.settings.max_duration_seconds_mp3:
                raise AppError(
                    status_code=400,
                    error_code="duration_limit_exceeded",
                    message="動画時間が音声の上限を超えています",
                    extras={
                        "limit_type": "duration_seconds",
                        "actual_value": resource["duration_seconds"],
                        "limit_value": self.settings.max_duration_seconds_mp3,
                    },
                )

        formats = info.get("formats") or []
        has_video_source = any((fmt.get("vcodec") or "none") != "none" for fmt in formats)
        has_audio_source = any((fmt.get("acodec") or "none") != "none" for fmt in formats)

        if item.media_type == "video" and not has_video_source:
            raise AppError(
                status_code=400,
                error_code="video_source_not_found",
                message="動画ソースが見つかりません",
            )

        if item.media_type == "audio" and not has_audio_source:
            raise AppError(
                status_code=400,
                error_code="audio_source_not_found",
                message="音声ソースが見つかりません",
            )

        return VideoContext(
            video_id=video_id,
            playlist_id=item.playlist_id,
            index=item.index,
            title=resource["title"],
            sanitized_title=resource["sanitized_title"],
            duration_seconds=resource["duration_seconds"],
            thumbnail_url=resource["thumbnail_url"],
            uploader=resource["uploader"],
            upload_date=resource["upload_date"],
            source_url=str(item.url),
            resource_type=item.target_type,
            raw_info=info,
        )
    def build_progress_key(
        self,
        video_id: str,
        media_type: str,
        quality: str | None,
        audio_format: str | None,
    ) -> str:
        if media_type == "video":
            return f"{video_id}_video_{quality or 'unknown'}_{audio_format or 'm4a'}"
        return f"{video_id}_audio_{audio_format or 'unknown'}"

    def build_output_paths(
        self,
        ctx: VideoContext,
        media_type: str,
        quality: str | None,
        audio_format: str | None,
        preferred_container: str | None,
    ) -> tuple[Path, Path | None]:
        if ctx.playlist_id and ctx.index is not None:
            base_dir = (
                self.settings.playlist_save_root_path
                / sanitize_component(ctx.playlist_id)
                / f"{ctx.index:03d}_{sanitize_component(ctx.video_id)}"
            )
        else:
            base_dir = self.settings.download_root_path / sanitize_component(ctx.video_id)

        if media_type == "video":
            selector_dir = base_dir / f"video_{sanitize_component(quality or 'best')}_{sanitize_component(preferred_container or 'mp4')}"
            final_path = selector_dir / f"{ctx.sanitized_title}.{preferred_container or 'mp4'}"
            temp_base = selector_dir / f"{ctx.sanitized_title}.downloaded"
            return final_path, temp_base

        selector_dir = base_dir / f"audio_{sanitize_component(audio_format or 'best')}"
        final_path = selector_dir / f"{ctx.sanitized_title}.{audio_format or 'm4a'}"
        temp_base = selector_dir / f"{ctx.sanitized_title}.source"
        return final_path, temp_base

    def _quality_to_height(self, quality: str) -> int:
        mapping = {
            "2160p": 2160,
            "1440p": 1440,
            "1080p": 1080,
            "720p": 720,
        }
        if quality not in mapping:
            raise AppError(
                status_code=400,
                error_code="invalid_quality",
                message="未対応の画質です",
            )
        return mapping[quality]

    def _height_to_quality_label(self, height: int | None) -> str | None:
        if height is None or height <= 0:
            return None
        if height in {2160, 1440, 1080, 720}:
            return f"{height}p"
        return f"{height}p"

    def _video_selector_for_quality(self, quality: str) -> str:
        height = self._quality_to_height(quality)
        return f"bv*[height<=?{height}]+ba/b[height<=?{height}]"

    def _video_sort_order(self) -> str:
        return "res,ext:mp4:m4a"

    def _audio_selector(self) -> str:
        return "bestaudio/best"

    def _audio_sort_order(self) -> str:
        return "ext:m4a"

    def _output_template_for_base(self, base_path: Path) -> str:
        return str(base_path.parent / f"{base_path.name}.%(ext)s")

    def _find_downloaded_artifact(self, base_path: Path) -> Path | None:
        candidates = sorted(base_path.parent.glob(f"{base_path.name}.*"))
        ignore_suffixes = {".part", ".ytdl", ".json", ".info.json", ".description", ".jpg", ".png", ".webp"}
        filtered = [p for p in candidates if p.suffix.lower() not in ignore_suffixes]
        if not filtered:
            return None
        filtered.sort(key=lambda p: (p.suffix.lower() != ".mp4", p.suffix.lower() != ".m4a", p.name))
        return filtered[0]

    async def download_item(self, *, ctx: VideoContext, item: DownloadItemRequest, progress_cb) -> Path:
        final_path, temp_base = self.build_output_paths(
            ctx,
            item.media_type,
            item.quality,
            item.audio_format,
            item.preferred_container,
        )
        ensure_directory(final_path.parent)

        if final_path.exists():
            return final_path

        if item.media_type == "video":
            assert item.quality is not None
            assert temp_base is not None
            await self._download_video(ctx, item.quality, final_path, temp_base, progress_cb)
            return final_path

        assert item.audio_format is not None
        assert temp_base is not None
        await self._download_audio(ctx, item.audio_format, final_path, temp_base, progress_cb)
        return final_path

    async def _run_download_process(
        self,
        cmd: list[str],
        progress_cb,
        converting_on_keywords: tuple[str, ...] = (),
    ) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(BASE_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise AppError(
                status_code=500,
                error_code="executable_not_found",
                message="実行ファイルが見つかりません",
            ) from exc

        async def consume() -> str:
            lines: list[str] = []
            assert proc.stdout is not None

            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                lines.append(line)
                await self._parse_progress_line(line, progress_cb, converting_on_keywords)

            return "\n".join(lines)

        try:
            output = await asyncio.wait_for(consume(), timeout=self.settings.request_timeout)
            rc = await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise AppError(
                status_code=504,
                error_code="download_timeout",
                message="ダウンロード処理がタイムアウトしました",
            ) from exc

        if rc != 0:
            raise AppError(
                status_code=502,
                error_code="download_process_failed",
                message="ダウンロード処理に失敗しました",
                extras={"detail": output[-4000:]},
            )

        return output

    @staticmethod
    def _int_or_none(value: str | None) -> int | None:
        if value is None:
            return None
        value = value.strip()
        if not value or value == "NA":
            return None
        try:
            return int(float(value))
        except ValueError:
            return None

    @staticmethod
    def _float_percent_or_none(value: str | None) -> float | None:
        if value is None:
            return None
        value = value.strip().replace("%", "")
        try:
            return round(float(value), 2)
        except ValueError:
            return None

    async def _parse_progress_line(self, line: str, progress_cb, converting_on_keywords: tuple[str, ...]) -> None:
        if line.startswith("download:"):
            payload = line.split("download:", 1)[1]
            match = PROGRESS_LINE_RE.match(payload)
            if match:
                downloaded = self._int_or_none(match.group("downloaded")) or 0
                total = self._int_or_none(match.group("total")) or self._int_or_none(match.group("estimated"))
                percent = self._float_percent_or_none(match.group("percent"))
                if percent is None and total:
                    percent = round(downloaded / total * 100, 2)

                await progress_cb(
                    status="downloading",
                    progress_percent=percent or 0.0,
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    message="ダウンロード中",
                )
                return

        lower = line.lower()
        if any(keyword.lower() in lower for keyword in converting_on_keywords):
            await progress_cb(status="postprocessing", progress_percent=95.0, message="後処理中")

    async def _download_video(
        self,
        ctx: VideoContext,
        quality: str,
        final_path: Path,
        temp_base: Path,
        progress_cb,
    ) -> None:
        await self.cookie_manager.ensure_ready()
        source_url = f"https://www.youtube.com/watch?v={ctx.video_id}"
        selector = self._video_selector_for_quality(quality)
        sort_order = self._video_sort_order()

        def build_cmd() -> list[str]:
            return [
                str(self.yt_dlp_path),
                "--ignore-config",
                *self._youtube_extractor_args("default"),
                "--no-playlist",
                "--newline",
                "--progress",
                "--progress-template",
                "download:%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress._percent_str)s|%(progress.status)s",
                "--ffmpeg-location",
                str(self.ffmpeg_path),
                "--format-sort",
                sort_order,
                "--merge-output-format",
                "mp4",
                "-f",
                selector,
                "-o",
                self._output_template_for_base(temp_base),
                source_url,
                *self._build_cookie_args(),
            ]

        async def run_once(*, force_refresh: bool = False) -> None:
            if force_refresh:
                await self.cookie_manager.ensure_ready(force_refresh=True)
            await self._run_download_process(build_cmd(), progress_cb, converting_on_keywords=("merger", "remux", "recode"))

        try:
            await run_once()
        except AppError as exc:
            detail = str(exc.extras.get("detail", "")) if exc.extras else ""
            if exc.error_code == "download_process_failed" and self._is_cookie_auth_error(detail):
                logger.warning("cookie-related yt-dlp video download failure detected; forcing cookie refresh and retry")
                await run_once(force_refresh=True)
            else:
                raise

        if final_path.exists():
            return

        source_path = self._find_downloaded_artifact(temp_base)
        if source_path is None:
            raise AppError(
                status_code=502,
                error_code="download_output_not_found",
                message="ダウンロード後の動画ファイルが見つかりません",
            )

        await progress_cb(status="postprocessing", progress_percent=96.0, message="mp4 へ整形中")
        await self._ensure_video_mp4(source_path, final_path)

    async def _download_audio(
        self,
        ctx: VideoContext,
        audio_format: str,
        final_path: Path,
        temp_base: Path,
        progress_cb,
    ) -> None:
        await self.cookie_manager.ensure_ready()
        source_url = f"https://www.youtube.com/watch?v={ctx.video_id}"

        def build_cmd() -> list[str]:
            return [
                str(self.yt_dlp_path),
                "--ignore-config",
                *self._youtube_extractor_args("default"),
                "--no-playlist",
                "--newline",
                "--progress",
                "--progress-template",
                "download:%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress._percent_str)s|%(progress.status)s",
                "--ffmpeg-location",
                str(self.ffmpeg_path),
                "--format-sort",
                self._audio_sort_order(),
                "-f",
                self._audio_selector(),
                "-o",
                self._output_template_for_base(temp_base),
                source_url,
                *self._build_cookie_args(),
            ]

        async def run_once(*, force_refresh: bool = False) -> None:
            if force_refresh:
                await self.cookie_manager.ensure_ready(force_refresh=True)
            await self._run_download_process(build_cmd(), progress_cb)

        try:
            await run_once()
        except AppError as exc:
            detail = str(exc.extras.get("detail", "")) if exc.extras else ""
            if exc.error_code == "download_process_failed" and self._is_cookie_auth_error(detail):
                logger.warning("cookie-related yt-dlp audio download failure detected; forcing cookie refresh and retry")
                await run_once(force_refresh=True)
            else:
                raise

        if final_path.exists():
            return

        source_path = self._find_downloaded_artifact(temp_base)
        if source_path is None:
            raise AppError(
                status_code=502,
                error_code="download_output_not_found",
                message="ダウンロード後の音声ファイルが見つかりません",
            )

        if audio_format == "m4a":
            if source_path.suffix.lower() == ".m4a":
                source_path.replace(final_path)
                return
            await progress_cb(status="postprocessing", progress_percent=96.0, message="m4a へ変換中")
            await self._convert_to_m4a(source_path, final_path)
            return

        await self._convert_to_mp3(source_path, final_path, ctx.duration_seconds, progress_cb)

    async def _run_ffmpeg_command(self, cmd: list[str], *, timeout: int | None = None, error_code: str, message: str) -> bytes:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(BASE_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout or self.settings.request_timeout)
        except asyncio.TimeoutError as exc:
            raise AppError(
                status_code=504,
                error_code=f"{error_code}_timeout",
                message=f"{message} がタイムアウトしました",
            ) from exc

        if proc.returncode != 0:
            raise AppError(
                status_code=502,
                error_code=error_code,
                message=f"{message} に失敗しました",
                extras={"detail": stdout.decode("utf-8", errors="ignore")[-4000:]},
            )
        return stdout

    async def _ensure_video_mp4(self, source_path: Path, final_path: Path) -> None:
        if source_path.resolve() == final_path.resolve():
            return
        if source_path.suffix.lower() == ".mp4":
            source_path.replace(final_path)
            return

        copy_cmd = [
            str(self.ffmpeg_path),
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c",
            "copy",
            str(final_path),
        ]
        try:
            await self._run_ffmpeg_command(
                copy_cmd,
                error_code="ffmpeg_video_remux_failed",
                message="ffmpeg による mp4 リマックス",
            )
        except AppError:
            transcode_cmd = [
                str(self.ffmpeg_path),
                "-y",
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(final_path),
            ]
            await self._run_ffmpeg_command(
                transcode_cmd,
                error_code="ffmpeg_video_transcode_failed",
                message="ffmpeg による mp4 変換",
            )

        if source_path.exists() and source_path.resolve() != final_path.resolve():
            source_path.unlink(missing_ok=True)

    async def _convert_to_m4a(self, input_path: Path, output_path: Path) -> None:
        cmd = [
            str(self.ffmpeg_path),
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_path),
        ]
        await self._run_ffmpeg_command(
            cmd,
            error_code="ffmpeg_m4a_failed",
            message="ffmpeg による m4a 変換",
        )
        if input_path.exists() and input_path.resolve() != output_path.resolve():
            input_path.unlink(missing_ok=True)

    async def _convert_to_mp3(self, input_path: Path, output_path: Path, duration_seconds: int | None, progress_cb) -> None:
        await progress_cb(status="postprocessing", progress_percent=95.0, message="mp3 へ変換中")

        cmd = [
            str(self.ffmpeg_path),
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "320k",
            str(output_path),
        ]

        await self._run_ffmpeg_command(
            cmd,
            error_code="ffmpeg_failed",
            message="ffmpeg による mp3 変換",
        )

        if input_path.exists() and input_path.resolve() != output_path.resolve():
            input_path.unlink(missing_ok=True)

        await progress_cb(
            status="postprocessing",
            progress_percent=99.0 if duration_seconds else 98.0,
            message="mp3 変換完了処理中",
        )

    async def _probe_video_height(self, path: Path) -> int | None:
        cmd = [
            str(self.ffprobe_path),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,height",
            "-of",
            "json",
            str(path),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(BASE_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=self.settings.request_timeout)
        except asyncio.TimeoutError as exc:
            raise AppError(
                status_code=504,
                error_code="ffprobe_timeout",
                message="ffprobe がタイムアウトしました",
            ) from exc

        if proc.returncode != 0:
            raise AppError(
                status_code=502,
                error_code="ffprobe_failed",
                message="ffprobe による画質確認に失敗しました",
            )

        payload = json.loads(stdout.decode("utf-8", errors="ignore") or "{}")
        heights = [
            int(stream.get("height"))
            for stream in (payload.get("streams") or [])
            if stream.get("codec_type") == "video" and stream.get("height")
        ]
        return max(heights) if heights else None

    async def inspect_downloaded_video(
        self,
        path: Path,
        requested_quality: str | None,
        *,
        raw_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            height = await self._probe_video_height(path)
        except Exception:
            logger.exception("failed to inspect downloaded video quality")
            height = None
        resolved_quality = self._height_to_quality_label(height)
        quality_exact_match = None
        fallback_reason = None
        requested_quality_available: bool | None = None

        if requested_quality and raw_info is not None:
            target_height = self._quality_to_height(requested_quality)
            requested_quality_available = any(
                (fmt.get("vcodec") or "none") != "none" and int(fmt.get("height") or 0) == target_height
                for fmt in (raw_info.get("formats") or [])
            )

        if requested_quality and resolved_quality:
            quality_exact_match = resolved_quality == requested_quality
            if not quality_exact_match:
                if requested_quality_available is True:
                    fallback_reason = "requested quality available but final output resolved to a different quality"
                else:
                    fallback_reason = "requested quality not available; downloaded nearest lower quality"
        elif requested_quality and not resolved_quality:
            fallback_reason = "requested quality could not be verified from downloaded file"

        return {
            "resolved_quality": resolved_quality,
            "quality_exact_match": quality_exact_match,
            "fallback_reason": fallback_reason,
        }
