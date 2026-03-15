from __future__ import annotations

import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import get_client_context, get_job_manager, get_settings_dep
from app.core.config import Settings
from app.core.security import ClientContext
from app.services.job_manager import JobManager
from app.utils.files import sanitize_filename

router = APIRouter(tags=["files"])


class FilesDownloadRequest(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=200)
    archive_name: str | None = Field(default=None, max_length=200)


def _storage_base_dir(settings: Settings) -> Path:
    """
    progress.file_path は download_root_path.parent からの相対パスとして保持している。
    例:
      DOWNLOAD_ROOT=/app/dl/f
      file_path=f/xxx.mp3
      復元基準=/app/dl
    """
    return settings.download_root_path.parent.resolve()


def _resolve_progress_file_path(file_path: str, settings: Settings) -> Path:
    storage_base = _storage_base_dir(settings)
    abs_path = (storage_base / file_path).resolve()

    try:
        abs_path.relative_to(storage_base)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error_code": "invalid_file_path",
                "message": "不正なファイルパスです",
            },
        ) from exc

    return abs_path


def _ensure_ready_progress(progress: dict) -> None:
    if progress["status"] not in {"completed", "reused"}:
        raise HTTPException(
            status_code=409,
            detail={
                "success": False,
                "error_code": "file_not_ready",
                "message": "まだファイルの準備ができていません",
            },
        )


def _delete_file_silently(path: Path) -> None:
    try:
        if path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        pass


@router.post("/files/download")
async def download_files(
    payload: FilesDownloadRequest,
    background_tasks: BackgroundTasks,
    _client: ClientContext = Depends(get_client_context),
    manager: JobManager = Depends(get_job_manager),
    settings: Settings = Depends(get_settings_dep),
):
    resolved_files: list[tuple[str, Path]] = []

    for key in payload.keys:
        progress = manager.get_progress(key)
        _ensure_ready_progress(progress)

        file_path = progress.get("file_path")
        if not file_path:
            raise HTTPException(
                status_code=404,
                detail={
                    "success": False,
                    "error_code": "file_path_not_found",
                    "message": f"ファイルパスが見つかりません: {key}",
                },
            )

        abs_path = _resolve_progress_file_path(file_path, settings)
        if not abs_path.exists() or not abs_path.is_file():
            raise HTTPException(
                status_code=404,
                detail={
                    "success": False,
                    "error_code": "file_not_found",
                    "message": f"ファイルが存在しません: {key}",
                },
            )

        resolved_files.append((key, abs_path))


    if len(resolved_files) == 1:
        only_path = resolved_files[0][1]
        return FileResponse(
            path=str(only_path),
            filename=only_path.name,
            media_type="application/octet-stream",
        )

    now_str = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive_name = sanitize_filename(payload.archive_name or f"downloads_{now_str}", max_length=120)
    if not archive_name.lower().endswith(".zip"):
        archive_name = f"{archive_name}.zip"

    tmp_dir = Path(tempfile.gettempdir()).resolve()
    with tempfile.NamedTemporaryFile(prefix="yt_files_", suffix=".zip", delete=False, dir=tmp_dir) as tmp:
        zip_path = Path(tmp.name)

    used_names: set[str] = set()

    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for key, abs_path in resolved_files:
            arcname = abs_path.name
            if arcname in used_names:
                stem = abs_path.stem
                suffix = abs_path.suffix
                safe_key = key.replace(":", "_").replace("/", "_")
                arcname = f"{stem}__{safe_key}{suffix}"
            used_names.add(arcname)
            zf.write(abs_path, arcname=arcname)

    background_tasks.add_task(_delete_file_silently, zip_path)

    return FileResponse(
        path=str(zip_path),
        filename=archive_name,
        media_type="application/zip",
    )
