"""Local storage helpers for the local-only asset store."""

from __future__ import annotations

import mimetypes
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote


def oss_disabled() -> bool:
    """All platform assets are persisted and served from local storage."""
    return True


def storage_root() -> Path:
    configured = os.getenv("STORAGE_PATH")
    if configured:
        return Path(configured).expanduser()
    desktop_root = Path(__file__).resolve().parents[2] / "novel-game-factory" / "storage"
    return desktop_root if desktop_root.exists() else Path("/storage")


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


def local_output_prefix_for_asset(prefix: str) -> str:
    """Map the legacy shared-asset prefix to local production output paths."""
    mapping = {
        "image-backups/character/": "images/characters",
        "image-backups/sence/": "images/scenes",
        "sound-backups/bgm/": "audio/music",
        "sound-backups/sfx/": "audio/sounds",
    }
    try:
        return mapping[prefix]
    except KeyError as exc:
        raise ValueError(f"未知本地素材分类: {prefix}") from exc
