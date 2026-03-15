from __future__ import annotations

from fastapi import Depends, Header, Request

from app.core.config import Settings, get_settings
from app.core.security import ClientContext, verify_ip_allowed
from app.services.cleanup_service import CleanupService
from app.services.job_manager import JobManager
from app.services.runtime_status_service import RuntimeStatusService
from app.services.ytdlp_service import YtDlpService


def get_settings_dep() -> Settings:
    return get_settings()


def get_client_context(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
) -> ClientContext:
    return verify_ip_allowed(request, settings=settings, cleanup=False)


def get_cleanup_client_context(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    cleanup_header_token: str | None = Header(default=None, alias="X-Cleanup-Token"),
) -> ClientContext:
    return verify_ip_allowed(
        request,
        settings=settings,
        cleanup=True,
        cleanup_header_token=cleanup_header_token,
    )


def get_ytdlp_service(request: Request) -> YtDlpService:
    return request.app.state.ytdlp_service


def get_job_manager(request: Request) -> JobManager:
    return request.app.state.job_manager


def get_cleanup_service(request: Request) -> CleanupService:
    return request.app.state.cleanup_service


def get_runtime_status_service(request: Request) -> RuntimeStatusService:
    return request.app.state.runtime_status_service
