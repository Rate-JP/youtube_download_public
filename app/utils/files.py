from __future__ import annotations

import os
import re
from pathlib import Path

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
MULTI_SPACE = re.compile(r"\s+")


def sanitize_filename(name: str, max_length: int = 180) -> str:
    value = name.strip()
    value = INVALID_FILENAME_CHARS.sub("_", value)
    value = MULTI_SPACE.sub(" ", value)
    value = value.rstrip(" .")
    if not value:
        value = "untitled"
    stem_upper = value.split(".")[0].upper()
    if stem_upper in WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    if len(value) > max_length:
        value = value[:max_length].rstrip(" .")
    return value or "untitled"


def sanitize_component(value: str, max_length: int = 80) -> str:
    value = sanitize_filename(value, max_length=max_length)
    value = value.replace(" ", "_")
    return value


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def relative_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve().parent))
    except Exception:
        return str(path)


def compute_last_activity_timestamp(path: Path) -> float:
    stat = path.stat()
    return max(stat.st_mtime, getattr(stat, "st_ctime", stat.st_mtime))


def is_path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False
