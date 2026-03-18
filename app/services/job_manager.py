from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.runtime_status_service import RuntimeStatusService

from app.core.config import Settings
from app.core.exceptions import AppError
from app.models.schemas import DownloadItemRequest
from app.services.ytdlp_service import VideoContext, YtDlpService
from app.utils.files import relative_to_root

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = {"queued", "downloading", "postprocessing", "quality_check"}


@dataclass
class JobState:
    key: str
    video_id: str
    playlist_id: str | None
    index: int | None
    title: str
    media_type: str
    format: str
    requested_quality: str | None
    requested_audio_format: str | None
    fallback_policy: str | None
    preferred_container: str | None
    status: str
    progress_percent: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    message: str = "queued"
    file_path: str | None = None
    error_code: str | None = None
    downloaded: bool = False
    reused: bool = False
    expires_at: str | None = None
    quality_check_status: str | None = None
    resolved_quality: str | None = None
    quality_exact_match: bool | None = None
    fallback_reason: str | None = None
    final_container: str | None = None
    final_audio_format: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def to_progress_dict(self) -> dict[str, Any]:
        return {
            "success": True,
            "key": self.key,
            "video_id": self.video_id,
            "playlist_id": self.playlist_id,
            "index": self.index,
            "media_type": self.media_type,
            "format": self.format,
            "requested_quality": self.requested_quality,
            "requested_audio_format": self.requested_audio_format,
            "fallback_policy": self.fallback_policy,
            "preferred_container": self.preferred_container,
            "status": self.status,
            "progress_percent": round(self.progress_percent, 2),
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "message": self.message,
            "file_path": self.file_path,
            "error_code": self.error_code,
            "quality_check_status": self.quality_check_status,
            "resolved_quality": self.resolved_quality,
            "quality_exact_match": self.quality_exact_match,
            "fallback_reason": self.fallback_reason,
            "final_container": self.final_container,
            "final_audio_format": self.final_audio_format,
        }


class JobManager:
    def __init__(self, settings: Settings, ytdlp_service: YtDlpService):
        self.settings = settings
        self.ytdlp_service = ytdlp_service
        self.video_semaphore = asyncio.Semaphore(settings.max_concurrent_video_jobs)
        self.audio_semaphore = asyncio.Semaphore(settings.max_concurrent_audio_jobs)
        self.quality_check_semaphore = asyncio.Semaphore(2)
        self.jobs: dict[str, JobState] = {}
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self._submission_lock = asyncio.Lock()

    def _expiry_string(self) -> str:
        return (datetime.now(UTC) + timedelta(hours=self.settings.file_ttl_hours)).replace(microsecond=0).isoformat()

    def _storage_base_dir(self) -> Path:
        return self.settings.download_root_path.parent.resolve()

    def _state_abs_path(self, state: JobState) -> Path | None:
        if not state.file_path:
            return None
        return (self._storage_base_dir() / state.file_path).resolve()

    def _job_state_to_download_item(self, state: JobState) -> dict[str, Any]:
        return {
            "video_id": state.video_id,
            "playlist_id": state.playlist_id,
            "index": state.index,
            "title": state.title,
            "media_type": state.media_type,
            "requested_quality": state.requested_quality,
            "requested_audio_format": state.requested_audio_format,
            "fallback_policy": state.fallback_policy,
            "preferred_container": state.preferred_container,
            "progress_key": state.key,
            "status": state.status,
            "downloaded": state.downloaded,
            "reused": state.reused,
            "file_path": state.file_path,
            "expires_at": state.expires_at,
            "error_code": state.error_code,
            "message": state.message,
            "quality_check_pending": state.media_type == "video" and state.quality_check_status != "completed",
        }

    async def enqueue_many(
        self,
        items: list[DownloadItemRequest],
        runtime_status: "RuntimeStatusService | None" = None,
    ) -> dict[str, Any]:
        self.cleanup_expired_progress()
        results: list[dict[str, Any]] = []
        accepted_count = 0
        queued_count = 0
        reused_count = 0
        failed_count = 0

        for item in items:
            try:
                result = await self.enqueue_item(item, runtime_status=runtime_status)
                results.append(result)
                accepted_count += 1
                if result["reused"]:
                    reused_count += 1
                else:
                    queued_count += 1
            except AppError as exc:
                failed_count += 1
                results.append(
                    {
                        "video_id": item.video_id,
                        "playlist_id": item.playlist_id,
                        "index": item.index,
                        "title": None,
                        "media_type": item.media_type,
                        "requested_quality": item.quality,
                        "requested_audio_format": item.audio_format,
                        "fallback_policy": item.fallback_policy,
                        "preferred_container": item.preferred_container,
                        "progress_key": None,
                        "status": "failed",
                        "downloaded": False,
                        "reused": False,
                        "file_path": None,
                        "expires_at": None,
                        "error_code": exc.error_code,
                        "message": exc.message,
                        "quality_check_pending": item.media_type == "video",
                        **exc.extras,
                    }
                )

        return {
            "success": failed_count == 0,
            "accepted_count": accepted_count,
            "queued_count": queued_count,
            "reused_count": reused_count,
            "failed_count": failed_count,
            "items": results,
        }

    async def enqueue_item(
        self,
        item: DownloadItemRequest,
        runtime_status: "RuntimeStatusService | None" = None,
    ) -> dict[str, Any]:
        ctx = await self.ytdlp_service.build_video_context(item)
        key = self.ytdlp_service.build_progress_key(
            ctx.video_id,
            item.media_type,
            item.quality,
            item.audio_format,
        )
        final_path, _temp_path = self.ytdlp_service.build_output_paths(
            ctx,
            item.media_type,
            item.quality,
            item.audio_format,
            item.preferred_container,
        )
        relative_path = relative_to_root(final_path, self.settings.download_root_path)

        async with self._submission_lock:
            existing = self.jobs.get(key)

            if existing and existing.status in ACTIVE_STATUSES:
                existing.reused = True
                existing.message = "既存ジョブを参照しました"
                return self._job_state_to_download_item(existing)

            if final_path.exists():
                quality_info = None
                if item.media_type == "video":
                    quality_info = await self.ytdlp_service.inspect_downloaded_video(final_path, item.quality)

                state = JobState(
                    key=key,
                    video_id=ctx.video_id,
                    playlist_id=ctx.playlist_id,
                    index=ctx.index,
                    title=ctx.title,
                    media_type=item.media_type,
                    format=item.preferred_container or item.audio_format or "mp4",
                    requested_quality=item.quality,
                    requested_audio_format=item.audio_format,
                    fallback_policy=item.fallback_policy,
                    preferred_container=item.preferred_container,
                    status="reused",
                    progress_percent=100.0,
                    message="既存ファイルを再利用しました",
                    file_path=relative_path,
                    downloaded=True,
                    reused=True,
                    expires_at=self._expiry_string(),
                    quality_check_status="completed" if item.media_type == "video" else "skipped",
                    resolved_quality=(quality_info or {}).get("resolved_quality"),
                    quality_exact_match=(quality_info or {}).get("quality_exact_match"),
                    fallback_reason=(quality_info or {}).get("fallback_reason"),
                    final_container=item.preferred_container if item.media_type == "video" else None,
                    final_audio_format=item.audio_format,
                    finished_at=datetime.now(UTC),
                )
                self.jobs[key] = state
                return self._job_state_to_download_item(state)

            if existing and existing.status in {"completed", "reused", "failed"}:
                self.jobs.pop(key, None)
                task = self.tasks.pop(key, None)
                if task and task.done():
                    task.cancel()

            if runtime_status is not None:
                runtime_status.enforce_and_increment_conversions(1)

            state = JobState(
                key=key,
                video_id=ctx.video_id,
                playlist_id=ctx.playlist_id,
                index=ctx.index,
                title=ctx.title,
                media_type=item.media_type,
                format=item.preferred_container or item.audio_format or "mp4",
                requested_quality=item.quality,
                requested_audio_format=item.audio_format,
                fallback_policy=item.fallback_policy,
                preferred_container=item.preferred_container,
                status="queued",
                progress_percent=0.0,
                message="ジョブを受け付けました",
                file_path=relative_path,
                downloaded=False,
                reused=False,
                expires_at=self._expiry_string(),
                quality_check_status="pending" if item.media_type == "video" else "skipped",
                final_audio_format=item.audio_format,
            )
            self.jobs[key] = state
            self.tasks[key] = asyncio.create_task(self._run_job(state, ctx, item))
            return self._job_state_to_download_item(state)

    async def _run_job(self, state: JobState, ctx: VideoContext, item: DownloadItemRequest) -> None:
        semaphore = self.video_semaphore if item.media_type == "video" else self.audio_semaphore
        try:
            async with semaphore:
                await self._update_job(
                    state.key,
                    status="downloading",
                    message="ダウンロードを開始しました",
                )
                output_path = await self.ytdlp_service.download_item(
                    ctx=ctx,
                    item=item,
                    progress_cb=lambda **kwargs: self._update_job(state.key, **kwargs),
                )

            if item.media_type == "video":
                await self._update_job(
                    state.key,
                    status="quality_check",
                    progress_percent=99.0,
                    message="画質確認中",
                    quality_check_status="running",
                    file_path=relative_to_root(output_path, self.settings.download_root_path),
                )
                async with self.quality_check_semaphore:
                    quality_info = await self.ytdlp_service.inspect_downloaded_video(output_path, item.quality, raw_info=ctx.raw_info)

                await self._update_job(
                    state.key,
                    status="completed",
                    progress_percent=100.0,
                    message="処理が完了しました",
                    downloaded=True,
                    reused=False,
                    file_path=relative_to_root(output_path, self.settings.download_root_path),
                    finished_at=datetime.now(UTC),
                    expires_at=self._expiry_string(),
                    quality_check_status="completed",
                    resolved_quality=quality_info.get("resolved_quality"),
                    quality_exact_match=quality_info.get("quality_exact_match"),
                    fallback_reason=quality_info.get("fallback_reason"),
                    final_container=item.preferred_container,
                    final_audio_format=item.audio_format,
                )
            else:
                await self._update_job(
                    state.key,
                    status="completed",
                    progress_percent=100.0,
                    message="処理が完了しました",
                    downloaded=True,
                    reused=False,
                    file_path=relative_to_root(output_path, self.settings.download_root_path),
                    finished_at=datetime.now(UTC),
                    expires_at=self._expiry_string(),
                    final_audio_format=item.audio_format,
                )
        except AppError as exc:
            await self._update_job(
                state.key,
                status="failed",
                message=exc.message,
                error_code=exc.error_code,
                finished_at=datetime.now(UTC),
            )
            logger.exception("job failed: %s", exc.message)
        except Exception as exc:  # pragma: no cover
            await self._update_job(
                state.key,
                status="failed",
                message=str(exc),
                error_code="unexpected_error",
                finished_at=datetime.now(UTC),
            )
            logger.exception("unexpected job failure")

    async def _update_job(self, key: str, **changes: Any) -> None:
        state = self.jobs[key]
        for field_name, value in changes.items():
            if value is not None:
                setattr(state, field_name, value)
        state.updated_at = datetime.now(UTC)

    def get_progress(self, key: str) -> dict[str, Any]:
        self.cleanup_expired_progress()
        state = self.jobs.get(key)
        if not state:
            raise AppError(
                status_code=404,
                error_code="progress_key_not_found",
                message="指定された progress key は存在しません",
            )

        if state.status in {"completed", "reused"}:
            abs_path = self._state_abs_path(state)
            if not abs_path or not abs_path.exists():
                state.status = "failed"
                state.error_code = "file_deleted_by_cleanup"
                state.message = "cleanup によりファイルが削除されました"
                state.updated_at = datetime.now(UTC)
                state.finished_at = datetime.now(UTC)

        return state.to_progress_dict()

    def get_active_queue_counts(self) -> dict[str, int]:
        self.cleanup_expired_progress()

        audio_active_count = 0
        video_active_count = 0
        audio_waiting_count = 0
        video_waiting_count = 0
        audio_running_count = 0
        video_running_count = 0

        for state in self.jobs.values():
            if state.status not in ACTIVE_STATUSES:
                continue

            is_waiting = state.status == "queued"

            if state.media_type == "video":
                video_active_count += 1
                if is_waiting:
                    video_waiting_count += 1
                else:
                    video_running_count += 1
            else:
                audio_active_count += 1
                if is_waiting:
                    audio_waiting_count += 1
                else:
                    audio_running_count += 1

        total_active_count = audio_active_count + video_active_count
        total_waiting_count = audio_waiting_count + video_waiting_count
        total_running_count = audio_running_count + video_running_count

        return {
            # Backward-compatible fields. These counts include both running and waiting jobs.
            "audio_processing_count": audio_active_count,
            "video_processing_count": video_active_count,
            "total_processing_count": total_active_count,
            # Explicit breakdown for current running and waiting jobs.
            "audio_running_count": audio_running_count,
            "video_running_count": video_running_count,
            "total_running_count": total_running_count,
            "audio_waiting_count": audio_waiting_count,
            "video_waiting_count": video_waiting_count,
            "total_waiting_count": total_waiting_count,
            # Configured concurrency limits from environment/settings.
            "max_concurrent_audio_jobs": self.settings.max_concurrent_audio_jobs,
            "max_concurrent_video_jobs": self.settings.max_concurrent_video_jobs,
        }

    def get_progress_many(self, keys: list[str]) -> dict[str, Any]:
        items = []
        for key in keys:
            if key in self.jobs:
                try:
                    items.append(self.get_progress(key))
                except AppError:
                    items.append(
                        {
                            "success": False,
                            "key": key,
                            "status": "not_found",
                            "progress_percent": 0,
                            "error_code": "progress_key_not_found",
                        }
                    )
            else:
                items.append(
                    {
                        "success": False,
                        "key": key,
                        "status": "not_found",
                        "progress_percent": 0,
                        "error_code": "progress_key_not_found",
                    }
                )
        return {"success": True, "items": items}

    def cleanup_expired_progress(self) -> None:
        now = datetime.now(UTC)
        retention = timedelta(minutes=self.settings.progress_retention_minutes)
        to_delete = [
            key
            for key, state in self.jobs.items()
            if state.finished_at and state.finished_at + retention < now and state.status not in ACTIVE_STATUSES
        ]
        for key in to_delete:
            self.jobs.pop(key, None)
            task = self.tasks.pop(key, None)
            if task and task.done():
                task.cancel()

    def active_file_paths(self) -> set[Path]:
        active_paths: set[Path] = set()
        for state in self.jobs.values():
            if state.status in ACTIVE_STATUSES and state.file_path:
                abs_path = self._state_abs_path(state)
                if abs_path:
                    active_paths.add(abs_path)
        return active_paths
