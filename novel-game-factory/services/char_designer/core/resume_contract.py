"""人物生成的阶段级断点续跑契约。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping

from sqlalchemy import text

from common.config import OUTPUTS_PATH
from common.cg_data import load_cg_contract
from common.platform_db import platform_engine
from common.story_tree_locale import story_tree_selection_metadata
from common.visual_checkpoint import (
    STEP_ASSET_ID,
    canonical_digest,
    checkpoint_is_reusable,
    load_checkpoint,
    mark_success,
)
from common.visual_style import (
    ASSET_CHARACTER,
    load_visual_style_config,
    resolve_asset_style_plan,
    should_remove_character_background,
)
from common.name_mapping_utils import (
    get_change_conversion,
    get_change_name_section,
    get_original_conversion,
)
from services.char_designer.core.artifact_contract import (
    validate_image_file,
    validate_transparent_character_file,
)
from services.char_designer.utils.native_transparency import (
    NATIVE_TRANSPARENCY_CONTRACT_VERSION,
    is_gpt_image_2,
)
from services.char_designer.core.character_manifest import (
    MANIFEST_RELATIVE_PATH,
    validate_character_image_manifest,
)
from services.char_designer.utils.character_identity import (
    apply_authoritative_speaker_id,
    normalize_resource_id,
)


CHARACTER_STAGE_CONTRACT_VERSION = (
    "char-designer-stage-v6-fixed-llm-style-anchor-multilingual-story-tree"
)
CHARACTER_STAGE_STEP = "stage_complete"


def _load_json_table(task_id: str, table: str) -> Any:
    allowed = {
        "characters",
        "character_prompts",
        "cg",
        "story_tree_json",
    }
    if table not in allowed:
        raise ValueError(f"unsupported character resume table: {table}")
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


def _task_image_model(task_id: str) -> str:
    with platform_engine.connect() as connection:
        row = connection.execute(
            text("SELECT image_model FROM novel_tasks WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id},
        ).fetchone()
    return str(row[0] or "") if row else ""


def _requires_transparent_outputs(task_id: str) -> bool:
    # char_designer resolves a missing/unknown task model to gpt-image-2.
    model = _task_image_model(task_id) or "gpt-image-2"
    return is_gpt_image_2(model) or should_remove_character_background(task_id)


def _cg_character_contract(cg_data: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for cg_id, raw_spec in (cg_data or {}).items():
        if not isinstance(raw_spec, Mapping):
            continue
        requirements = []
        for raw in raw_spec.get("reference_requirements") or []:
            if not isinstance(raw, Mapping) or raw.get("role") != "character":
                continue
            requirements.append(
                {
                    key: raw.get(key)
                    for key in (
                        "entity_id",
                        "entity_name",
                        "entity_type",
                        "required",
                        "reference_policy",
                        "speaker_id",
                        "variant",
                        "aliases",
                        "identity_description",
                        "identity_prompt",
                        "preserve",
                        "costume_state",
                    )
                }
            )
        result[str(cg_id)] = {
            "characters": raw_spec.get("characters") or [],
            "cg_visual_entities": raw_spec.get("cg_visual_entities") or [],
            "reference_contract_schema": raw_spec.get("reference_contract_schema"),
            "character_requirements": requirements,
        }
    return result


def _load_name_mappings(task_id: str) -> Dict[str, Any]:
    try:
        with platform_engine.connect() as connection:
            row = connection.execute(
                text("SELECT data FROM name_mappings WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id},
            ).fetchone()
        value = row[0] if row else None
        if isinstance(value, (str, bytes, bytearray)):
            value = json.loads(value)
        return dict(value) if isinstance(value, Mapping) else {}
    except Exception:
        return {}


def _rename_map(task_id: str) -> Dict[str, str]:
    data = _load_name_mappings(task_id)
    original = get_original_conversion(data)
    change = get_change_name_section(data)
    new_names = change.get("new-name")
    new_names = dict(new_names) if isinstance(new_names, Mapping) else {}
    changed = get_change_conversion(data)
    result = {}
    for original_name, old_id in original.items():
        new_name = new_names.get(original_name)
        new_id = changed.get(new_name)
        if old_id and new_id and old_id != new_id:
            try:
                result[normalize_resource_id(old_id, field="original_name.conversion")] = (
                    normalize_resource_id(new_id, field="change_name.conversion")
                )
            except ValueError:
                # The strict finalization step reports the malformed mapping;
                # resume validation must simply decline reuse for this task.
                return {}
    return result


def _active_character_path(
    task_root: Path,
    old_speaker_id: str,
    filename_suffix: str,
    rename_map: Mapping[str, str],
) -> Path | None:
    directory = task_root / "images" / "characters"
    candidates = [directory / f"{old_speaker_id}_{filename_suffix}"]
    new_id = rename_map.get(old_speaker_id)
    if new_id:
        candidates.insert(0, directory / f"{new_id}_{filename_suffix}")
    return next((path for path in candidates if validate_image_file(path)), None)


def validate_public_character_outputs(task_id: str) -> bool:
    task_root = Path(OUTPUTS_PATH).resolve() / task_id
    prompts = _load_json_table(task_id, "character_prompts") or []
    conversion = get_original_conversion(_load_name_mappings(task_id))
    if isinstance(prompts, list):
        prompts = [
            apply_authoritative_speaker_id(item, conversion)
            if isinstance(item, Mapping)
            else item
            for item in prompts
        ]
    cg_data = _load_json_table(task_id, "cg") or {}
    rename_map = _rename_map(task_id)
    public_validator = (
        validate_transparent_character_file
        if _requires_transparent_outputs(task_id)
        else validate_image_file
    )

    if not prompts and not cg_data:
        return True

    if prompts:
        if not isinstance(prompts, list):
            return False
        for record in prompts:
            if not isinstance(record, Mapping):
                return False
            speaker_id = str(record.get("speaker_id") or "").strip()
            combination = str(record.get("combination") or "").strip()
            parts = combination.rsplit("-", 3)
            if not speaker_id or len(parts) != 4 or not all(
                value.isdigit() for value in parts[1:]
            ):
                return False
            suffix = "-".join(parts[1:])
            emotions = record.get("emotions")
            emotions = (
                [str(value) for value in emotions if str(value)]
                if isinstance(emotions, list) and emotions
                else ["calm"]
            )
            if "calm" not in emotions:
                emotions.insert(0, "calm")
            for emotion in emotions:
                for shot in ("LS", "MS", "CS"):
                    filename_suffix = f"{suffix}_{emotion}_{shot}.png"
                    if _active_character_path(
                        task_root,
                        speaker_id,
                        filename_suffix,
                        rename_map,
                    ) is None:
                        return False
                    active = _active_character_path(
                        task_root,
                        speaker_id,
                        filename_suffix,
                        rename_map,
                    )
                    if active is None or not public_validator(active):
                        return False

        manifest_path = task_root / MANIFEST_RELATIVE_PATH
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            validate_character_image_manifest(manifest)
        except Exception:
            return False

    for spec in (cg_data or {}).values():
        if not isinstance(spec, Mapping):
            continue
        for requirement in spec.get("reference_requirements") or []:
            if (
                not isinstance(requirement, Mapping)
                or requirement.get("role") != "character"
                or not requirement.get("required")
            ):
                continue
            candidates = requirement.get("file_candidates") or []
            if not any(
                public_validator(task_root / str(candidate))
                for candidate in candidates
                if str(candidate) and ".." not in Path(str(candidate)).parts
            ):
                return False
    return True


def character_stage_input_digest(task_id: str) -> str:
    story_tree_payload = _load_json_table(task_id, "story_tree_json") or {}
    story_selection = story_tree_selection_metadata(story_tree_payload)
    story_tree = story_selection["tree"]
    style_config = load_visual_style_config(task_id, fetch_remote=True)
    character_style = dict(style_config.get(ASSET_CHARACTER) or {})
    stable_style = {
        key: character_style.get(key)
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
            "contract_version": CHARACTER_STAGE_CONTRACT_VERSION,
            "characters": _load_json_table(task_id, "characters") or {},
            # The upstream name mapping is part of the identity contract.  A
            # changed (including Unicode) speaker ID must invalidate the stage
            # checkpoint even when the character prompt text is unchanged.
            "name_mappings": _load_name_mappings(task_id) or {},
            "character_prompts": _load_json_table(task_id, "character_prompts") or [],
            "cg_character_contract": _cg_character_contract(
                _load_json_table(task_id, "cg") or {}
            ),
            "story_style": {
                "requested_language": story_selection["requested_language"],
                "actual_language": story_selection["actual_language"],
                "publication_fingerprint": (
                    story_selection["publication"].get("fingerprint")
                ),
                "visual_style_context": story_tree.get("visual_style_context"),
                "global_style": story_tree.get("global_style"),
                "global_style_generation": story_tree.get("global_style_generation"),
            },
            "visual_style": {
                "mode": style_config.get("mode"),
                "character": stable_style,
            },
            "image_model": _task_image_model(task_id),
            "remove_background": should_remove_character_background(task_id),
            "transparency_required": _requires_transparent_outputs(task_id),
            "native_transparency_contract": NATIVE_TRANSPARENCY_CONTRACT_VERSION,
        }
    )


def publish_character_stage_checkpoint(task_id: str, *, allow_partial: bool = False) -> Path:
    task_root = Path(OUTPUTS_PATH).resolve() / task_id
    source_dir = task_root / ".resume_sources" / "char_designer" / "ls"
    output_paths = (
        [
            path
            for path in sorted(source_dir.glob("*.png"))
            if (
                validate_transparent_character_file(path)
                if _requires_transparent_outputs(task_id)
                else validate_image_file(path)
            )
        ]
        if source_dir.is_dir()
        else []
    )
    prompts = _load_json_table(task_id, "character_prompts") or []
    cg_data = _load_json_table(task_id, "cg") or {}
    requires_character_sources = bool(prompts)
    if not requires_character_sources:
        requires_character_sources = any(
            isinstance(spec, Mapping)
            and any(
                isinstance(item, Mapping)
                and item.get("role") == "character"
                and item.get("required")
                for item in spec.get("reference_requirements") or []
            )
            for spec in (cg_data or {}).values()
        )
    prompts_require_sources = bool(prompts)
    if requires_character_sources and not output_paths and not allow_partial:
        raise RuntimeError(
            f"char_designer 需要人物资产，但共享恢复源为空: task_id={task_id}"
        )
    if prompts_require_sources and not allow_partial:
        expected_source_count = sum(
            1
            for record in prompts
            if isinstance(record, Mapping)
            and str(record.get("combination") or record.get("speaker_id") or "").strip()
        )
        if len(output_paths) < expected_source_count:
            raise RuntimeError(
                f"char_designer 恢复源不完整: expected>={expected_source_count} "
                f"actual={len(output_paths)} task_id={task_id}"
            )

    # Persist the fixed visual style bundle in the stage output contract.  The
    # resolver remains the authoritative semantic/hash check; recording these
    # paths also lets checkpoint reuse detect deleted Anchors.
    style_config_path = task_root / "style_anchors" / "config.json"
    if style_config_path.is_file() and style_config_path.stat().st_size > 0:
        output_paths.append(style_config_path)
    if requires_character_sources:
        anchor_path = task_root / "style_anchors" / "character_anchor.png"
        if not anchor_path.is_file() or anchor_path.stat().st_size <= 0:
            if not allow_partial:
                raise RuntimeError(
                    f"char_designer 自动 character Anchor 缺失: task_id={task_id}"
                )
        else:
            output_paths.append(anchor_path)

    # A legitimate no-character task still publishes a successful stage row;
    # the reusable style bundle remains part of the fixed visual contract.
    input_digest = character_stage_input_digest(task_id)
    mark_success(
        task_id,
        "char_designer",
        CHARACTER_STAGE_STEP,
        STEP_ASSET_ID,
        input_digest=input_digest,
        generator_version=CHARACTER_STAGE_CONTRACT_VERSION,
        output_paths=output_paths,
        result_mode="stage_bundle_partial" if allow_partial else "stage_bundle",
    )
    return source_dir


def validate_character_stage_checkpoint_partial(task_id: str) -> bool:
    """Accept a completed character stage with a recorded partial report.

    This is used only by scene_painter's CG join.  Strict resume validation
    remains unchanged, so a later replay still audits every expected asset.
    """
    try:
        from services.char_designer.core.asset_failure_report import load_failure_report

        report = load_failure_report(task_id)
    except Exception:
        report = {}
    if not report or report.get("status") != "partial":
        return validate_character_stage_checkpoint(task_id)
    task_root = Path(OUTPUTS_PATH).resolve() / task_id
    valid_outputs = []
    for relative_dir in ("images/characters", "images/references", ".resume_sources/char_designer/ls"):
        directory = task_root / relative_dir
        if directory.is_dir():
            valid_outputs.extend(
                path for path in directory.glob("*.png") if validate_image_file(path)
            )
    # A partial report is an explicit terminal hand-off for the parallel scene
    # branch, including the case where every character asset failed.  Strict
    # validation is still used when deciding whether a future replay may skip
    # this stage.
    return bool(valid_outputs or isinstance(report.get("summary"), Mapping))


def validate_character_stage_checkpoint(task_id: str) -> bool:
    if os.getenv("VISUAL_OUTPUT_DURABILITY", "pvc").strip().lower() == "oss_required":
        try:
            from common.output_mirror import fetch_task_from_oss

            fetch_task_from_oss(task_id)
        except Exception:
            return False
    if not validate_public_character_outputs(task_id):
        return False
    # Require the character Anchor when this task has character sprites or a
    # required CG identity reference; no-character stories remain zero-output.
    prompts = _load_json_table(task_id, "character_prompts") or []
    cg_data = _load_json_table(task_id, "cg") or {}
    requires_anchor = bool(prompts)
    if not requires_anchor and isinstance(cg_data, Mapping):
        requires_anchor = any(
            isinstance(spec, Mapping)
            and any(
                isinstance(item, Mapping)
                and item.get("role") == "character"
                and item.get("required")
                for item in spec.get("reference_requirements") or []
            )
            for spec in cg_data.values()
        )
    if requires_anchor:
        try:
            resolve_asset_style_plan(task_id, ASSET_CHARACTER, fetch_remote=True)
        except Exception:
            return False
    record = load_checkpoint(
        task_id,
        "char_designer",
        CHARACTER_STAGE_STEP,
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
    for path in output_paths:
        if path.is_file():
            continue
        try:
            from common.output_mirror import fetch_output_file

            relative = path.relative_to(task_root)
            fetch_output_file(task_id, relative)
        except Exception:
            pass
    return checkpoint_is_reusable(
        record,
        task_id=task_id,
        input_digest=character_stage_input_digest(task_id),
        generator_version=CHARACTER_STAGE_CONTRACT_VERSION,
        output_paths=output_paths,
    ).reusable
