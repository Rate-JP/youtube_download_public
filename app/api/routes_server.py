from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_client_context, get_runtime_status_service
from app.core.security import ClientContext
from app.services.runtime_status_service import RuntimeStatusService

router = APIRouter(tags=["server"])


@router.get("/server/status")
async def server_status(
    _client: ClientContext = Depends(get_client_context),
    service: RuntimeStatusService = Depends(get_runtime_status_service),
):
    return service.get_server_status()
