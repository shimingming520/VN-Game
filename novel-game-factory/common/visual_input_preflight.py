"""Fail-closed validation before any delayed visual-style Provider call."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Set

from sqlalchemy import text

from common.platform_db import platform_engine
from common.cg_data import load_cg_contract
from common.global_style import VISUAL_STYLE_CONTEXT_SCHEMA
from common.story_tree_locale import select_story_tree_for_consumer
from common.task_models import ImageModel, TaskStatus


CG_REFERENCE_CONTRACT_SCHEMA = "cg-reference-v1"


class VisualInputPreflightError(ValueError):
    """Upstream visual inputs are incomplete or internally inconsistent."""


@dataclass(frozen=True)
class VisualInputPreflightReport:
    task_id: str
    stage: str
    image_model: str
    character_prompt_count: int
    scene_prompt_count: int
    cg_count: int
    needs_character_anchor: bool


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


def _fetch_data(conn, table_name: str, task_id: str, default: Any) -> Any:
    row = conn.execute(
        text(f"SELECT data FROM {table_name} WHERE task_id = :tid LIMIT 1"),
        {"tid": task_id},
    ).fetchone()
    return _json_value(row[0], default) if row else default


def _character_records(characters: Any) -> Iterable[Mapping[str, Any]]:
    if not isinstance(characters, Mapping):
        return
    for raw in characters.values():
        entries = raw if isinstance(raw, list) else [raw]
        for item in entries:
            if isinstance(item, Mapping):
                yield item


def _active_character_combinations(characters: Any) -> Set[str]:
    combinations: Set[str] = set()
    for character in _character_records(characters):
        for raw in character.get("valid_status_combinations") or []:
            if not isinstance(raw, Mapping) or not raw.get("is_active"):
                continue
            combination = str(raw.get("combination") or "").strip()
            if combination:
                combinations.add(combination)
    return combinations


def _validate_character_prompts(raw: Any, errors: List[str]) -> Set[str]:
    if not isinstance(raw, list):
        errors.append("character_prompts.data 必须是 JSON array")
        return set()
    combinations: Set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            errors.append(f"character_prompts[{index}] 不是 JSON object")
            continue
        prompt = str(item.get("positive_prompt") or "").strip()
        combination = str(
            item.get("combination") or item.get("speaker_id") or ""
        ).strip()
        if not prompt:
            errors.append(f"character_prompts[{index}] positive_prompt 为空")
        if not combination:
            errors.append(f"character_prompts[{index}] 缺少 combination/speaker_id")
        else:
            combinations.add(combination)
    return combinations


def _validate_scene_prompts(raw: Any, errors: List[str]) -> int:
    if not isinstance(raw, Mapping):
        errors.append("scene_prompts.data 必须是非空 JSON object")
        return 0
    valid = 0
    for scene_id, item in raw.items():
        if not isinstance(item, Mapping):
            errors.append(f"scene_prompts[{scene_id}] 不是 JSON object")
            continue
        if not str(item.get("image_prompt") or "").strip():
            errors.append(f"scene_prompts[{scene_id}].image_prompt 为空")
            continue
        valid += 1
    if valid == 0:
        errors.append("scene_prompts 没有任何有效场景提示词")
    return valid


def _plot_combinations(plots: Any) -> Set[str]:
    combinations: Set[str] = set()
    if not isinstance(plots, Mapping):
        return combinations
    for beats in plots.values():
        if not isinstance(beats, list):
            continue
        for beat in beats:
            if not isinstance(beat, Mapping):
                continue
            combination = str(beat.get("speaker_combination") or "").strip()
            if combination:
                combinations.add(combination)
    return combinations


def _validate_visual_style_context(
    story_tree: Any,
    errors: List[str],
) -> None:
    """Validate new contexts while allowing pre-v1 historical tasks to fall back."""
    if not isinstance(story_tree, Mapping):
        return
    if "visual_style_context" not in story_tree:
        return
    context = story_tree.get("visual_style_context")
    if not isinstance(context, Mapping):
        errors.append("visual_style_context 必须是 JSON object")
        return
    if context.get("schema_version") != VISUAL_STYLE_CONTEXT_SCHEMA:
        errors.append(
            "visual_style_context.schema_version 无效: "
            f"{context.get('schema_version') or '(empty)'}"
        )
    try:
        source_char_count = int(context.get("source_char_count") or 0)
    except (TypeError, ValueError):
        source_char_count = 0
    if source_char_count <= 0:
        errors.append("visual_style_context.source_char_count 必须大于 0")
    if not str(context.get("coverage_strategy") or "").strip():
        errors.append("visual_style_context.coverage_strategy 为空")
    if not any(
        context.get(field)
        for field in (
            "representative_excerpts",
            "worldview",
            "chapter_summaries",
            "narrative_structure",
        )
    ):
        errors.append("visual_style_context 缺少可用于判断题材与视觉风格的内容")


def _cg_reference_limit(image_model: str) -> int:
    name = str(image_model or "").strip().lower()
    if name.startswith("qwen-image") or name.startswith("wan"):
        return 3
    try:
        configured = int(os.getenv("CG_MAX_REFERENCE_IMAGES", "8"))
    except ValueError:
        configured = 8
    return max(1, configured)


def _validate_cg_contracts(
    cg_data: Any,
    errors: List[str],
    *,
    image_model: str,
) -> tuple[int, bool]:
    if not cg_data:
        return 0, False
    if not isinstance(cg_data, Mapping):
        errors.append("cg.data 必须是 JSON object")
        return 0, False

    runnable = 0
    needs_character_anchor = False
    for cg_id, spec in cg_data.items():
        if not isinstance(spec, Mapping):
            errors.append(f"cg[{cg_id}] 不是 JSON object")
            continue
        if not str(spec.get("cg_prompt") or "").strip():
            errors.append(f"cg[{cg_id}].cg_prompt 为空")
            continue
        runnable += 1
        if spec.get("reference_contract_schema") != CG_REFERENCE_CONTRACT_SCHEMA:
            continue
        requirements = spec.get("reference_requirements") or []
        if not isinstance(requirements, list):
            errors.append(f"cg[{cg_id}].reference_requirements 必须是 array")
            continue
        required_scenes = 0
        required_references = 0
        for index, requirement in enumerate(requirements):
            if not isinstance(requirement, Mapping):
                errors.append(
                    f"cg[{cg_id}].reference_requirements[{index}] 不是 object"
                )
                continue
            role = str(requirement.get("role") or "").strip()
            if role in {"scene", "character"} and requirement.get("required"):
                required_references += 1
            if role == "scene":
                if requirement.get("required"):
                    required_scenes += 1
                if not str(requirement.get("entity_id") or "").strip():
                    errors.append(f"cg[{cg_id}] 场景参考缺少 entity_id")
                if not requirement.get("file_candidates"):
                    errors.append(f"cg[{cg_id}] 场景参考缺少 file_candidates")
            elif role == "character":
                needs_character_anchor = True
                entity_id = str(requirement.get("entity_id") or "").strip()
                entity_name = str(requirement.get("entity_name") or "").strip()
                if not entity_id or not entity_name:
                    errors.append(
                        f"cg[{cg_id}] 人物参考缺少可解析的 entity_id/entity_name"
                    )
                if not requirement.get("file_candidates"):
                    errors.append(
                        f"cg[{cg_id}] 人物 {entity_name or entity_id} 缺少 file_candidates"
                    )
                if (
                    requirement.get("required")
                    and not str(requirement.get("identity_prompt") or "").strip()
                ):
                    errors.append(
                        f"cg[{cg_id}] required 人物 {entity_name or entity_id} "
                        "缺少 identity_prompt"
                    )
        if required_scenes != 1:
            errors.append(
                f"cg[{cg_id}] 必须声明且仅声明 1 个 required 主场景参考，"
                f"实际为 {required_scenes}"
            )
        required_with_anchor = required_references + 1
        reference_limit = _cg_reference_limit(image_model)
        if required_with_anchor > reference_limit:
            errors.append(
                f"cg[{cg_id}] required 主场景/人物 + scene_anchor 共 "
                f"{required_with_anchor} 张，超过模型 {image_model} 上限 "
                f"{reference_limit}；不会静默丢弃 required 人物"
            )
    return runnable, needs_character_anchor


def validate_visual_inputs(
    task_id: str,
    *,
    stage: str,
    db_engine=platform_engine,
) -> VisualInputPreflightReport:
    """Validate all upstream contracts without repairing or rewriting them."""
    if stage not in {"char_designer", "scene_painter"}:
        raise ValueError(f"不支持的视觉预检阶段: {stage}")
    errors: List[str] = []

    with db_engine.connect() as conn:
        task = conn.execute(
            text(
                "SELECT story_planner_status, info_extractor_status, image_model "
                "FROM novel_tasks WHERE task_id = :tid LIMIT 1"
            ),
            {"tid": task_id},
        ).mappings().first()
        if not task:
            raise VisualInputPreflightError(f"novel_tasks 中找不到 task_id={task_id}")

        story_tree_payload = _fetch_data(conn, "story_tree_json", task_id, {})
        story_md_count = int(
            conn.execute(
                text("SELECT COUNT(*) FROM story_tree_md WHERE task_id = :tid"),
                {"tid": task_id},
            ).scalar_one()
        )
        character_prompts = _fetch_data(
            conn, "character_prompts", task_id, []
        )
        scene_prompts = _fetch_data(conn, "scene_prompts", task_id, {})
        characters = _fetch_data(conn, "characters", task_id, {})
        plots = _fetch_data(conn, "plots", task_id, {})
        cg_data = load_cg_contract(task_id, db_engine=db_engine)

    if task["story_planner_status"] != TaskStatus.COMPLETED:
        errors.append(
            "story_planner 尚未完成 "
            f"(status={task['story_planner_status']})"
        )
    if task["info_extractor_status"] != TaskStatus.COMPLETED:
        errors.append(
            "info_extractor 尚未完成 "
            f"(status={task['info_extractor_status']})"
        )
    story_tree = select_story_tree_for_consumer(story_tree_payload)
    if not isinstance(story_tree, Mapping):
        errors.append("story_tree_json 没有可用的语言版本")
    elif not isinstance(story_tree.get("nodes"), Mapping) or not story_tree.get("nodes"):
        errors.append("story_tree_json.nodes 缺失或为空")
    _validate_visual_style_context(story_tree, errors)
    if story_md_count <= 0:
        errors.append("story_tree_md 没有任何章节正文")

    image_model = str(task["image_model"] or "").strip()
    if image_model not in ImageModel.VALID_MODELS:
        errors.append(
            f"image_model 无效: {image_model or '(empty)'}；"
            f"允许值={ImageModel.VALID_MODELS}"
        )

    prompt_combinations = _validate_character_prompts(
        character_prompts, errors
    )
    scene_prompt_count = _validate_scene_prompts(scene_prompts, errors)
    active_combinations = _active_character_combinations(characters)
    plot_combinations = _plot_combinations(plots)

    missing_active = sorted(active_combinations - prompt_combinations)
    extra_prompts = sorted(prompt_combinations - active_combinations)
    missing_plot = sorted(plot_combinations - prompt_combinations)
    if active_combinations and not prompt_combinations:
        errors.append(
            f"characters 有 {len(active_combinations)} 个 active combination，"
            "但人物提示词为 0"
        )
    if missing_active:
        errors.append(
            "active combination 找不到人物提示词: " + ", ".join(missing_active)
        )
    if extra_prompts and active_combinations:
        errors.append(
            "人物提示词 combination 与 characters active 组合错位: "
            + ", ".join(extra_prompts)
        )
    if missing_plot:
        errors.append(
            "plots.speaker_combination 找不到人物提示词: "
            + ", ".join(missing_plot)
        )

    cg_count, cg_needs_character_anchor = _validate_cg_contracts(
        cg_data, errors, image_model=image_model
    )
    needs_character_anchor = bool(
        prompt_combinations or cg_needs_character_anchor
    )

    if errors:
        raise VisualInputPreflightError(
            f"{stage} 上游预检失败，未调用任何 LLM/图片 Provider: "
            + "；".join(errors)
        )

    return VisualInputPreflightReport(
        task_id=task_id,
        stage=stage,
        image_model=image_model,
        character_prompt_count=len(prompt_combinations),
        scene_prompt_count=scene_prompt_count,
        cg_count=cg_count,
        needs_character_anchor=needs_character_anchor,
    )
