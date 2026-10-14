"""Scene/CG asset failure report writer.

This is intentionally service-local and file based.  The scene worker already
publishes under the shared task output directory, so no database or platform
schema change is required to expose partial visual results to operators.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from common.config import OUTPUTS_PATH


REPORT_FILENAME = "scene_painter_asset_failures.json"
REPORT_SCHEMA_VERSION = "visual-asset-failure-v1"


def _max_attempts() -> int:
    try:
        return max(1, int(os.getenv("VISUAL_ASSET_MAX_FAILED_ATTEMPTS", "8")))
    except (TypeError, ValueError):
        return 8


def report_path(task_id: str, *, outputs_root: str | Path = OUTPUTS_PATH) -> Path:
    task = str(task_id or "").strip()
    if not task or task in {".", ".."} or Path(task).name != task:
        raise ValueError(f"unsafe task_id: {task_id!r}")
    if str(outputs_root) == str(OUTPUTS_PATH) and os.getenv("STORAGE_PATH"):
        outputs_root = Path(os.getenv("STORAGE_PATH", "")) / "outputs"
    root = Path(outputs_root).resolve()
    task_root = (root / task).resolve()
    task_root.relative_to(root)
    return task_root / "reports" / REPORT_FILENAME


def _execution_metadata() -> tuple[str, int]:
    execution_id = str(os.getenv("NOVEL_GAME_EXECUTION_ID", "") or "").strip()
    raw_attempt = str(os.getenv("NOVEL_GAME_PLATFORM_ATTEMPT", "0") or "0").strip()
    if not execution_id:
        try:
            from celery import current_task

            request = getattr(current_task, "request", None)
            execution_id = str(getattr(request, "id", "") or "")
            headers = getattr(request, "headers", None) or {}
            if raw_attempt in {"", "0"}:
                raw_attempt = str(headers.get("platform_attempt") or "0")
        except Exception:
            pass
    try:
        attempt = max(0, int(raw_attempt or 0))
    except (TypeError, ValueError):
        attempt = 0
    return execution_id, attempt


def _safe_error(value: Any) -> str:
    message = str(value or "")
    message = re.sub(
        r"(?i)(api[_-]?key|authorization|bearer|token)(\s*[:=]\s*)\S+",
        r"\1\2<redacted>",
        message,
    )
    return message[:1200]


def classify_reason(error: Any, *, stage: str = "") -> str:
    text = f"{stage} {error}".lower()
    if any(word in text for word in ("moderation", "审核", "safety", "content policy")):
        return "content_moderation"
    if any(word in text for word in ("reference", "参考图", "人物资源", "required")):
        return "required_reference_missing"
    if any(word in text for word in ("fallback", "兜底", "父级")):
        return "fallback_copy"
    if any(word in text for word in ("prompt", "提示词")):
        return "missing_prompt"
    if any(word in text for word in ("timeout", "超时")):
        return "provider_timeout"
    if any(word in text for word in ("retry", "重试", "上限")):
        return "retry_exhausted"
    if any(word in text for word in ("http", "provider", "api", "network", "连接")):
        return "provider_error"
    return "asset_generation_error"


def _checkpoint_failures(task_id: str) -> list[dict[str, Any]]:
    try:
        from sqlalchemy import text

        from common.platform_db import platform_engine
        from common.visual_checkpoint import ensure_visual_checkpoint_schema

        ensure_visual_checkpoint_schema(platform_engine)
        table = (
            "visual_asset_checkpoints"
            if platform_engine.dialect.name == "sqlite"
            else "public.visual_asset_checkpoints"
        )
        with platform_engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT stage, step_name, asset_id, status, retry_count, "
                    f"last_error, execution_id, platform_attempt, result_mode, "
                    f"output_contract FROM {table} WHERE task_id = :task_id "
                    "AND (status = 'failed' OR result_mode = 'fallback_copy') "
                    "ORDER BY updated_at"
                ),
                {"task_id": task_id},
            ).all()
    except Exception as exc:
        raise RuntimeError(
            f"读取 scene_painter visual checkpoint 失败: {exc}"
        ) from exc

    items: list[dict[str, Any]] = []
    for row in rows:
        contract = row[9]
        if isinstance(contract, (str, bytes, bytearray)):
            try:
                contract = json.loads(contract)
            except (TypeError, ValueError):
                contract = {}
        paths = [
            str(item.get("path") or "")
            for item in (contract.get("outputs") or [] if isinstance(contract, Mapping) else [])
            if isinstance(item, Mapping) and str(item.get("path") or "")
        ]
        stage = str(row[0] or "")
        is_fallback = str(row[8] or "") == "fallback_copy"
        items.append(
            {
                "service": "scene_painter",
                "stage": stage,
                "step": str(row[1] or ""),
                "asset_id": str(row[2] or ""),
                "asset_type": "cg" if "cg" in stage else "scene",
                "status": "fallback" if is_fallback else "missing",
                "reason_code": "fallback_copy" if is_fallback else classify_reason(row[5], stage=stage),
                "attempts": int(row[4] or 0),
                "max_attempts": _max_attempts(),
                "paths": paths,
                "last_error": _safe_error(row[5]) if not is_fallback else "copied from a valid fallback source",
                "execution_id": str(row[6] or ""),
                "platform_attempt": int(row[7] or 0),
                "result_mode": str(row[8] or ""),
            }
        )
    return items


def _load_json_table(task_id: str, table: str) -> Any:
    try:
        from sqlalchemy import text

        from common.platform_db import platform_engine

        with platform_engine.connect() as connection:
            row = connection.execute(
                text(f"SELECT data FROM {table} WHERE task_id = :task_id "
                     "ORDER BY id DESC LIMIT 1"),
                {"task_id": task_id},
            ).first()
        value = row[0] if row else None
        if isinstance(value, (str, bytes, bytearray)):
            value = json.loads(value)
        return value
    except Exception as exc:
        raise RuntimeError(
            f"读取 scene_painter 资产输入表 {table} 失败: {exc}"
        ) from exc


def _expected_assets(task_id: str, *, include_cg: bool = True) -> list[dict[str, str]]:
    expected: list[dict[str, str]] = []
    scene_prompts = _load_json_table(task_id, "scene_prompts")
    if isinstance(scene_prompts, Mapping):
        for scene_id, spec in scene_prompts.items():
            if str(scene_id).startswith("cg_") or not isinstance(spec, Mapping):
                continue
            safe_id = re.sub(r"[^\w\-]", "_", str(scene_id))
            expected.append(
                {
                    "asset_id": str(scene_id),
                    "asset_type": "scene",
                    "stage": (
                        "scene_variant"
                        if re.search(r"_\d+$", str(scene_id))
                        else "scene_base"
                    ),
                    "path": f"images/scenes/{safe_id}.png",
                }
            )
            try:
                copy_count = max(0, int(spec.get("copy_count", 0) or 0))
            except (TypeError, ValueError):
                copy_count = 0
            for index in range(1, copy_count + 1):
                instance = re.sub(r"[^\w\-]", "_", f"{scene_id}_{index}")
                expected.append(
                    {
                        "asset_id": instance,
                        "asset_type": "scene_instance",
                        "stage": "scene_instances",
                        "path": f"images/scenes/{instance}.png",
                    }
                )

    backgrounds = _load_json_table(task_id, "backgrounds")
    if isinstance(backgrounds, Mapping):
        for node_id in (backgrounds.get("generated_prompts") or {}):
            expected.append(
                {
                    "asset_id": str(node_id),
                    "asset_type": "chapter_cover",
                    "stage": "generate_cover",
                    "path": f"images/backgrounds/{node_id}.png",
                }
            )
        metadata = backgrounds.get("novel_cover_metadata") or {}
        if isinstance(metadata, Mapping) and str(metadata.get("prompt") or "").strip():
            expected.append(
                {
                    "asset_id": "novel_cover",
                    "asset_type": "novel_cover",
                    "stage": "generate_cover",
                    "path": "images/backgrounds/novel_cover.png",
                }
            )

    cg_data = _load_json_table(task_id, "cg") if include_cg else None
    if isinstance(cg_data, Mapping):
        for cg_id, spec in cg_data.items():
            if isinstance(spec, Mapping) and str(spec.get("cg_prompt") or "").strip():
                safe_id = re.sub(r"[^\w\-]", "_", str(cg_id))
                expected.append(
                    {
                        "asset_id": str(cg_id),
                        "asset_type": "cg",
                        "stage": "cg",
                        "path": f"images/cg/{safe_id}.png",
                    }
                )
    return expected


def _valid_image(task_root: Path, relative_path: str) -> bool:
    try:
        from services.scene_painter.core.artifact_contract import validate_image_file

        return validate_image_file(task_root / Path(*relative_path.split("/")))
    except Exception:
        return False


def _deduplicate(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in items:
        item = dict(raw)
        key = (
            str(item.get("stage") or ""),
            str(item.get("step") or ""),
            str(item.get("asset_id") or ""),
        )
        if key != ("", "", ""):
            result[key] = item
    return list(result.values())


def write_failure_report(
    task_id: str,
    *,
    extra_items: Optional[Iterable[Mapping[str, Any]]] = None,
    expected: int = 0,
    generated: int = 0,
    fallback: int = 0,
    blocked: int = 0,
    include_cg: bool = True,
    outputs_root: str | Path = OUTPUTS_PATH,
) -> Path:
    execution_id, platform_attempt = _execution_metadata()
    task_root = Path(outputs_root).resolve() / task_id
    checkpoint_items = _checkpoint_failures(task_id)
    items = list(checkpoint_items)
    items.extend(dict(item) for item in (extra_items or []))
    expected_assets = _expected_assets(task_id, include_cg=include_cg)
    checkpoint_ids = {str(item.get("asset_id") or "") for item in checkpoint_items}
    for expected_asset in expected_assets:
        relative_path = str(expected_asset.get("path") or "")
        if not relative_path or _valid_image(task_root, relative_path):
            continue
        if str(expected_asset.get("asset_id") or "") in checkpoint_ids:
            continue
        stage = str(expected_asset.get("stage") or "")
        items.append(
            {
                "service": "scene_painter",
                "stage": stage,
                "step": stage,
                "asset_id": str(expected_asset.get("asset_id") or relative_path),
                "asset_type": str(expected_asset.get("asset_type") or "scene"),
                "status": "missing",
                "reason_code": "not_generated",
                "attempts": 0,
                "max_attempts": _max_attempts(),
                "paths": [relative_path],
                "last_error": "expected output was not published",
                "execution_id": execution_id,
                "platform_attempt": platform_attempt,
                "result_mode": "",
            }
        )
    assets = _deduplicate(items)
    expected_count = len(expected_assets) if expected_assets else max(0, int(expected))
    generated_count = (
        sum(1 for item in expected_assets if _valid_image(task_root, str(item.get("path") or "")))
        if expected_assets
        else max(0, int(generated))
    )
    missing = sum(1 for item in assets if item.get("status") == "missing")
    fallback_count = sum(1 for item in assets if item.get("status") == "fallback")
    blocked_count = sum(1 for item in assets if item.get("status") == "blocked")
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "task_id": task_id,
        "service": "scene_painter",
        "status": "partial" if assets else "success",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execution_id": execution_id,
        "platform_attempt": platform_attempt,
        "summary": {
            "expected": expected_count,
            "generated": generated_count,
            "fallback": max(fallback_count, int(fallback)),
            "blocked": max(blocked_count, int(blocked)),
            "missing": missing,
        },
        "assets": assets,
    }
    destination = report_path(task_id, outputs_root=outputs_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".part",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
    return destination


def load_failure_report(task_id: str, *, outputs_root: str | Path = OUTPUTS_PATH) -> dict[str, Any]:
    try:
        value = json.loads(report_path(task_id, outputs_root=outputs_root).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}
