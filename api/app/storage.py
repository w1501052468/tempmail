import os
import re
from pathlib import Path

from .config import get_settings

FILENAME_SANITIZER = re.compile(r"[^A-Za-z0-9._-]+")


def get_data_root() -> Path:
    return Path(get_settings().data_dir)


def ensure_storage_dirs() -> None:
    root = get_data_root()
    (root / "raw").mkdir(parents=True, exist_ok=True)
    (root / "attachments").mkdir(parents=True, exist_ok=True)


def sanitize_filename(filename: str | None, fallback: str) -> str:
    cleaned = FILENAME_SANITIZER.sub("_", (filename or "").strip())
    return cleaned[:200] or fallback


def write_bytes(relative_path: str, content: bytes) -> None:
    root = get_data_root()
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def remove_relative_path(relative_path: str | None) -> None:
    if not relative_path:
        return
    target = get_data_root() / relative_path
    try:
        target.unlink(missing_ok=True)
    except TypeError:
        if target.exists():
            target.unlink()
    parent = target.parent
    root = get_data_root()
    while parent != root and parent.exists():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def resolve_relative_path(relative_path: str) -> Path:
    root = get_data_root().resolve()
    target = (root / relative_path).resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise ValueError("Resolved path escaped data directory")
    return target

