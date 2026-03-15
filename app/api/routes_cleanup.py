from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_cleanup_client_context, get_cleanup_service
from app.core.config import get_settings
from app.core.security import ClientContext
from app.services.cleanup_service import CleanupService

router = APIRouter(tags=["cleanup"])


@router.get("/cleanup/{secret_path}")
async def cleanup(
    secret_path: str,
    _client: ClientContext = Depends(get_cleanup_client_context),
    service: CleanupService = Depends(get_cleanup_service),
):
    settings = get_settings()
    if secret_path != settings.cleanup_secret_path:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "cleanup_secret_path_mismatch", "message": "cleanup path が不正です"},
        )
    return service.run_cleanup()
