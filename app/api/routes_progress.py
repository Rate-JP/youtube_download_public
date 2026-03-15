from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_client_context, get_job_manager
from app.core.security import ClientContext
from app.models.schemas import ProgressBulkRequest
from app.services.job_manager import JobManager

router = APIRouter(tags=["progress"])


@router.get("/progress")
async def progress(
    key: str = Query(..., min_length=3),
    _client: ClientContext = Depends(get_client_context),
    manager: JobManager = Depends(get_job_manager),
):
    return manager.get_progress(key)


@router.post("/progress")
async def progress_many(
    payload: ProgressBulkRequest,
    _client: ClientContext = Depends(get_client_context),
    manager: JobManager = Depends(get_job_manager),
):
    return manager.get_progress_many(payload.keys)
