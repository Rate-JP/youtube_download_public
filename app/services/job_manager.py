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

ACTIVE_STATUSES = {"queued", "downloading", "converting"}


@dataclass
class JobState:
    key: str
    video_id: str
    playlist_id: str | None
    index: int | None
    title: str
    format: str
    format_id: str | None
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
            "format": self.format,
            "format_id": self.format_id,
            "status": self.status,
            "progress_percent": round(self.progress_percent, 2),
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "message": self.message,
            "file_path": self.file_path,
            "error_code": self.error_code,
        }


class JobManager:
    def __init__(self, settings: Settings, ytdlp_service: YtDlpService):
        self.settings = settings
        self.ytdlp_service = ytdlp_service
        self.video_semaphore = asyncio.Semaphore(settings.max_concurrent_video_jobs)
        self.audio_semaphore = asyncio.Semaphore(settings.max_concurrent_audio_jobs)
        self.jobs: dict[str, JobState] = {}
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self._submission_lock = asyncio.Lock()

    def _expiry_string(self) -> str:
        return (datetime.now(UTC) + timedelta(hours=self.settings.file_ttl_hours)).replace(microsecond=0).isoformat()

    def _storage_base_dir(self) -> Path:
        """
        state.file_path は download_root_path.parent からの相対パスとして保存している。
        例:
          DOWNLOAD_ROOT=/app/dl/f
          file_path=f/abc.mp3
          storage_base=/app/dl
        """
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
            "format": state.format,
            "format_id": state.format_id,
            "progress_key": state.key,
            "status": state.status,
            "downloaded": state.downloaded,
            "reused": state.reused,
            "file_path": state.file_path,
            "expires_at": state.expires_at,
            "error_code": state.error_code,
            "message": state.message,
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
                        "format": item.format,
                        "format_id": item.format_id,
                        "progress_key": None,
                        "status": "failed",
                        "downloaded": False,
                        "reused": False,
                        "file_path": None,
                        "expires_at": None,
                        "error_code": exc.error_code,
                        "message": exc.message,
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
        key = self.ytdlp_service.build_progress_key(ctx.video_id, item.format, item.format_id)
        final_path, _ = self.ytdlp_service.build_output_paths(ctx, item.format, item.format_id)
        relative_path = relative_to_root(final_path, self.settings.download_root_path)

        async with self._submission_lock:
            existing = self.jobs.get(key)

            # 1) 実行中ジョブだけは既存参照
            if existing and existing.status in ACTIVE_STATUSES:
                existing.reused = True
                existing.message = "既存ジョブを参照しました"
                return self._job_state_to_download_item(existing)

            # 2) 実ファイルが存在する時だけ reused 扱い
            if final_path.exists():
                state = JobState(
                    key=key,
                    video_id=ctx.video_id,
                    playlist_id=ctx.playlist_id,
                    index=ctx.index,
                    title=ctx.title,
                    format=item.format,
                    format_id=item.format_id,
                    status="reused",
                    progress_percent=100.0,
                    message="既存ファイルを再利用しました",
                    file_path=relative_path,
                    downloaded=False,
                    reused=True,
                    expires_at=self._expiry_string(),
                    finished_at=datetime.now(UTC),
                )
                self.jobs[key] = state
                return self._job_state_to_download_item(state)

            # 3) completed / reused / failed が残っていても、
            #    実ファイルが無いなら古い状態は破棄して新規ジョブへ
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
                format=item.format,
                format_id=item.format_id,
                status="queued",
                progress_percent=0.0,
                message="ジョブを受け付けました",
                file_path=relative_path,
                downloaded=True,
                reused=False,
                expires_at=self._expiry_string(),
            )
            self.jobs[key] = state
            self.tasks[key] = asyncio.create_task(self._run_job(state, ctx))
            return self._job_state_to_download_item(state)

    async def _run_job(self, state: JobState, ctx: VideoContext) -> None:
        semaphore = self.video_semaphore if state.format == "mp4" else self.audio_semaphore
        async with semaphore:
            try:
                await self._update_job(
                    state.key,
                    status="downloading",
                    message="ダウンロードを開始しました",
                )
                output_path = await self.ytdlp_service.download_item(
                    ctx=ctx,
                    output_format=state.format,
                    format_id=state.format_id,
                    progress_cb=lambda **kwargs: self._update_job(state.key, **kwargs),
                )
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

        # completed / reused の見かけだけ残っていて、cleanup 等で実体が消えている場合を補正
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
        audio_count = 0
        video_count = 0

        for state in self.jobs.values():
            if state.status not in ACTIVE_STATUSES:
                continue
            if state.format == "mp4":
                video_count += 1
            else:
                audio_count += 1

        return {
            "audio_processing_count": audio_count,
            "video_processing_count": video_count,
            "total_processing_count": audio_count + video_count,
        }

    def get_progress_many(self, keys: list[str]) -> dict[str, Any]:
        items = []
        for key in keys:
            if key in self.jobs:
                # 単体取得側で cleanup 後のファイル消失補正をしているので、
                # 一括側も同じ処理を経由させる
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
