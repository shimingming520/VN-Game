"""Local storage helpers used when OSS is explicitly disabled."""

from __future__ import annotations

import mimetypes
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote


_TRUE_VALUES = {"1", "true", "yes", "on"}


def oss_disabled() -> bool:
    """All creation-engine assets are persisted and served from local storage."""
    return True


def storage_root() -> Path:
    return Path(os.getenv("STORAGE_PATH", "/storage")).expanduser()


def local_path_for_key(key: str) -> Path:
    normalized = str(key).replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"非法本地素材 key: {key}")
    if parts[0] == "storage":
        parts = parts[1:]

    root = storage_root().resolve()
    path = root.joinpath(*parts).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"本地素材路径越界: {key}") from exc
    return path


def local_asset_url(key: str) -> str:
    normalized = str(key).replace("\\", "/").lstrip("/")
    return f"/api/v1/local-assets/{quote(normalized, safe='/')}"


def local_asset_record(key: str, path: Path) -> dict:
    stat = path.stat()
    return {
        "key": key,
        "name": path.name,
        "url": local_asset_url(key),
        "size": stat.st_size,
        "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }


def local_media_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
