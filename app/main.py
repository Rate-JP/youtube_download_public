from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes_cleanup import router as cleanup_router
from app.api.routes_downloads import router as download_router
from app.api.routes_files import router as files_router
from app.api.routes_formats import router as formats_router
from app.api.routes_progress import router as progress_router
from app.api.routes_server import router as server_router
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.services.cleanup_service import CleanupService
from app.services.job_manager import JobManager
from app.services.runtime_status_service import RuntimeStatusService
from app.services.ytdlp_service import YtDlpService
from app.utils.files import ensure_directory

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="YouTube Downloader API",
    version="1.4.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

origins = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "https://ydl.mt-latte.net",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger.info("CORS middleware enabled for origins: %s", origins)


@app.on_event("startup")
async def startup_event() -> None:
    if settings.uvicorn_workers != 1:
        raise RuntimeError("This implementation is single-process only. Set UVICORN_WORKERS=1.")
    ensure_directory(settings.asset_dir_path)
    ensure_directory(settings.download_root_path)
    ensure_directory(settings.playlist_save_root_path)
    app.state.ytdlp_service = YtDlpService(settings)
    app.state.job_manager = JobManager(settings, app.state.ytdlp_service)
    app.state.cleanup_service = CleanupService(settings, app.state.job_manager)
    app.state.runtime_status_service = RuntimeStatusService(settings, app.state.ytdlp_service, app.state.job_manager)
    logger.info("application started")


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error_code": exc.error_code,
            "message": exc.message,
            **exc.extras,
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    detail.setdefault("success", False)
    return JSONResponse(status_code=exc.status_code, content=detail)


@app.get("/healthz", tags=["health"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(formats_router)
app.include_router(download_router)
app.include_router(progress_router)
app.include_router(files_router)
app.include_router(server_router)
app.include_router(cleanup_router)
