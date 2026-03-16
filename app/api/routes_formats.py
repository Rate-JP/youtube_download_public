from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_client_context, get_runtime_status_service, get_ytdlp_service
from app.core.security import ClientContext
from app.models.schemas import FormatsRequest
from app.services.runtime_status_service import RuntimeStatusService
from app.services.ytdlp_service import YtDlpService

router = APIRouter(tags=["formats"])


@router.post("/formats")
async def formats(
    payload: FormatsRequest,
    _client: ClientContext = Depends(get_client_context),
    runtime_status: RuntimeStatusService = Depends(get_runtime_status_service),
    service: YtDlpService = Depends(get_ytdlp_service),
):
    runtime_status.enforce_and_increment_formats()
    return await service.get_formats(
        str(payload.url),
        payload.target_type,
        playlist_start_index=payload.playlist_start_index,
        playlist_end_index=payload.playlist_end_index,
    )
