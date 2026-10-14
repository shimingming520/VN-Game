"""Local-only generation audit records for the Style Anchor experiment UI.

The records are deliberately file based and live beside task outputs. They do
not add a database table, alter Provider payloads, or participate in OSS
mirroring. Generation services call this module only to record the final prompt,
the actual image-to-image references, and a short explanation after a local
asset has been saved successfully.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from common.config import OUTPUTS_PATH
from common.local_runtime import env_flag


DEBUG_REQUIRED_FLAGS = (
    "STYLE_ANCHOR_REUSE_ENABLED",
    "LOCAL_PIPELINE_MODE",
    "OSS_DISABLED",
)
DEBUG_RELATIVE_DIR = Path("style_anchors/debug_assets")
DEBUG_CATEGORIES = {"characters", "scenes", "cg", "backgrounds"}


def visual_debug_enabled() -> bool:
    """Return whether the isolated local experiment audit is enabled."""
    return all(env_flag(name) for name in DEBUG_REQUIRED_FLAGS)


def _safe_component(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or Path(text).name != text
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
    ):
        raise ValueError(f"非法 {field_name}: {value}")
    return text


def _task_root(task_id: str) -> Path:
    return Path(OUTPUTS_PATH).resolve() / _safe_component(task_id, "task_id")


def debug_record_path(task_id: str, category: str, filename: str) -> Path:
    category = _safe_component(category, "category")
    if category not in DEBUG_CATEGORIES:
        raise ValueError(f"不支持的审计分类: {category}")
    filename = _safe_component(filename, "filename")
    return _task_root(task_id) / DEBUG_RELATIVE_DIR / category / f"{filename}.json"


def _relative_reference(task_root: Path, reference: Any) -> Dict[str, Any]:
    role = "参考图"
    path_value: Any = reference
    if isinstance(reference, Mapping):
        path_value = reference.get("path")
        role = str(reference.get("role") or role).strip()

    path = Path(path_value) if path_value is not None else Path("")
    exists = bool(path_value and path.exists() and path.is_file())
    relative_path = ""
    if path_value:
        try:
            relative_path = path.resolve().relative_to(task_root.resolve()).as_posix()
        except (OSError, ValueError):
            relative_path = ""
    return {
        "name": path.name if path_value else "",
        "role": role,
        "relative_path": relative_path,
        "exists": exists,
    }


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def record_visual_generation(
    task_id: str,
    category: str,
    output_path: str | Path,
    *,
    prompt: str,
    generation_mode: str,
    reference_images: Iterable[Any] = (),
    logic: str | Iterable[str] = (),
    model: str = "",
    provider: str = "",
    extra: Optional[Mapping[str, Any]] = None,
) -> Optional[Path]:
    """Persist one successful local generation record.

    The function is intentionally a no-op outside the isolated experiment
    runtime, so shared generation modules keep their production behaviour.
    """
    if not visual_debug_enabled():
        return None

    output = Path(output_path)
    filename = _safe_component(output.name, "filename")
    task_root = _task_root(task_id)
    if isinstance(logic, str):
        logic_steps = [logic] if logic.strip() else []
    else:
        logic_steps = [str(step).strip() for step in logic if str(step).strip()]

    payload: Dict[str, Any] = {
        "version": 1,
        "source": "recorded",
        "task_id": _safe_component(task_id, "task_id"),
        "category": category,
        "filename": filename,
        "generation_mode": str(generation_mode or "unknown").strip(),
        "prompt": str(prompt or "").strip(),
        "reference_images": [
            _relative_reference(task_root, reference) for reference in reference_images
        ],
        "logic": logic_steps,
        "model": str(model or "").strip(),
        "provider": str(provider or "").strip(),
        "recorded_at": datetime.now().astimezone().isoformat(),
    }
    if extra:
        payload["extra"] = dict(extra)

    target = debug_record_path(task_id, category, filename)
    _write_json_atomic(target, payload)
    return target


def load_visual_generation_record(
    task_id: str,
    category: str,
    filename: str,
) -> Optional[Dict[str, Any]]:
    """Load one recorded audit entry, returning ``None`` when absent."""
    path = debug_record_path(task_id, category, filename)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def record_derived_visual_asset(
    task_id: str,
    category: str,
    output_path: str | Path,
    source_path: str | Path,
    *,
    logic: str,
    generation_mode: str,
) -> Optional[Path]:
    """Record a crop/copy asset while inheriting its source prompt."""
    source = Path(source_path)
    source_record = load_visual_generation_record(task_id, category, source.name) or {}
    extra = dict(source_record.get("extra") or {})
    extra["derived_from"] = source.name
    return record_visual_generation(
        task_id,
        category,
        output_path,
        prompt=str(source_record.get("prompt") or ""),
        generation_mode=generation_mode,
        reference_images=[{"path": source, "role": "直接来源图"}],
        logic=logic,
        model=str(source_record.get("model") or ""),
        provider=str(source_record.get("provider") or "local_postprocess"),
        extra=extra,
    )
