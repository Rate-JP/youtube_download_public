from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import (
    get_client_context,
    get_job_manager,
    get_runtime_status_service,
    get_settings_dep,
)
from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import ClientContext
from app.models.schemas import DownloadRequest
from app.services.job_manager import JobManager
from app.services.runtime_status_service import RuntimeStatusService

router = APIRouter(tags=["download"])


@router.post("/download")
async def download(
    payload: DownloadRequest,
    _client: ClientContext = Depends(get_client_context),
    manager: JobManager = Depends(get_job_manager),
    runtime_status: RuntimeStatusService = Depends(get_runtime_status_service),
    settings: Settings = Depends(get_settings_dep),
):
    item_count = len(payload.items)
    max_items = settings.max_download_items_per_request

    if item_count > max_items:
        raise AppError(
            status_code=400,
            error_code="download_items_limit_exceeded",
            message="1回のリクエストで指定できるダウンロード件数の上限を超えています",
            extras={
                "max_download_items_per_request": max_items,
                "requested_items": item_count,
            },
        )

    return await manager.enqueue_many(payload.to_item_requests(), runtime_status=runtime_status)
