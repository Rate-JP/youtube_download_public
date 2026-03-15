from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.config import BASE_DIR, Settings
from app.services.job_manager import ACTIVE_STATUSES, JobManager
from app.utils.files import compute_last_activity_timestamp, is_path_within, relative_to_root

logger = logging.getLogger(__name__)


class CleanupService:
    def __init__(self, settings: Settings, job_manager: JobManager):
        self.settings = settings
        self.job_manager = job_manager

    def run_cleanup(self) -> dict[str, Any]:
        cleanup_roots = self._cleanup_roots()
        for root in cleanup_roots:
            root.mkdir(parents=True, exist_ok=True)

        cutoff = datetime.now(UTC) - timedelta(hours=self.settings.file_ttl_hours)
        deleted_paths: list[str] = []
        skipped_paths: list[str] = []
        deleted_files_count = 0
        deleted_dirs_count = 0
        skipped_files_count = 0
        skipped_dirs_count = 0
        errors_count = 0

        active_paths = {
            (BASE_DIR / state.file_path).resolve()
            for state in self.job_manager.jobs.values()
            if state.status in ACTIVE_STATUSES and state.file_path
        }

        for root in cleanup_roots:
            files = sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: len(p.parts), reverse=True)
            for path in files:
                try:
                    if not is_path_within(path, root):
                        skipped_files_count += 1
                        skipped_paths.append(str(path))
                        continue
                    if self._is_active(path, active_paths):
                        skipped_files_count += 1
                        skipped_paths.append(relative_to_root(path, root))
                        continue
                    last_activity = datetime.fromtimestamp(compute_last_activity_timestamp(path), tz=UTC)
                    if last_activity > cutoff:
                        skipped_files_count += 1
                        continue
                    path.unlink(missing_ok=True)
                    deleted_files_count += 1
                    deleted_paths.append(relative_to_root(path, root))
                except Exception:
                    errors_count += 1
                    logger.exception("cleanup file deletion failed: %s", path)

            dirs = sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True)
            for path in dirs:
                try:
                    if not is_path_within(path, root):
                        skipped_dirs_count += 1
                        skipped_paths.append(str(path))
                        continue
                    if self._is_active(path, active_paths):
                        skipped_dirs_count += 1
                        skipped_paths.append(relative_to_root(path, root))
                        continue
                    if any(path.iterdir()):
                        skipped_dirs_count += 1
                        continue
                    path.rmdir()
                    deleted_dirs_count += 1
                    deleted_paths.append(relative_to_root(path, root))
                except OSError:
                    skipped_dirs_count += 1
                except Exception:
                    errors_count += 1
                    logger.exception("cleanup dir deletion failed: %s", path)

        return {
            "success": True,
            "message": "cleanup completed",
            "deleted_files_count": deleted_files_count,
            "deleted_dirs_count": deleted_dirs_count,
            "skipped_files_count": skipped_files_count,
            "skipped_dirs_count": skipped_dirs_count,
            "errors_count": errors_count,
            "deleted_paths": deleted_paths,
            "skipped_paths": skipped_paths,
        }

    def _cleanup_roots(self) -> list[Path]:
        candidates = [
            self.settings.download_root_path.resolve(),
            self.settings.playlist_save_root_path.resolve(),
        ]

        unique_candidates = list(dict.fromkeys(candidates))
        cleanup_roots: list[Path] = []
        for candidate in sorted(unique_candidates, key=lambda p: len(p.parts)):
            if any(candidate == existing or candidate in existing.parents for existing in cleanup_roots):
                continue
            cleanup_roots.append(candidate)
        return cleanup_roots

    @staticmethod
    def _is_active(path: Path, active_paths: set[Path]) -> bool:
        resolved = path.resolve()
        for active in active_paths:
            if resolved == active or active in resolved.parents or resolved in active.parents:
                return True
        return False
