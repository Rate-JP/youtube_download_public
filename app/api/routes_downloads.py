from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_client_context, get_job_manager, get_runtime_status_service
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
):
    return await manager.enqueue_many(payload.items, runtime_status=runtime_status)
