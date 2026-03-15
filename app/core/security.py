from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from app.core.config import Settings, get_settings


@dataclass
class ClientContext:
    client_ip: str
    source_ip: str
    used_forwarded_for: bool


def verify_ip_allowed(
    request: Request,
    settings: Settings | None = None,
    cleanup: bool = False,
    cleanup_header_token: str | None = None,
) -> ClientContext:
    settings = settings or get_settings()

    remote_ip = request.client.host if request.client else None
    if not remote_ip:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "client_ip_resolution_failed",
                "message": "クライアントIPを解決できませんでした",
            },
        )

    try:
        ip_obj = ipaddress.ip_address(remote_ip)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "invalid_client_ip",
                "message": "解決済みクライアントIPが不正です",
            },
        ) from exc

    if settings.allowed_ip_networks and not any(ip_obj in network for network in settings.allowed_ip_networks):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "ip_not_allowed",
                "message": "許可されていないIPアドレスです",
            },
        )

    if cleanup and settings.cleanup_require_header_token:
        if not settings.cleanup_header_token or cleanup_header_token != settings.cleanup_header_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "cleanup_token_invalid",
                    "message": "cleanup 用ヘッダトークンが不正です",
                },
            )

    return ClientContext(
        client_ip=remote_ip,
        source_ip=remote_ip,
        used_forwarded_for=settings.trust_proxy_headers,
    )
