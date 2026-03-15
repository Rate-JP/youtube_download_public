from __future__ import annotations

import platform
from pathlib import Path


def is_windows() -> bool:
    return platform.system().lower().startswith("win")


def resolve_binary(asset_dir: Path, linux_name: str, windows_name: str) -> Path:
    return (asset_dir / windows_name) if is_windows() else (asset_dir / linux_name)
