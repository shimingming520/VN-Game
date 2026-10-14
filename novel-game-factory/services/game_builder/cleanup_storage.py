"""Best-effort cleanup for old local game-builder outputs.

The standalone Docker deployment keeps generated files on the mounted
``storage`` directory.  Cleanup is intentionally filesystem-only and must
never prevent a game build from running.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Set


def _normalise_ids(values: Iterable[object] | None) -> Set[str]:
    return {str(value).strip() for value in (values or ()) if str(value).strip()}


def _active_task_ids() -> Set[str]:
    """Return task ids visible to the local Celery worker, if available."""
    try:
        from common.celery_app import celery_app

        inspector = celery_app.control.inspect(timeout=0.5)
        active: Mapping[str, list[dict[str, Any]]] = inspector.active() or {}
        reserved: Mapping[str, list[dict[str, Any]]] = inspector.reserved() or {}
        scheduled: Mapping[str, list[dict[str, Any]]] = inspector.scheduled() or {}
        task_ids: Set[str] = set()
        for worker_tasks in (*active.values(), *reserved.values()):
            for task in worker_tasks or []:
                task_id = task.get("id") or task.get("request", {}).get("id")
                if task_id:
                    task_ids.add(str(task_id))
        for worker_tasks in scheduled.values():
            for entry in worker_tasks or []:
                request = entry.get("request") or {}
                task_id = request.get("id") or entry.get("id")
                if task_id:
                    task_ids.add(str(task_id))
        return task_ids
    except Exception:
        return set()


def cleanup_old_outputs(
    storage_root: str | Path,
    *,
    days: float = 7,
    protected_task_ids: Iterable[object] | None = None,
    include_active_tasks: bool = True,
) -> dict[str, list[str]]:
    """Delete expired task directories under ``<storage_root>/outputs``.

    Only directories whose modification time is older than ``days`` are
    candidates.  The current task and any task visible to Celery are protected.
    Errors are returned as warnings rather than raised.
    """
    root = Path(storage_root)
    outputs_dir = root / "outputs"
    result: dict[str, list[str]] = {
        "candidates": [],
        "deleted": [],
        "warnings": [],
    }
    if not outputs_dir.is_dir():
        return result

    protected = _normalise_ids(protected_task_ids)
    if include_active_tasks:
        protected.update(_active_task_ids())

    cutoff = time.time() - (max(float(days), 0.0) * 86400)
    try:
        entries = list(outputs_dir.iterdir())
    except OSError as exc:
        result["warnings"].append(f"无法读取 outputs 目录: {exc}")
        return result

    for entry in entries:
        if not entry.is_dir() or entry.name in protected:
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            result["candidates"].append(entry.name)
            for child in sorted(entry.rglob("*"), reverse=True):
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    child.rmdir()
            entry.rmdir()
            result["deleted"].append(entry.name)
        except OSError as exc:
            result["warnings"].append(f"{entry.name}: {exc}")

    return result
