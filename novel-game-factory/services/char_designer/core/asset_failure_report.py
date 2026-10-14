"""尽力而为（best-effort）视觉生成中的人物素材失败报告。

该报告有意采用基于文件的方式：人物与场景 worker 可能同时运行，而共享的输出 PVC
已经是跨 pod 的产物存储。本模块不改变 checkpoint 结构或平台任务状态机。
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
from common.name_mapping_utils import get_original_conversion
from services.char_designer.utils.character_identity import (
    apply_authoritative_speaker_id,
    normalize_resource_id,
)


REPORT_FILENAME = "char_designer_asset_failures.json"
REPORT_SCHEMA_VERSION = "visual-asset-failure-v1"
_VALID_EMOTIONS = {
    "calm",
    "happy",
    "sad",
    "angry",
    "fearful",
    "disgusted",
    "surprised",
}


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


def classify_reason(error: Any, *, step_name: str = "") -> str:
    text = f"{step_name} {error}".lower()
    if any(word in text for word in ("moderation", "审核", "safety", "content policy")):
        return "content_moderation"
    if any(word in text for word in ("rgba", "alpha", "transparen")):
        return "rgba_validation"
    if any(word in text for word in ("prompt", "提示词")):
        return "missing_prompt"
    if any(word in text for word in ("depend", "基础图片", "基础图", "source")):
        return "dependency_missing"
    if any(word in text for word in ("timeout", "超时")):
        return "provider_timeout"
    if any(word in text for word in ("retry budget", "retry exhausted", "重试", "上限")):
        return "retry_exhausted"
    if any(word in text for word in ("http", "provider", "api", "network", "连接")):
        return "provider_error"
    return "asset_generation_error"


def _checkpoint_failures(task_id: str) -> list[dict[str, Any]]:
    """Read failed visual checkpoints when the platform DB is available."""
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
                    "AND status = 'failed' ORDER BY updated_at"
                ),
                {"task_id": task_id},
            ).all()
    except Exception as exc:
        raise RuntimeError(
            f"读取 char_designer visual checkpoint 失败: {exc}"
        ) from exc

    items: list[dict[str, Any]] = []
    for row in rows:
        output_contract = row[9]
        if isinstance(output_contract, (str, bytes, bytearray)):
            try:
                output_contract = json.loads(output_contract)
            except (TypeError, ValueError):
                output_contract = {}
        outputs = []
        if isinstance(output_contract, Mapping):
            outputs = [
                str(item.get("path") or "")
                for item in output_contract.get("outputs") or []
                if isinstance(item, Mapping) and str(item.get("path") or "")
            ]
        step_name = str(row[1] or "")
        asset_id = str(row[2] or "")
        items.append(
            {
                "service": "char_designer",
                "stage": str(row[0] or ""),
                "step": step_name,
                "asset_id": asset_id,
                "asset_type": "character",
                "status": "missing",
                "reason_code": classify_reason(row[5], step_name=step_name),
                "attempts": int(row[4] or 0),
                "max_attempts": _max_attempts(),
                "paths": outputs,
                "last_error": _safe_error(row[5]),
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
            f"读取 char_designer 资产输入表 {table} 失败: {exc}"
        ) from exc


def _expected_assets(task_id: str) -> list[dict[str, str]]:
    """Build expected public paths from the same upstream contracts as generators."""
    records = _load_json_table(task_id, "character_prompts")
    try:
        conversion = get_original_conversion(
            _load_json_table(task_id, "name_mappings")
        )
    except Exception:
        conversion = {}
    if isinstance(records, list):
        records = [
            apply_authoritative_speaker_id(item, conversion)
            if isinstance(item, Mapping)
            else item
            for item in records
        ]
    expected: list[dict[str, str]] = []
    speaker_candidates: dict[str, list[str]] = {}
    try:
        from services.char_designer.core.resume_contract import _rename_map

        rename_map = _rename_map(task_id)
        for old_id, new_id in rename_map.items():
            speaker_candidates.setdefault(str(old_id), [str(old_id)]).append(str(new_id))
            speaker_candidates.setdefault(str(new_id), [str(new_id)]).append(str(old_id))
    except Exception:
        rename_map = {}

    if isinstance(records, list):
        for record in records:
            if not isinstance(record, Mapping):
                continue
            combination = str(record.get("combination") or "").strip()
            speaker_id = str(record.get("speaker_id") or "").strip()
            parts = combination.rsplit("-", 3)
            if len(parts) != 4 or not speaker_id or not all(p.isdigit() for p in parts[1:]):
                continue
            suffix = "-".join(parts[1:])
            emotions = record.get("emotions")
            if not isinstance(emotions, list) or not emotions:
                emotions = ["calm"]
            if "calm" not in emotions:
                emotions = ["calm", *emotions]
            for emotion in dict.fromkeys(
                str(value)
                for value in emotions
                if str(value) in _VALID_EMOTIONS
            ):
                for shot in ("LS", "MS", "CS"):
                    candidates = list(
                        dict.fromkeys(
                            speaker_candidates.get(speaker_id, [speaker_id])
                        )
                    )
                    expected.append(
                        {
                            "asset_id": f"{speaker_id}_{suffix}_{emotion}_{shot}",
                            "asset_type": "character",
                            "stage": (
                                "generate_shots"
                                if shot != "LS"
                                else ("generate_emotion" if emotion != "calm" else "generate_base_calm")
                            ),
                            "path": f"images/characters/{speaker_id}_{suffix}_{emotion}_{shot}.png",
                            "candidate_paths": "|".join(
                                f"images/characters/{candidate}_{suffix}_{emotion}_{shot}.png"
                                for candidate in candidates
                            ),
                        }
                    )

    cg_data = _load_json_table(task_id, "cg")
    if isinstance(cg_data, Mapping):
        for spec in cg_data.values():
            if not isinstance(spec, Mapping):
                continue
            for requirement in spec.get("reference_requirements") or []:
                if not isinstance(requirement, Mapping) or not requirement.get("required"):
                    continue
                if requirement.get("role") != "character":
                    continue
                raw_entity_id = requirement.get("entity_id") or "CgIdentity"
                try:
                    entity_id = normalize_resource_id(
                        raw_entity_id, field="CG entity_id"
                    )
                except ValueError:
                    # Keep the report writable even when upstream supplied an
                    # unsafe identity.  The generator itself remains fail-closed;
                    # this fallback is only a diagnostic filename.
                    entity_id = "CgIdentity"
                expected.append(
                    {
                        "asset_id": entity_id,
                        "asset_type": "cg_identity",
                        "stage": "generate_cg_identity",
                        "path": f"images/references/{entity_id}.png",
                    }
                )
    return expected


def _valid_image(task_root: Path, relative_path: str) -> bool:
    try:
        from services.char_designer.core.artifact_contract import validate_image_file

        return validate_image_file(task_root / Path(*relative_path.split("/")))
    except Exception:
        return False


def _valid_expected_asset(task_root: Path, expected: Mapping[str, Any]) -> bool:
    candidates = str(expected.get("candidate_paths") or expected.get("path") or "")
    return any(_valid_image(task_root, value) for value in candidates.split("|") if value)


def _deduplicate(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in items:
        item = dict(raw)
        key = (
            str(item.get("stage") or ""),
            str(item.get("step") or ""),
            str(item.get("asset_id") or ""),
        )
        if key == ("", "", ""):
            continue
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
    outputs_root: str | Path = OUTPUTS_PATH,
) -> Path:
    """Atomically publish the current unresolved character-asset report."""
    execution_id, platform_attempt = _execution_metadata()
    task_root = Path(outputs_root).resolve() / task_id
    checkpoint_items = _checkpoint_failures(task_id)
    items = list(checkpoint_items)
    items.extend(dict(item) for item in (extra_items or []))
    expected_assets = _expected_assets(task_id)
    checkpoint_by_path = {
        path
        : item
        for item in checkpoint_items
        for path in (item.get("paths") or [])
        if path
    }
    for expected_asset in expected_assets:
        relative_path = str(expected_asset.get("path") or "")
        if not relative_path or _valid_expected_asset(task_root, expected_asset):
            continue
        if relative_path in checkpoint_by_path:
            continue
        stage = str(expected_asset.get("stage") or "")
        items.append(
            {
                "service": "char_designer",
                "stage": stage,
                "step": stage,
                "asset_id": str(expected_asset.get("asset_id") or relative_path),
                "asset_type": str(expected_asset.get("asset_type") or "character"),
                "status": "missing",
                "reason_code": "not_generated",
                "attempts": 0,
                "max_attempts": 8,
                "paths": [relative_path],
                "last_error": "expected output was not published",
                "execution_id": execution_id,
                "platform_attempt": platform_attempt,
                "result_mode": "",
            }
        )
    assets = _deduplicate(items)
    missing = sum(1 for item in assets if item.get("status") == "missing")
    fallback_count = sum(1 for item in assets if item.get("status") == "fallback")
    blocked_count = sum(1 for item in assets if item.get("status") == "blocked")
    expected_count = len(expected_assets) if expected_assets else max(0, int(expected))
    generated_count = sum(
        1
        for item in expected_assets
        if _valid_expected_asset(task_root, item)
    ) if expected_assets else max(0, int(generated))
    unresolved = [
        item for item in assets
        if item.get("status") in {"missing", "blocked", "fallback"}
    ]
    status = "partial" if unresolved else "success"
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "task_id": task_id,
        "service": "char_designer",
        "status": status,
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


def load_failure_report(
    task_id: str, *, outputs_root: str | Path = OUTPUTS_PATH
) -> dict[str, Any]:
    path = report_path(task_id, outputs_root=outputs_root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}
