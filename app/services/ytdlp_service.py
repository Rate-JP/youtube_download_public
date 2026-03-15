from __future__ import annotations

import asyncio
import json
import logging
import math
import re
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
        async def run_once(*, force_refresh: bool = False) -> tuple[int, str]:
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
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.settings.request_timeout)
            except asyncio.TimeoutError as exc:
                raise AppError(
                    status_code=504,
                    error_code="yt_dlp_timeout",
                    message="yt-dlp がタイムアウトしました",
                ) from exc

            raw = stdout.decode("utf-8", errors="ignore").strip()
            return proc.returncode, raw

        returncode, raw = await run_once()
        if returncode != 0 and self.settings.youtube_cookies_enabled and self._is_cookie_auth_error(raw):
            logger.warning("cookie-related yt-dlp metadata failure detected; forcing cookie refresh and retry")
            returncode, raw = await run_once(force_refresh=True)

        if returncode != 0:
            raise AppError(
                status_code=502,
                error_code="yt_dlp_failed",
                message="yt-dlp の実行に失敗しました",
                extras={"detail": raw[-4000:]},
            )

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AppError(
                status_code=502,
                error_code="yt_dlp_invalid_json",
                message="yt-dlp のメタデータ解析に失敗しました",
                extras={"detail": raw[-4000:]},
            ) from exc

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

    async def fetch_playlist_overview(self, url: str) -> dict[str, Any]:
        self.validate_youtube_url(url)
        args = [*self._base_metadata_args(), "--yes-playlist", "--flat-playlist", url]
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
            "video_formats": self._build_video_formats(info),
            "audio_formats": self._build_audio_formats(info),
            "cookie_warning": self.cookie_manager.last_warning,
        }

    async def get_formats(self, url: str, target_type: str) -> dict[str, Any]:
        resolved = self.resolve_target_type(url, target_type)

        if resolved == "video":
            info = await self.fetch_video_info(url)
            return self._build_video_response(info)

        overview = await self.fetch_playlist_overview(url)
        playlist_id = overview.get("id") or self.extract_ids_from_url(url)[1]
        if not playlist_id:
            raise AppError(
                status_code=400,
                error_code="playlist_id_not_found",
                message="Playlist ID を取得できませんでした",
            )

        entries = overview.get("entries") or []
        processed_entries: list[dict[str, Any]] = []
        accepted_count = 0
        rejected_count = 0

        for pos, entry in enumerate(entries, start=1):
            entry_id = entry.get("id")

            if pos > self.settings.playlist_max_items:
                processed_entries.append(
                    {
                        "index": pos,
                        "video_id": entry_id,
                        "title": entry.get("title"),
                        "duration_seconds": entry.get("duration"),
                        "thumbnail_url": None,
                        "uploader": overview.get("uploader"),
                        "upload_date": None,
                        "is_downloadable": False,
                        "reject_reason": "playlist_max_items_exceeded",
                        "video_formats": [],
                        "audio_formats": [],
                    }
                )
                rejected_count += 1
                continue

            if not entry_id:
                processed_entries.append(
                    {
                        "index": pos,
                        "video_id": None,
                        "title": entry.get("title"),
                        "duration_seconds": entry.get("duration"),
                        "thumbnail_url": None,
                        "uploader": overview.get("uploader"),
                        "upload_date": None,
                        "is_downloadable": False,
                        "reject_reason": "playlist_entry_id_not_found",
                        "video_formats": [],
                        "audio_formats": [],
                    }
                )
                rejected_count += 1
                continue

            try:
                video_info = await self.fetch_video_info(f"https://www.youtube.com/watch?v={entry_id}")
                item = self._build_video_response(video_info)

                entry_payload = {
                    "index": pos,
                    "video_id": item["video_id"],
                    "title": item["title"],
                    "duration_seconds": item["duration_seconds"],
                    "thumbnail_url": item["thumbnail_url"],
                    "uploader": item["uploader"],
                    "upload_date": item["upload_date"],
                    "is_downloadable": bool(
                        any(v["selectable"] for v in item["video_formats"])
                        or any(a["selectable"] for a in item["audio_formats"])
                    ),
                    "reject_reason": None,
                    "video_formats": item["video_formats"],
                    "audio_formats": item["audio_formats"],
                }

                if entry_payload["is_downloadable"]:
                    accepted_count += 1
                else:
                    entry_payload["reject_reason"] = "no_selectable_format"
                    rejected_count += 1

                processed_entries.append(entry_payload)

            except AppError as exc:
                logger.warning("playlist entry rejected: %s", exc.message)
                processed_entries.append(
                    {
                        "index": pos,
                        "video_id": entry_id,
                        "title": entry.get("title"),
                        "duration_seconds": entry.get("duration"),
                        "thumbnail_url": None,
                        "uploader": overview.get("uploader"),
                        "upload_date": None,
                        "is_downloadable": False,
                        "reject_reason": exc.error_code,
                        "video_formats": [],
                        "audio_formats": [],
                    }
                )
                rejected_count += 1
                if not self.settings.playlist_continue_on_error:
                    break

        return {
            "success": True,
            "resource_type": "playlist",
            "playlist_id": playlist_id,
            "playlist_title": overview.get("title"),
            "playlist_count": len(entries),
            "uploader": overview.get("uploader"),
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
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
            if item.format == "mp4" and resource["duration_seconds"] > self.settings.max_duration_seconds_mp4:
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

            if item.format in {"mp3", "m4a"} and resource["duration_seconds"] > self.settings.max_duration_seconds_mp3:
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

        if item.format == "mp4":
            matched = next((fmt for fmt in resource["video_formats"] if fmt["format_id"] == item.format_id), None)
            if not matched:
                raise AppError(
                    status_code=400,
                    error_code="invalid_format_id",
                    message="指定された mp4 format_id が無効です",
                )
            if not matched["selectable"]:
                raise AppError(
                    status_code=400,
                    error_code=matched["reject_reason"] or "format_not_selectable",
                    message="指定された mp4 フォーマットは選択できません",
                )
        else:
            audio_item = next((fmt for fmt in resource["audio_formats"] if fmt["format"] == item.format), None)
            if not audio_item or not audio_item["selectable"]:
                raise AppError(
                    status_code=400,
                    error_code=(audio_item or {}).get("reject_reason") or "audio_format_not_selectable",
                    message="指定された音声フォーマットは選択できません",
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

    def build_progress_key(self, video_id: str, output_format: str, format_id: str | None) -> str:
        selector = format_id if output_format == "mp4" else "audio"
        return f"{video_id}:{output_format}:{selector}"

    def build_output_paths(self, ctx: VideoContext, output_format: str, format_id: str | None) -> tuple[Path, Path | None]:
        title_with_ext = f"{ctx.sanitized_title}.{output_format}"

        if ctx.playlist_id and ctx.index is not None:
            base_dir = (
                self.settings.playlist_save_root_path
                / sanitize_component(ctx.playlist_id)
                / f"{ctx.index:03d}_{sanitize_component(ctx.video_id)}"
            )
        else:
            base_dir = self.settings.download_root_path / sanitize_component(ctx.video_id)

        if output_format == "mp4":
            selector_dir = base_dir / f"mp4_{sanitize_component(format_id or 'best')}"
            final_path = selector_dir / title_with_ext
        else:
            selector_dir = base_dir
            final_path = selector_dir / title_with_ext

        temp_audio = None
        if output_format == "mp3":
            temp_audio = selector_dir / f"{ctx.sanitized_title}.source.m4a"

        return final_path, temp_audio

    async def download_item(self, *, ctx: VideoContext, output_format: str, format_id: str | None, progress_cb) -> Path:
        final_path, temp_audio_path = self.build_output_paths(ctx, output_format, format_id)
        ensure_directory(final_path.parent)

        if final_path.exists():
            return final_path

        if output_format == "mp4":
            await self._download_mp4(ctx, format_id or self._mp4_fallback_selector(), final_path, progress_cb)
            return final_path

        if output_format == "m4a":
            await self._download_m4a(ctx, final_path, progress_cb)
            return final_path

        if output_format == "mp3":
            assert temp_audio_path is not None
            await self._download_m4a(ctx, temp_audio_path, progress_cb)
            await self._convert_to_mp3(temp_audio_path, final_path, ctx.duration_seconds, progress_cb)
            if temp_audio_path.exists():
                temp_audio_path.unlink(missing_ok=True)
            return final_path

        raise AppError(
            status_code=400,
            error_code="unsupported_format",
            message="未対応のフォーマットです",
        )

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
            await progress_cb(status="converting", progress_percent=95.0, message="変換中")

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

    async def _download_mp4(self, ctx: VideoContext, format_id: str, final_path: Path, progress_cb) -> None:
        await self.cookie_manager.ensure_ready()

        source_url = ctx.source_url if ctx.resource_type == "video" else f"https://www.youtube.com/watch?v={ctx.video_id}"
        selector = format_id or self._mp4_fallback_selector()

        async def run_with_selector(current_selector: str) -> str:
            async def run_once(*, force_refresh: bool = False) -> str:
                if force_refresh:
                    await self.cookie_manager.ensure_ready(force_refresh=True)
                cmd = [
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
                    "--merge-output-format",
                    "mp4",
                    "-f",
                    current_selector,
                    "-o",
                    str(final_path.with_suffix(".%(ext)s")),
                    source_url,
                    *self._build_cookie_args(),
                ]
                return await self._run_download_process(cmd, progress_cb, converting_on_keywords=("merger",))

            try:
                return await run_once()
            except AppError as exc:
                detail = str(exc.extras.get("detail", "")) if exc.extras else ""
                if exc.error_code == "download_process_failed" and self._is_cookie_auth_error(detail):
                    logger.warning("cookie-related yt-dlp download failure detected; forcing cookie refresh and retry")
                    return await run_once(force_refresh=True)
                raise

        try:
            await run_with_selector(selector)
        except AppError as exc:
            detail = str(exc.extras.get("detail", "")) if exc.extras else ""
            if exc.error_code == "download_process_failed" and "Requested format is not available" in detail:
                fallback = self._mp4_fallback_selector()
                if selector != fallback:
                    logger.warning("requested mp4 selector unavailable; retry with fallback selector: %s", fallback)
                    await progress_cb(
                        status="downloading",
                        progress_percent=0.0,
                        message="選択フォーマットが取得できないため自動フォールバックします",
                    )
                    await run_with_selector(fallback)
                    return
            raise

    async def _download_m4a(self, ctx: VideoContext, final_path: Path, progress_cb) -> None:
        best_audio = self._find_best_m4a_audio(ctx.raw_info)
        if not best_audio:
            raise AppError(
                status_code=400,
                error_code="audio_source_not_found",
                message="m4a 音声ソースが見つかりません",
            )

        await self.cookie_manager.ensure_ready()

        source_url = f"https://www.youtube.com/watch?v={ctx.video_id}"
        selector = self._m4a_selector()

        def build_cmd(current_selector: str) -> list[str]:
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
                "-f",
                current_selector,
                "-o",
                str(final_path.with_suffix(".%(ext)s")),
                source_url,
                *self._build_cookie_args(),
            ]

        async def run_cmd(current_selector: str, *, force_refresh: bool = False) -> None:
            if force_refresh:
                await self.cookie_manager.ensure_ready(force_refresh=True)
            await self._run_download_process(build_cmd(current_selector), progress_cb)

        try:
            await run_cmd(selector)
        except AppError as exc:
            detail = str(exc.extras.get("detail", "")) if exc.extras else ""
            if exc.error_code == "download_process_failed" and self._is_cookie_auth_error(detail):
                logger.warning("cookie-related yt-dlp download failure detected; forcing cookie refresh and retry")
                await run_cmd(selector, force_refresh=True)
                return
            if exc.error_code == "download_process_failed" and "Requested format is not available" in detail:
                logger.warning("requested m4a selector unavailable; retry with bestaudio")
                await progress_cb(
                    status="downloading",
                    progress_percent=0.0,
                    message="選択音声が取得できないため自動フォールバックします",
                )
                try:
                    await run_cmd("bestaudio/best")
                except AppError as fallback_exc:
                    fallback_detail = str(fallback_exc.extras.get("detail", "")) if fallback_exc.extras else ""
                    if fallback_exc.error_code == "download_process_failed" and self._is_cookie_auth_error(fallback_detail):
                        logger.warning("cookie-related yt-dlp fallback download failure detected; forcing cookie refresh and retry")
                        await run_cmd("bestaudio/best", force_refresh=True)
                        return
                    raise

        if final_path.suffix != ".m4a" and not final_path.exists():
            for alt_ext in (".m4a", ".webm", ".m4b", ".mp4"):
                alt = final_path.with_suffix(alt_ext)
                if alt.exists() and alt != final_path:
                    alt.replace(final_path)
                    break

    async def _convert_to_mp3(self, input_path: Path, output_path: Path, duration_seconds: int | None, progress_cb) -> None:
        await progress_cb(status="converting", progress_percent=95.0, message="mp3 へ変換中")

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

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(BASE_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.settings.request_timeout)
        except asyncio.TimeoutError as exc:
            raise AppError(
                status_code=504,
                error_code="ffmpeg_timeout",
                message="ffmpeg がタイムアウトしました",
            ) from exc

        if proc.returncode != 0:
            raise AppError(
                status_code=502,
                error_code="ffmpeg_failed",
                message="ffmpeg による mp3 変換に失敗しました",
                extras={"detail": stdout.decode("utf-8", errors="ignore")[-4000:]},
            )

        await progress_cb(
            status="converting",
            progress_percent=99.0 if duration_seconds else 98.0,
            message="mp3 変換完了処理中",
        )
