"""Stage-level resume contract for scene, cover and CG generation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from sqlalchemy import text

from common.config import OUTPUTS_PATH
from common.cg_data import load_cg_contract
from common.platform_db import platform_engine
from common.story_tree_locale import story_tree_selection_metadata
from common.visual_checkpoint import (
    STEP_ASSET_ID,
    canonical_digest,
    checkpoint_is_reusable,
    file_sha256,
    load_checkpoint,
    mark_success,
)
from common.visual_style import (
    ASSET_SCENE,
    load_visual_style_config,
    resolve_asset_style_plan,
)
from services.scene_painter.core.artifact_contract import (
    validate_image_file,
    validate_scene_output_contract,
)


SCENE_STAGE_CONTRACT_VERSION = (
    "scene-painter-stage-v5-fixed-llm-style-anchor-multilingual-story-tree"
)
SCENE_STAGE_STEP = "stage_complete"
_LEGACY_CONTACT_SHEET = re.compile(r"^cg_.+_char_\d+\.png$")


def _load_json_table(task_id: str, table: str) -> Any:
    allowed = {
        "scene_prompts",
        "backgrounds",
        "cg",
        "name_mappings",
        "story_tree_json",
    }
    if table not in allowed:
        raise ValueError(f"unsupported scene resume table: {table}")
    if table == "cg":
        return load_cg_contract(task_id)
    with platform_engine.connect() as connection:
        row = connection.execute(
            text(f"SELECT data FROM {table} WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id},
        ).fetchone()
    value = row[0] if row else None
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    return value


def _load_optional_json_table(task_id: str, table: str) -> Any:
    try:
        return _load_json_table(task_id, table)
    except Exception:
        return None


def _task_image_model(task_id: str) -> str:
    with platform_engine.connect() as connection:
        row = connection.execute(
            text("SELECT image_model FROM novel_tasks WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id},
        ).fetchone()
    return str(row[0] or "") if row else ""


def _character_dependency(task_id: str) -> list[dict[str, Any]]:
    task_root = Path(OUTPUTS_PATH).resolve() / task_id
    items = []
    for relative_dir in ("images/characters", "images/references"):
        directory = task_root / relative_dir
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.png")):
            if relative_dir == "images/characters" and _LEGACY_CONTACT_SHEET.fullmatch(
                path.name
            ):
                continue
            if validate_image_file(path):
                items.append(
                    {
                        "path": path.relative_to(task_root).as_posix(),
                        "sha256": file_sha256(path),
                    }
                )
    return items


def scene_stage_input_digest(task_id: str) -> str:
    story_selection = story_tree_selection_metadata(
        _load_json_table(task_id, "story_tree_json") or {}
    )
    style_config = load_visual_style_config(task_id, fetch_remote=True)
    scene_style = dict(style_config.get(ASSET_SCENE) or {})
    stable_style = {
        key: scene_style.get(key)
        for key in (
            "global_style",
            "source",
            "sha256",
            "requested_model",
            "prompt_version",
            "reference_sha256",
            "status",
            "stale",
        )
    }
    return canonical_digest(
        {
            "contract_version": SCENE_STAGE_CONTRACT_VERSION,
            "story_tree": {
                "requested_language": story_selection["requested_language"],
                "actual_language": story_selection["actual_language"],
                "publication_fingerprint": (
                    story_selection["publication"].get("fingerprint")
                ),
            },
            "scene_prompts": _load_json_table(task_id, "scene_prompts") or {},
            "backgrounds": _load_json_table(task_id, "backgrounds") or {},
            "cg": _load_json_table(task_id, "cg") or {},
            "name_mappings": _load_optional_json_table(
                task_id, "name_mappings"
            ) or {},
            "visual_style": {
                "mode": style_config.get("mode"),
                "scene": stable_style,
            },
            "image_model": _task_image_model(task_id),
            "character_dependencies": _character_dependency(task_id),
        }
    )


def _stage_outputs(task_id: str) -> list[Path]:
    task_root = Path(OUTPUTS_PATH).resolve() / task_id
    paths = []
    for relative_dir in (
        "images/scenes",
        "images/backgrounds",
        "images/cg",
        "images/characters",
        "images/references",
    ):
        directory = task_root / relative_dir
        if directory.is_dir():
            paths.extend(
                path
                for path in sorted(directory.glob("*.png"))
                if not (
                    relative_dir == "images/characters"
                    and _LEGACY_CONTACT_SHEET.fullmatch(path.name)
                )
                if validate_image_file(path)
            )
    for relative in (
        "manifests/character_image_manifest.json",
        "style_anchors/scene_anchor.png",
        "style_anchors/config.json",
    ):
        path = task_root / relative
        if path.is_file() and path.stat().st_size > 0:
            paths.append(path)
    report_path = task_root / "reports" / "scene_painter_asset_failures.json"
    if report_path.is_file() and report_path.stat().st_size > 0:
        paths.append(report_path)
    return paths


def publish_scene_stage_checkpoint(task_id: str, *, allow_partial: bool = False) -> None:
    validate_scene_output_contract(task_id, allow_partial=allow_partial)
    mark_success(
        task_id,
        "scene_painter",
        SCENE_STAGE_STEP,
        STEP_ASSET_ID,
        input_digest=scene_stage_input_digest(task_id),
        generator_version=SCENE_STAGE_CONTRACT_VERSION,
        output_paths=_stage_outputs(task_id),
        result_mode="stage_contract_partial" if allow_partial else "stage_contract",
    )


def validate_scene_stage_checkpoint(task_id: str, *, allow_partial: bool = False) -> bool:
    if os.getenv("VISUAL_OUTPUT_DURABILITY", "pvc").strip().lower() == "oss_required":
        try:
            from common.output_mirror import fetch_task_from_oss

            fetch_task_from_oss(task_id)
        except Exception:
            return False
    try:
        # Anchor/config state is part of the fixed LLM/custom visual contract;
        # a missing, stale, corrupt, or hash-mismatched Anchor must force replay.
        resolve_asset_style_plan(task_id, ASSET_SCENE, fetch_remote=True)
        validate_scene_output_contract(task_id, allow_partial=allow_partial)
    except Exception:
        return False
    record = load_checkpoint(
        task_id,
        "scene_painter",
        SCENE_STAGE_STEP,
        STEP_ASSET_ID,
    )
    if record is None:
        return False
    task_root = Path(OUTPUTS_PATH).resolve() / task_id
    output_paths = [
        task_root / str(item.get("path") or "")
        for item in (record.output_contract.get("outputs") or [])
        if isinstance(item, Mapping) and str(item.get("path") or "")
    ]
    reusable = checkpoint_is_reusable(
        record,
        task_id=task_id,
        input_digest=scene_stage_input_digest(task_id),
        generator_version=SCENE_STAGE_CONTRACT_VERSION,
        output_paths=output_paths,
    ).reusable
    return reusable
