"""Local-only helpers for reusing completed story/info extraction outputs.

The experiment creates a fresh derived task instead of mutating the source task.
Only database-side semantic/preprocessing products are cloned. Generated images,
style anchors, audio, sound mappings, regeneration history, and built games are
deliberately excluded so each visual-style experiment remains independently
reviewable.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from sqlalchemy import inspect, text

from common.celery_app import celery_app
from common.local_runtime import env_flag
from common.platform_db import platform_engine
from common.task_models import ImageModel, TaskStatus, engine as business_engine
from common.visual_style import (
    ASSET_CHARACTER,
    ASSET_SCENE,
    REMOVE_CHARACTER_BACKGROUND_KEY,
    STYLE_MODE_CUSTOM,
    apply_asset_style,
    compose_anchor_guided_prompt,
    default_visual_style_config,
    load_visual_style_config,
    resolve_asset_style_plan,
    save_visual_style_config,
    strip_leading_style,
    task_output_dir,
)
from common.visual_debug import load_visual_generation_record


REUSE_REQUIRED_FLAGS = (
    "STYLE_ANCHOR_REUSE_ENABLED",
    "LOCAL_PIPELINE_MODE",
    "OSS_DISABLED",
)
REUSE_MANIFEST_RELATIVE_PATH = Path("style_anchors/reuse_manifest.json")

# Cross-service tables live in the platform database. These names are constants,
# never request input, so using them as SQL identifiers is safe.
PLATFORM_CLONE_TABLES: Mapping[str, Tuple[str, ...]] = {
    "story_tree_json": ("data",),
    "story_tree_md": ("name", "data"),
    "plots": ("data",),
    "characters": ("data",),
    "name_mappings": ("data",),
    "character_styles": ("data",),
    "character_prompts": ("data",),
    "scene_prompts": ("data",),
    "backgrounds": ("data",),
    "cg": ("data",),
    "atmosphere": ("data",),
}

# Internal service tables remain in the business database.
BUSINESS_CLONE_TABLES: Mapping[str, Tuple[str, ...]] = {
    "character_position": ("data",),
    "scenes": ("data", "raw"),
    "ultimate": ("data",),
}

# Minimum contract actually consumed by char_designer and scene_painter.
REQUIRED_SOURCE_TABLES = {
    "story_tree_json",
    "plots",
    "characters",
    "name_mappings",
    "character_prompts",
    "scene_prompts",
    "backgrounds",
    "scenes",
}

VISUAL_ASSET_CATEGORIES = {
    "characters": Path("images/characters"),
    "scenes": Path("images/scenes"),
    "cg": Path("images/cg"),
    "backgrounds": Path("images/backgrounds"),
}
VISUAL_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

SCENE_ASPECT_RATIO_SUFFIX = ", 16:9 aspect ratio, cinematic widescreen composition"
SCENE_QUALITY_SUFFIX = (
    ", high clarity, coherent lighting, well-defined complete shapes,"
    " clean spatial relationships, stable perspective"
)
SCENE_IMG2IMG_GUIDANCE = (
    "Use the reference image only to understand the spatial layout, orientation, and architecture of this location, "
    "and to keep the same place identity and art style. "
    "Do NOT copy the reference image's framing or composition: the final composition, camera view, focal point, "
    "and how much of the space is shown are determined entirely by the preceding text description "
    "(it may depict only part of the space, a different angle, or use the location merely as background). "
)
CG_RENDER_SUFFIX = (
    ", 16:9 aspect ratio, cinematic widescreen composition, single decisive moment, "
    "visually powerful dramatic staging, strong emotional tension, dynamic coherent composition"
)
CG_CLEAN_STYLE_SUFFIX = (
    ", high clarity, coherent lighting, clear well-defined complete shapes, "
    "stable anatomy and spatial relationships"
)
CHARACTER_IDENTITY_PREFIX = (
    "原图与目标是同一角色在不同时期或状态下的形象。"
    "保持画风一致和角色的整体识别度，"
    "以下方提示词为准，与原图不一致时以提示词为准。"
    "全身构图，从头到脚完整可见，包括鞋子，不要裁切腿部或脚部，不要拉近镜头。"
    "Do not add outlines around the character. "
)


class ReusePipelineError(ValueError):
    """Raised when a local reuse operation cannot be performed safely."""


def reuse_feature_enabled() -> bool:
    """Return whether every local-only safety switch is explicitly enabled."""
    return all(env_flag(name) for name in REUSE_REQUIRED_FLAGS)


def require_reuse_feature() -> None:
    if reuse_feature_enabled():
        return
    missing = [name for name in REUSE_REQUIRED_FLAGS if not env_flag(name)]
    raise ReusePipelineError(
        "复用实验接口仅允许在本地隔离模式使用，缺少开关: " + ", ".join(missing)
    )


def _validate_task_id(task_id: str, field_name: str = "task_id") -> str:
    value = str(task_id or "").strip()
    if (
        not value
        or Path(value).name != value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise ReusePipelineError(f"非法 {field_name}: {task_id}")
    return value


def _table_exists(engine: Any, table_name: str) -> bool:
    return bool(inspect(engine).has_table(table_name, schema="public"))


def _count_task_rows(connection: Any, table_name: str, task_id: str) -> int:
    row = connection.execute(
        text(f'SELECT COUNT(*) FROM "{table_name}" WHERE task_id = :task_id'),
        {"task_id": task_id},
    ).first()
    return int(row[0] if row else 0)


def _load_json_data(connection: Any, table_name: str, task_id: str) -> Any:
    row = connection.execute(
        text(f'SELECT data FROM "{table_name}" WHERE task_id = :task_id LIMIT 1'),
        {"task_id": task_id},
    ).first()
    if not row:
        return None
    value = row[0]
    if isinstance(value, str):
        return json.loads(value)
    return value


def _active_source_styles(source_task_id: str, story_data: Any) -> Dict[str, str]:
    story_style = ""
    if isinstance(story_data, dict):
        story_style = str(story_data.get("global_style") or "").strip()

    try:
        config = load_visual_style_config(source_task_id, fetch_remote=False)
    except Exception:
        config = default_visual_style_config()

    configured_character_style = str(
        (config.get(ASSET_CHARACTER) or {}).get("global_style") or ""
    ).strip()
    configured_scene_style = str(
        (config.get(ASSET_SCENE) or {}).get("global_style") or ""
    ).strip()
    if config.get("mode") == STYLE_MODE_CUSTOM:
        character_style = configured_character_style or story_style
        scene_style = configured_scene_style or story_style
    else:
        character_style = story_style
        scene_style = story_style

    return {
        "story_global_style": story_style,
        "character_global_style": character_style,
        "scene_global_style": scene_style,
        # Keep inactive custom notes as cleanup candidates too. A source task may
        # have switched back to LLM mode after its prompts were built in custom
        # mode; those stored prompts still need the former prefix removed.
        "configured_character_global_style": configured_character_style,
        "configured_scene_global_style": configured_scene_style,
    }


def _strip_known_styles(prompt: str, styles: Iterable[str]) -> Tuple[str, bool]:
    """Strip exact known style prefixes, including accidental double prefixes."""
    current = str(prompt or "").strip()
    original = current
    candidates = []
    for style in styles:
        normalized = str(style or "").strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    changed = True
    while changed and current:
        changed = False
        for style in candidates:
            cleaned = strip_leading_style(current, style)
            if cleaned != current:
                current = cleaned
                changed = True
    return current, current != original


def cleanse_character_prompts(data: Any, old_styles: Sequence[str]) -> Tuple[Any, int]:
    """Remove old global-style prefixes while preserving character semantics."""
    cleaned = deepcopy(data)
    if not isinstance(cleaned, list):
        return cleaned, 0

    changed_count = 0
    for record in cleaned:
        if not isinstance(record, dict):
            continue
        prompt, changed = _strip_known_styles(record.get("positive_prompt", ""), old_styles)
        if changed:
            record["positive_prompt"] = prompt
            changed_count += 1
    return cleaned, changed_count


def cleanse_backgrounds(data: Any, old_styles: Sequence[str]) -> Tuple[Any, int]:
    """Remove old scene style from chapter-cover and novel-cover prompts."""
    cleaned = deepcopy(data)
    if not isinstance(cleaned, dict):
        return cleaned, 0

    changed_count = 0
    generated = cleaned.get("generated_prompts")
    if isinstance(generated, dict):
        for prompt_record in generated.values():
            if not isinstance(prompt_record, dict):
                continue
            prompt, changed = _strip_known_styles(prompt_record.get("prompt", ""), old_styles)
            if changed:
                prompt_record["prompt"] = prompt
                changed_count += 1

    novel_cover = cleaned.get("novel_cover_metadata")
    if isinstance(novel_cover, dict):
        prompt, changed = _strip_known_styles(novel_cover.get("prompt", ""), old_styles)
        if changed:
            novel_cover["prompt"] = prompt
            changed_count += 1
    return cleaned, changed_count


def cleanse_cg_prompts(data: Any, old_styles: Sequence[str]) -> Tuple[Any, int]:
    """Rebuild CG prompts from semantic descriptions whenever possible."""
    cleaned = deepcopy(data)
    if not isinstance(cleaned, dict):
        return cleaned, 0

    changed_count = 0
    for spec in cleaned.values():
        if not isinstance(spec, dict):
            continue
        original = str(spec.get("cg_prompt") or "").strip()
        semantic = str(spec.get("init_description") or "").strip()
        if not semantic:
            semantic, _ = _strip_known_styles(original, old_styles)
        if semantic != original:
            spec["cg_prompt"] = semantic
            changed_count += 1
        # The character worker regenerates this list for the derived task. Keeping
        # a source-task attachment name could otherwise hide a failed stitch.
        spec["char_img_att"] = []
    return cleaned, changed_count


def preflight_source_task(source_task_id: str) -> Dict[str, Any]:
    """Read-only validation of a source task and its reusable products."""
    require_reuse_feature()
    source_task_id = _validate_task_id(source_task_id, "source_task_id")
    errors = []
    warnings = []
    table_counts: Dict[str, int] = {}

    with platform_engine.connect() as connection:
        task = connection.execute(
            text(
                """
                SELECT task_id, novel_filename, project_name, image_model,
                       story_planner_status, info_extractor_status,
                       char_designer_status, scene_painter_status
                FROM novel_tasks
                WHERE task_id = :task_id
                """
            ),
            {"task_id": source_task_id},
        ).mappings().first()

        if not task:
            return {
                "source_task_id": source_task_id,
                "eligible": False,
                "errors": ["源任务不存在"],
                "warnings": [],
                "tables": {},
                "source_task": None,
                "source_styles": {},
            }

        if task["story_planner_status"] != TaskStatus.COMPLETED:
            errors.append("story_planner 尚未完成")
        if task["info_extractor_status"] != TaskStatus.COMPLETED:
            errors.append("info_extractor 尚未完成")

        for table_name in PLATFORM_CLONE_TABLES:
            if not _table_exists(platform_engine, table_name):
                table_counts[table_name] = 0
                if table_name in REQUIRED_SOURCE_TABLES:
                    errors.append(f"缺少必需表: {table_name}")
                else:
                    warnings.append(f"可选表不存在，将跳过: {table_name}")
                continue
            table_counts[table_name] = _count_task_rows(
                connection, table_name, source_task_id
            )

        story_data = (
            _load_json_data(connection, "story_tree_json", source_task_id)
            if table_counts.get("story_tree_json")
            else None
        )

    with business_engine.connect() as connection:
        for table_name in BUSINESS_CLONE_TABLES:
            if not _table_exists(business_engine, table_name):
                table_counts[table_name] = 0
                if table_name in REQUIRED_SOURCE_TABLES:
                    errors.append(f"缺少必需表: {table_name}")
                else:
                    warnings.append(f"可选表不存在，将跳过: {table_name}")
                continue
            table_counts[table_name] = _count_task_rows(
                connection, table_name, source_task_id
            )

    for table_name in sorted(REQUIRED_SOURCE_TABLES):
        if table_counts.get(table_name, 0) <= 0:
            errors.append(f"源任务缺少必需产物: {table_name}")

    for table_name, count in sorted(table_counts.items()):
        if table_name not in REQUIRED_SOURCE_TABLES and count <= 0:
            warnings.append(f"可选产物为空，将跳过: {table_name}")

    # Preserve order while removing duplicate diagnostics.
    errors = list(dict.fromkeys(errors))
    warnings = list(dict.fromkeys(warnings))
    source_styles = _active_source_styles(source_task_id, story_data)
    return {
        "source_task_id": source_task_id,
        "eligible": not errors,
        "errors": errors,
        "warnings": warnings,
        "tables": table_counts,
        "source_task": dict(task),
        "source_styles": source_styles,
        "pipeline_scope": {
            "reused": ["story_planner", "info_extractor"],
            "will_run": ["char_designer", "scene_painter"],
            "will_not_run": [
                "audio_producer",
                "sound_arranger",
                "game_builder",
            ],
        },
    }


def _new_derived_task_id(source_task_id: str) -> str:
    source = "".join(ch for ch in source_task_id if ch.isalnum() or ch in "_-")
    source = source[:12] or "source"
    return f"style_{source}_{uuid.uuid4().hex[:8]}"


def _task_exists(task_id: str) -> bool:
    with platform_engine.connect() as connection:
        return bool(
            connection.execute(
                text("SELECT 1 FROM novel_tasks WHERE task_id = :task_id"),
                {"task_id": task_id},
            ).first()
        )


def _clone_table(
    connection: Any,
    table_name: str,
    columns: Sequence[str],
    source_task_id: str,
    target_task_id: str,
) -> int:
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    insert_columns = (
        f'task_id, {quoted_columns}, create_time, update_time'
        if quoted_columns
        else "task_id, create_time, update_time"
    )
    select_columns = (
        f':target_task_id, {quoted_columns}, NOW(), NOW()'
        if quoted_columns
        else ":target_task_id, NOW(), NOW()"
    )
    result = connection.execute(
        text(
            f'INSERT INTO "{table_name}" ({insert_columns}) '
            f'SELECT {select_columns} FROM "{table_name}" '
            "WHERE task_id = :source_task_id"
        ),
        {
            "source_task_id": source_task_id,
            "target_task_id": target_task_id,
        },
    )
    return int(result.rowcount or 0)


def _update_json_data(
    connection: Any,
    table_name: str,
    task_id: str,
    data: Any,
) -> None:
    connection.execute(
        text(
            f'UPDATE "{table_name}" '
            "SET data = CAST(:data AS JSONB), update_time = NOW() "
            "WHERE task_id = :task_id"
        ),
        {
            "task_id": task_id,
            "data": json.dumps(data, ensure_ascii=False),
        },
    )


def _insert_derived_novel_task(
    connection: Any,
    source_task_id: str,
    target_task_id: str,
    *,
    image_model: str,
    project_name: str,
) -> None:
    result = connection.execute(
        text(
            """
            INSERT INTO novel_tasks (
                task_id, novel_filename, project_name, user_id, user_name,
                group_name, pipeline_name, output_language, art_style, story_mode,
                gameplay, text_model, image_model, audio_model, resolution,
                upload_type, target_audience, narrative_perspective, hot_elements,
                generated_project_name, remake_task_ids, info_confirm_stage,
                story_planner_status, info_extractor_status,
                sound_arranger_status, char_designer_status, audio_producer_status,
                scene_painter_status, game_builder_status, overall_status,
                progress_percentage, error_message, celery_chain_id, completed_at,
                is_deleted, is_purged, is_published, create_time, update_time
            )
            SELECT
                :target_task_id, novel_filename, :project_name, user_id, user_name,
                group_name, 'style-anchor-reuse-local', output_language, art_style,
                story_mode, gameplay, text_model, :image_model, audio_model,
                resolution, 'style_anchor_reuse', target_audience,
                narrative_perspective, hot_elements, generated_project_name,
                '[]'::jsonb, 'confirmed',
                :completed, :completed,
                :pending, :pending, :pending, :pending, :pending, :pending,
                20, NULL, NULL, NULL, FALSE, FALSE, FALSE, NOW(), NOW()
            FROM novel_tasks
            WHERE task_id = :source_task_id
            """
        ),
        {
            "source_task_id": source_task_id,
            "target_task_id": target_task_id,
            "project_name": project_name,
            "image_model": image_model,
            "completed": TaskStatus.COMPLETED,
            "pending": TaskStatus.PENDING,
        },
    )
    if result.rowcount != 1:
        raise ReusePipelineError(f"复制 novel_tasks 失败: {source_task_id}")


def _manifest_path(task_id: str) -> Path:
    return task_output_dir(task_id) / REUSE_MANIFEST_RELATIVE_PATH


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


def load_reuse_manifest(task_id: str) -> Dict[str, Any]:
    task_id = _validate_task_id(task_id)
    path = _manifest_path(task_id)
    if not path.exists():
        raise ReusePipelineError(f"任务 {task_id} 不是本地复用实验派生任务")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ReusePipelineError(f"读取复用实验清单失败: {exc}") from exc
    if data.get("derived_task_id") != task_id:
        raise ReusePipelineError("复用实验清单与 task_id 不一致")
    return data


def _cleanup_derived_database_rows(task_id: str) -> None:
    """Best-effort cleanup for a target created during a failed clone attempt."""
    for engine, tables in (
        (business_engine, BUSINESS_CLONE_TABLES),
        (platform_engine, PLATFORM_CLONE_TABLES),
    ):
        try:
            with engine.begin() as connection:
                for table_name in tables:
                    if _table_exists(engine, table_name):
                        connection.execute(
                            text(f'DELETE FROM "{table_name}" WHERE task_id = :task_id'),
                            {"task_id": task_id},
                        )
        except Exception:
            pass
    try:
        with platform_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM novel_tasks WHERE task_id = :task_id"),
                {"task_id": task_id},
            )
    except Exception:
        pass


def create_derived_visual_task(
    source_task_id: str,
    *,
    character_global_style: str = "",
    scene_global_style: str = "",
    remove_character_background: bool = True,
    image_model: str = ImageModel.DEFAULT,
    project_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Clone reusable preprocessing products into a new local experiment task."""
    require_reuse_feature()
    source_task_id = _validate_task_id(source_task_id, "source_task_id")
    if image_model not in ImageModel.VALID_MODELS:
        raise ReusePipelineError(
            "image_model 只支持: " + ", ".join(ImageModel.VALID_MODELS)
        )

    preflight = preflight_source_task(source_task_id)
    if not preflight["eligible"]:
        raise ReusePipelineError("源任务不可复用: " + "; ".join(preflight["errors"]))

    target_task_id = _new_derived_task_id(source_task_id)
    while _task_exists(target_task_id):
        target_task_id = _new_derived_task_id(source_task_id)

    source_project_name = str(preflight["source_task"]["project_name"] or source_task_id)
    derived_project_name = str(project_name or "").strip() or f"{source_project_name}（风格实验）"
    source_styles = preflight["source_styles"]
    old_character_styles = [
        source_styles.get("character_global_style", ""),
        source_styles.get("configured_character_global_style", ""),
        source_styles.get("story_global_style", ""),
    ]
    old_scene_styles = [
        source_styles.get("scene_global_style", ""),
        source_styles.get("configured_scene_global_style", ""),
        source_styles.get("story_global_style", ""),
    ]

    cloned_tables: Dict[str, int] = {}
    cleansed_counts = {
        "character_prompts": 0,
        "backgrounds": 0,
        "cg": 0,
    }

    try:
        with platform_engine.begin() as connection:
            _insert_derived_novel_task(
                connection,
                source_task_id,
                target_task_id,
                image_model=image_model,
                project_name=derived_project_name,
            )
            for table_name, columns in PLATFORM_CLONE_TABLES.items():
                if not _table_exists(platform_engine, table_name):
                    continue
                cloned_tables[table_name] = _clone_table(
                    connection,
                    table_name,
                    columns,
                    source_task_id,
                    target_task_id,
                )

            character_data = _load_json_data(
                connection, "character_prompts", target_task_id
            )
            cleaned_characters, count = cleanse_character_prompts(
                character_data, old_character_styles
            )
            cleansed_counts["character_prompts"] = count
            _update_json_data(
                connection, "character_prompts", target_task_id, cleaned_characters
            )

            background_data = _load_json_data(connection, "backgrounds", target_task_id)
            cleaned_backgrounds, count = cleanse_backgrounds(
                background_data, old_scene_styles
            )
            cleansed_counts["backgrounds"] = count
            _update_json_data(
                connection, "backgrounds", target_task_id, cleaned_backgrounds
            )

            if cloned_tables.get("cg", 0) > 0:
                cg_data = _load_json_data(connection, "cg", target_task_id)
                cleaned_cg, count = cleanse_cg_prompts(cg_data, old_scene_styles)
                cleansed_counts["cg"] = count
                _update_json_data(connection, "cg", target_task_id, cleaned_cg)

        with business_engine.begin() as connection:
            for table_name, columns in BUSINESS_CLONE_TABLES.items():
                if not _table_exists(business_engine, table_name):
                    continue
                cloned_tables[table_name] = _clone_table(
                    connection,
                    table_name,
                    columns,
                    source_task_id,
                    target_task_id,
                )
            if cloned_tables.get("scenes", 0) > 0:
                connection.execute(
                    text(
                        "UPDATE scenes SET failed_scenes = '{}'::jsonb "
                        "WHERE task_id = :task_id"
                    ),
                    {"task_id": target_task_id},
                )

        config = default_visual_style_config()
        config["mode"] = STYLE_MODE_CUSTOM
        config[REMOVE_CHARACTER_BACKGROUND_KEY] = bool(remove_character_background)
        config[ASSET_CHARACTER]["global_style"] = str(
            character_global_style or ""
        ).strip()
        config[ASSET_SCENE]["global_style"] = str(scene_global_style or "").strip()
        save_visual_style_config(
            target_task_id,
            config,
            mirror=False,
        )

        manifest = {
            "version": 1,
            "source_task_id": source_task_id,
            "derived_task_id": target_task_id,
            "project_name": derived_project_name,
            "created_at": datetime.now().astimezone().isoformat(),
            "image_model": image_model,
            "pipeline_scope": preflight["pipeline_scope"],
            "cloned_tables": cloned_tables,
            "cleansed_prompt_records": cleansed_counts,
            "source_styles_removed": source_styles,
            "requested_styles": {
                ASSET_CHARACTER: str(character_global_style or "").strip(),
                ASSET_SCENE: str(scene_global_style or "").strip(),
            },
            REMOVE_CHARACTER_BACKGROUND_KEY: bool(remove_character_background),
            "confirmed_anchors": None,
            "celery_chain_id": None,
            "submitted_at": None,
        }
        _write_json_atomic(_manifest_path(target_task_id), manifest)
        return manifest
    except Exception:
        _cleanup_derived_database_rows(target_task_id)
        output_dir = task_output_dir(target_task_id)
        if output_dir.exists():
            # This directory belongs exclusively to the just-failed derived task.
            import shutil

            shutil.rmtree(output_dir, ignore_errors=True)
        raise


def _visual_task_row(task_id: str) -> Optional[Dict[str, Any]]:
    with platform_engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT task_id, project_name, image_model, progress_percentage,
                       overall_status, error_message, celery_chain_id,
                       story_planner_status, info_extractor_status,
                       char_designer_status, scene_painter_status,
                       audio_producer_status, sound_arranger_status,
                       game_builder_status
                FROM novel_tasks
                WHERE task_id = :task_id
                """
            ),
            {"task_id": task_id},
        ).mappings().first()
    return dict(row) if row else None


def start_visual_only_pipeline(task_id: str) -> Dict[str, Any]:
    """Submit exactly char_designer -> scene_painter for a derived task."""
    require_reuse_feature()
    task_id = _validate_task_id(task_id)
    manifest = load_reuse_manifest(task_id)

    # Fail closed if either custom Anchor is missing, stale, changed, or unconfirmed.
    character_plan = resolve_asset_style_plan(
        task_id, ASSET_CHARACTER, fetch_remote=False
    )
    scene_plan = resolve_asset_style_plan(task_id, ASSET_SCENE, fetch_remote=False)

    row = _visual_task_row(task_id)
    if not row:
        raise ReusePipelineError(f"派生任务不存在: {task_id}")
    if row["story_planner_status"] != TaskStatus.COMPLETED:
        raise ReusePipelineError("派生任务的 story_planner 复用状态异常")
    if row["info_extractor_status"] != TaskStatus.COMPLETED:
        raise ReusePipelineError("派生任务的 info_extractor 复用状态异常")
    for stage in ("char_designer", "scene_painter"):
        status = row[f"{stage}_status"]
        if status != TaskStatus.PENDING:
            raise ReusePipelineError(
                f"{stage} 当前状态不是待处理；如需做另一组风格对比，请新建派生任务"
            )

    with platform_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE novel_tasks
                SET overall_status = :processing,
                    progress_percentage = 20,
                    error_message = NULL,
                    celery_chain_id = NULL,
                    update_time = NOW()
                WHERE task_id = :task_id
                """
            ),
            {"task_id": task_id, "processing": TaskStatus.PROCESSING},
        )

    try:
        from celery import chain

        result = chain(
            celery_app.signature(
                "services.char_designer.tasks.generate_characters",
                args=[task_id],
                queue="char_queue",
            ),
            celery_app.signature(
                "services.scene_painter.tasks.generate_scenes",
                queue="scene_queue",
            ),
        ).apply_async()
    except Exception as exc:
        with platform_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE novel_tasks
                    SET overall_status = :pending,
                        error_message = :error,
                        update_time = NOW()
                    WHERE task_id = :task_id
                    """
                ),
                {
                    "task_id": task_id,
                    "pending": TaskStatus.PENDING,
                    "error": f"visual-only 提交失败: {exc}",
                },
            )
        raise ReusePipelineError(f"visual-only Pipeline 提交失败: {exc}") from exc

    chain_id = str(result.id)
    with platform_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE novel_tasks
                SET celery_chain_id = :chain_id, update_time = NOW()
                WHERE task_id = :task_id
                """
            ),
            {"task_id": task_id, "chain_id": chain_id},
        )

    manifest["celery_chain_id"] = chain_id
    manifest["submitted_at"] = datetime.now().astimezone().isoformat()
    manifest["requested_styles"] = {
        ASSET_CHARACTER: character_plan.style,
        ASSET_SCENE: scene_plan.style,
    }
    style_config = load_visual_style_config(task_id, fetch_remote=False)
    manifest[REMOVE_CHARACTER_BACKGROUND_KEY] = bool(
        style_config.get(REMOVE_CHARACTER_BACKGROUND_KEY, True)
    )
    output_root = task_output_dir(task_id)
    manifest["confirmed_anchors"] = {
        ASSET_CHARACTER: {
            "source": character_plan.anchor_source,
            "sha256": character_plan.anchor_sha256,
            "file": character_plan.anchor_path.relative_to(output_root).as_posix(),
        },
        ASSET_SCENE: {
            "source": scene_plan.anchor_source,
            "sha256": scene_plan.anchor_sha256,
            "file": scene_plan.anchor_path.relative_to(output_root).as_posix(),
        },
    }
    _write_json_atomic(_manifest_path(task_id), manifest)
    return {
        "success": True,
        "task_id": task_id,
        "source_task_id": manifest["source_task_id"],
        "celery_chain_id": chain_id,
        "pipeline": ["char_designer", "scene_painter"],
        "excluded": ["audio_producer", "sound_arranger", "game_builder"],
        REMOVE_CHARACTER_BACKGROUND_KEY: manifest[REMOVE_CHARACTER_BACKGROUND_KEY],
    }


def _load_visual_source_data(task_id: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    with platform_engine.connect() as connection:
        for table_name in ("character_prompts", "scene_prompts", "backgrounds", "cg"):
            if _table_exists(platform_engine, table_name):
                data[table_name] = _load_json_data(connection, table_name, task_id)
            else:
                data[table_name] = None
    data["_character_style_plan"] = _style_plan_or_none(task_id, ASSET_CHARACTER)
    data["_scene_style_plan"] = _style_plan_or_none(task_id, ASSET_SCENE)
    return data


def _expected_visual_asset_counts(source_data: Mapping[str, Any]) -> Dict[str, int]:
    """Estimate provider-reviewable outputs from reused semantic products."""
    character_total = 0
    seen_combinations = set()
    character_prompts = source_data.get("character_prompts")
    if isinstance(character_prompts, list):
        for record in character_prompts:
            if not isinstance(record, dict) or not record.get("positive_prompt"):
                continue
            key = str(record.get("combination") or record.get("speaker_id") or "").strip()
            if not key or key in seen_combinations:
                continue
            seen_combinations.add(key)
            emotions = record.get("emotions")
            emotion_count = 0
            if isinstance(emotions, list):
                emotion_count = len(
                    {
                        str(value).strip()
                        for value in emotions
                        if str(value).strip() and str(value).strip() != "calm"
                    }
                )
            # Every calm/emotion provider image is published as LS/MS/CS.
            character_total += (1 + emotion_count) * 3

    scene_total = 0
    scene_prompts = source_data.get("scene_prompts")
    if isinstance(scene_prompts, dict):
        for scene_id, record in scene_prompts.items():
            if str(scene_id).startswith("cg_") or not isinstance(record, dict):
                continue
            if not str(record.get("image_prompt") or "").strip():
                continue
            try:
                copy_count = max(0, int(record.get("copy_count", 0) or 0))
            except (TypeError, ValueError):
                copy_count = 0
            scene_total += 1 + copy_count

    cg_total = 0
    cg_data = source_data.get("cg")
    if isinstance(cg_data, dict):
        cg_total = sum(
            1
            for record in cg_data.values()
            if isinstance(record, dict) and str(record.get("cg_prompt") or "").strip()
        )

    background_total = 0
    backgrounds = source_data.get("backgrounds")
    if isinstance(backgrounds, dict):
        generated = backgrounds.get("generated_prompts")
        if isinstance(generated, dict):
            background_total += sum(
                1
                for record in generated.values()
                if isinstance(record, dict) and str(record.get("prompt") or "").strip()
            )
        novel_cover = backgrounds.get("novel_cover_metadata")
        if isinstance(novel_cover, dict) and str(novel_cover.get("prompt") or "").strip():
            background_total += 1

    return {
        "characters": character_total,
        "scenes": scene_total,
        "cg": cg_total,
        "backgrounds": background_total,
    }


def _visual_progress(
    visual_state: str,
    expected: Mapping[str, int],
    generated: Mapping[str, int],
) -> Tuple[int, Dict[str, Dict[str, int]]]:
    breakdown: Dict[str, Dict[str, int]] = {}
    expected_total = 0
    generated_total = 0
    for category in VISUAL_ASSET_CATEGORIES:
        expected_count = max(0, int(expected.get(category, 0) or 0))
        generated_count = max(0, int(generated.get(category, 0) or 0))
        completed_count = min(generated_count, expected_count) if expected_count else generated_count
        breakdown[category] = {
            "expected": expected_count,
            "generated": generated_count,
            "completed": completed_count,
        }
        expected_total += expected_count
        generated_total += completed_count

    if visual_state == "completed":
        percentage = 100
    elif expected_total > 0:
        percentage = int((generated_total * 100) / expected_total)
        percentage = min(99, max(0, percentage))
    elif visual_state == "processing":
        percentage = 1
    else:
        percentage = 0

    breakdown["total"] = {
        "expected": expected_total,
        "generated": generated_total,
        "completed": generated_total,
    }
    return percentage, breakdown


def _asset_url(task_id: str, category: str, filename: str) -> str:
    return (
        f"/api/tasks/{task_id}/visual-assets/{category}/"
        f"{quote(filename, safe='')}"
    )


def _reference_payload(task_id: str, reference: Mapping[str, Any]) -> Dict[str, Any]:
    relative_path = str(reference.get("relative_path") or "").strip()
    name = str(reference.get("name") or Path(relative_path).name).strip()
    role = str(reference.get("role") or "参考图").strip()
    url = None
    exists = False
    if relative_path:
        parts = Path(relative_path).parts
        target = task_output_dir(task_id) / relative_path
        exists = target.exists() and target.is_file() and target.stat().st_size > 0
        if len(parts) == 2 and parts[0] == "style_anchors":
            if parts[1] == "character_anchor.png":
                url = f"/api/tasks/{task_id}/anchors/{ASSET_CHARACTER}/image"
            elif parts[1] == "scene_anchor.png":
                url = f"/api/tasks/{task_id}/anchors/{ASSET_SCENE}/image"
        elif len(parts) >= 3 and parts[0] == "images":
            category = parts[1]
            if category in VISUAL_ASSET_CATEGORIES:
                url = _asset_url(task_id, category, parts[-1])
    return {
        "name": name,
        "role": role,
        "relative_path": relative_path,
        "exists": exists,
        "url": url,
    }


def _reference_record(task_id: str, relative_path: str, role: str) -> Dict[str, Any]:
    return _reference_payload(
        task_id,
        {
            "name": Path(relative_path).name,
            "role": role,
            "relative_path": relative_path,
        },
    )


def _style_plan_or_none(task_id: str, asset_kind: str):
    try:
        return resolve_asset_style_plan(task_id, asset_kind, fetch_remote=False)
    except Exception:
        return None


def _character_prompt_record(
    source_data: Mapping[str, Any],
    speaker_id: str,
    combination_suffix: str,
) -> Optional[Dict[str, Any]]:
    rows = source_data.get("character_prompts")
    if not isinstance(rows, list):
        return None
    suffix = f"-{combination_suffix}"
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("speaker_id") or "") != speaker_id:
            continue
        if str(row.get("combination") or "").endswith(suffix):
            return row
    return None


def _base_combination_suffix(
    source_data: Mapping[str, Any],
    speaker_id: str,
) -> str:
    rows = source_data.get("character_prompts")
    candidates = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict) or str(row.get("speaker_id") or "") != speaker_id:
                continue
            parts = str(row.get("combination") or "").rsplit("-", 3)
            if len(parts) != 4:
                continue
            try:
                numeric = tuple(int(value) for value in parts[1:])
            except ValueError:
                continue
            candidates.append((numeric, "-".join(parts[1:])))
    return min(candidates)[1] if candidates else ""


def _reconstruct_character_debug(
    task_id: str,
    filename: str,
    source_data: Mapping[str, Any],
) -> Dict[str, Any]:
    if re.match(r"^cg_.+_char_\d+\.(png|jpe?g|webp)$", filename, re.I):
        return {
            "source": "reconstructed",
            "generation_mode": "local_composite",
            "prompt": "",
            "reference_images": [],
            "logic": [
                "该文件是 CG 使用的人物参考拼接图，不是 Provider 直接生成的单张立绘。",
                "它由 char_designer 将已生成的人物立绘按 CG 需求在本地拼接。",
            ],
        }

    match = re.match(
        r"^(?P<speaker>.+)_(?P<combo>\d+-\d+-\d+)_(?P<emotion>[A-Za-z]+)_(?P<shot>LS|MS|CS)\.(?:png|jpe?g|webp)$",
        filename,
        re.I,
    )
    if not match:
        return {
            "source": "reconstructed",
            "generation_mode": "unknown",
            "prompt": "",
            "reference_images": [],
            "logic": ["旧任务没有该文件的生成期审计记录，且文件名无法映射到人物提示词。"],
        }

    speaker = match.group("speaker")
    combo = match.group("combo")
    emotion = match.group("emotion").lower()
    shot = match.group("shot").upper()
    if shot in {"MS", "CS"}:
        source_name = filename.replace(f"_{shot}.", "_LS.")
        source_debug = _visual_asset_debug_from_context(
            task_id, "characters", source_name, source_data
        )
        return {
            "source": "reconstructed",
            "generation_mode": "local_crop",
            "prompt": source_debug.get("prompt", ""),
            "reference_images": [
                _reference_record(
                    task_id,
                    f"images/characters/{source_name}",
                    "LS 原图",
                )
            ],
            "logic": [
                f"{shot} 不是再次调用 Provider 生图。",
                "项目在本地从对应 LS 远景图按镜头裁剪规则生成。",
            ],
        }

    record = _character_prompt_record(source_data, speaker, combo) or {}
    semantic_prompt = str(record.get("positive_prompt") or "").strip()
    plan = source_data.get("_character_style_plan")
    styled_prompt = apply_asset_style(plan, semantic_prompt) if plan else semantic_prompt
    references = []
    logic = ["旧任务缺少生成期 sidecar，以下信息由当前风格配置和上游数据重建。"]

    if emotion != "calm":
        calm_name = re.sub(
            rf"_{re.escape(match.group('emotion'))}_LS\.",
            "_calm_LS.",
            filename,
            flags=re.I,
        )
        references.append(
            _reference_record(
                task_id,
                f"images/characters/{calm_name}",
                "同组合 calm 基础立绘",
            )
        )
        prompt = (
            "[旧任务未记录精确情绪生图提示词]\n"
            "下方仅展示该组合的上游人物语义提示词：\n"
            f"{semantic_prompt}"
        )
        logic.extend(
            [
                "情绪图以同一组合的 calm 立绘作为图生图输入。",
                "实际情绪提示词由 personality 与 emotion 在生图阶段动态组成；新任务会记录精确最终值。",
            ]
        )
        mode = "image_to_image"
    else:
        base_combo = _base_combination_suffix(source_data, speaker)
        if base_combo and combo != base_combo:
            base_name = f"{speaker}_{base_combo}_calm_LS.png"
            references.append(
                _reference_record(
                    task_id,
                    f"images/characters/{base_name}",
                    "人物基础组合立绘",
                )
            )
            prompt = CHARACTER_IDENTITY_PREFIX + styled_prompt
            logic.extend(
                [
                    "该 calm 图属于人物服饰/年龄/状态变体。",
                    "以该人物数字最小组合的 calm 立绘做图生图，文本要求覆盖参考图中的内容差异。",
                ]
            )
            mode = "image_to_image"
        elif plan and plan.uses_anchor:
            prompt = compose_anchor_guided_prompt(plan, styled_prompt)
            references.append(
                _reference_record(
                    task_id,
                    "style_anchors/character_anchor.png",
                    "人物风格 Anchor",
                )
            )
            logic.extend(
                [
                    "人物基础 calm 立绘使用已确认人物 Anchor 做图生图。",
                    "Anchor 只控制媒介、线条、色彩、光影和材质，人物身份与服饰等内容以文字为准。",
                ]
            )
            mode = "image_to_image"
        else:
            prompt = styled_prompt
            logic.append("人物基础 calm 立绘由上游语义提示词直接文生图。")
            mode = "text_to_image"

    return {
        "source": "reconstructed",
        "generation_mode": mode,
        "prompt": prompt,
        "reference_images": references,
        "logic": logic,
    }


def _scene_parent_id(scene_id: str) -> Optional[str]:
    if not scene_id.startswith("scene_") or "_" not in scene_id[len("scene_"):]:
        return None
    head, _, tail = scene_id.rpartition("_")
    return head if head and tail.isdigit() else None


def _reconstruct_scene_debug(
    task_id: str,
    filename: str,
    source_data: Mapping[str, Any],
) -> Dict[str, Any]:
    scene_prompts = source_data.get("scene_prompts")
    if not isinstance(scene_prompts, dict):
        scene_prompts = {}
    stem = Path(filename).stem
    scene_id = stem
    while scene_id not in scene_prompts and re.search(r"_\d+$", scene_id):
        scene_id = scene_id.rsplit("_", 1)[0]

    if scene_id not in scene_prompts:
        return {
            "source": "reconstructed",
            "generation_mode": "unknown",
            "prompt": "",
            "reference_images": [],
            "logic": ["旧任务没有该场景文件的生成期审计记录，且无法映射 scene_prompts。"],
        }

    if stem != scene_id:
        source_name = f"{scene_id}.png"
        source_debug = _visual_asset_debug_from_context(
            task_id, "scenes", source_name, source_data
        )
        return {
            "source": "reconstructed",
            "generation_mode": "local_copy",
            "prompt": source_debug.get("prompt", ""),
            "reference_images": [
                _reference_record(task_id, f"images/scenes/{source_name}", "场景原图")
            ],
            "logic": [
                "该文件是剧情连续区域实例，不会再次调用 Provider。",
                "项目按 scene_prompts.copy_count 在本地复制对应场景原图。",
            ],
        }

    record = scene_prompts.get(scene_id) or {}
    semantic = str(record.get("image_prompt") or "").strip()
    plan = source_data.get("_scene_style_plan")
    styled = apply_asset_style(plan, semantic) if plan else semantic
    positive = f"{styled}{SCENE_QUALITY_SUFFIX}{SCENE_ASPECT_RATIO_SUFFIX}"
    parent_id = _scene_parent_id(scene_id)
    parent_path = task_output_dir(task_id) / "images" / "scenes" / f"{parent_id}.png" if parent_id else None
    logic = ["旧任务缺少生成期 sidecar，以下信息由当前风格配置和上游数据重建。"]
    if parent_id and parent_path and parent_path.exists():
        prompt = f"{positive.rstrip()} {SCENE_IMG2IMG_GUIDANCE}"
        references = [
            _reference_record(
                task_id,
                f"images/scenes/{parent_id}.png",
                "父级场景底图",
            )
        ]
        mode = "image_to_image"
        logic.append("二级场景变体以父级场景底图做图生图，文字决定新镜头与构图。")
    elif plan and plan.uses_anchor:
        prompt = compose_anchor_guided_prompt(plan, positive)
        references = [
            _reference_record(task_id, "style_anchors/scene_anchor.png", "场景风格 Anchor")
        ]
        mode = "image_to_image"
        logic.append("场景图以已确认场景 Anchor 做图生图，Anchor 只控制视觉风格。")
    else:
        prompt = positive
        references = []
        mode = "text_to_image"
        logic.append("场景图由上游场景提示词直接文生图。")
    return {
        "source": "reconstructed",
        "generation_mode": mode,
        "prompt": prompt,
        "reference_images": references,
        "logic": logic,
    }


def _cg_scene_hint(prompt: str, scene_count: int) -> str:
    if scene_count <= 0:
        return prompt
    hint = (
        f", USE the {scene_count} attached scene reference image(s) ONLY for ambient style — "
        f"reuse their lighting, color palette, mist/lantern/material cues — "
        f"DO NOT copy their camera angle, framing, or focal subject; "
        f"the camera and composition are defined by this prompt"
    )
    return prompt.rstrip().rstrip(",") + hint


def _reconstruct_cg_debug(
    task_id: str,
    filename: str,
    source_data: Mapping[str, Any],
) -> Dict[str, Any]:
    cg_data = source_data.get("cg")
    cg_id = Path(filename).stem
    spec = cg_data.get(cg_id) if isinstance(cg_data, dict) else None
    if not isinstance(spec, dict):
        return {
            "source": "reconstructed",
            "generation_mode": "unknown",
            "prompt": "",
            "reference_images": [],
            "logic": ["旧任务没有该 CG 的生成期审计记录，且无法映射 cg 表。"],
        }

    plan = source_data.get("_scene_style_plan")
    prompt = str(spec.get("cg_prompt") or "").strip()
    prompt = apply_asset_style(plan, prompt) if plan else prompt
    if "16:9 aspect ratio" not in prompt:
        prompt = prompt.rstrip().rstrip(",") + CG_RENDER_SUFFIX
    if "stable anatomy and spatial relationships" not in prompt:
        prompt = prompt.rstrip().rstrip(",") + CG_CLEAN_STYLE_SUFFIX

    references = []
    char_dir = task_output_dir(task_id) / "images" / "characters"
    scene_dir = task_output_dir(task_id) / "images" / "scenes"
    char_names = [name for name in (spec.get("char_img_att") or []) if (char_dir / name).exists()]
    scene_names = [name for name in (spec.get("scene_img_att") or []) if (scene_dir / name).exists()][:2]
    if char_names:
        references.append(
            _reference_record(
                task_id,
                f"images/characters/{char_names[0]}",
                "人物拼接参考图",
            )
        )
    references.extend(
        _reference_record(task_id, f"images/scenes/{name}", "场景氛围参考图")
        for name in scene_names
    )
    logic = ["旧任务缺少生成期 sidecar，以下信息由当前 cg 表附件和风格配置重建。"]
    if references:
        prompt = _cg_scene_hint(prompt, len(scene_names))
        logic.append("CG 优先以人物拼接图锁角色，并可附加最多两张场景图约束环境氛围。")
        mode = "image_to_image"
    elif plan and plan.uses_anchor:
        prompt = compose_anchor_guided_prompt(plan, prompt)
        references = [
            _reference_record(task_id, "style_anchors/scene_anchor.png", "场景风格 Anchor")
        ]
        logic.append("CG 没有可用业务参考图时，使用场景 Anchor 保持视觉风格。")
        mode = "image_to_image"
    else:
        logic.append("CG 没有可用参考图，退化为文生图。")
        mode = "text_to_image"
    return {
        "source": "reconstructed",
        "generation_mode": mode,
        "prompt": prompt,
        "reference_images": references,
        "logic": logic,
    }


def _reconstruct_background_debug(
    task_id: str,
    filename: str,
    source_data: Mapping[str, Any],
) -> Dict[str, Any]:
    backgrounds = source_data.get("backgrounds")
    record = None
    if isinstance(backgrounds, dict):
        if filename == "novel_cover.png":
            record = backgrounds.get("novel_cover_metadata")
        else:
            generated = backgrounds.get("generated_prompts")
            if isinstance(generated, dict):
                record = generated.get(Path(filename).stem)
    semantic = str((record or {}).get("prompt") or "").strip()
    plan = source_data.get("_scene_style_plan")
    styled = apply_asset_style(plan, semantic) if plan else semantic
    if plan and plan.uses_anchor:
        prompt = compose_anchor_guided_prompt(plan, styled)
        references = [
            _reference_record(task_id, "style_anchors/scene_anchor.png", "场景风格 Anchor")
        ]
        mode = "image_to_image"
    else:
        prompt = styled
        references = []
        mode = "text_to_image"
    return {
        "source": "reconstructed",
        "generation_mode": mode,
        "prompt": prompt,
        "reference_images": references,
        "logic": [
            "旧任务缺少生成期 sidecar，封面信息由 backgrounds 表和当前场景风格配置重建。"
        ],
    }


def _visual_asset_debug_from_context(
    task_id: str,
    category: str,
    filename: str,
    source_data: Mapping[str, Any],
) -> Dict[str, Any]:
    recorded = load_visual_generation_record(task_id, category, filename)
    if recorded:
        result = dict(recorded)
        result["reference_images"] = [
            _reference_payload(task_id, reference)
            for reference in (recorded.get("reference_images") or [])
            if isinstance(reference, dict)
        ]
    elif category == "characters":
        result = _reconstruct_character_debug(task_id, filename, source_data)
    elif category == "scenes":
        result = _reconstruct_scene_debug(task_id, filename, source_data)
    elif category == "cg":
        result = _reconstruct_cg_debug(task_id, filename, source_data)
    else:
        result = _reconstruct_background_debug(task_id, filename, source_data)

    result.update(
        task_id=task_id,
        category=category,
        filename=filename,
        image_url=_asset_url(task_id, category, filename),
        debug_url=(
            f"/debug?task_id={quote(task_id, safe='')}&category={quote(category, safe='')}"
            f"&filename={quote(filename, safe='')}"
        ),
    )
    result.setdefault("prompt", "")
    result.setdefault("reference_images", [])
    result.setdefault("logic", [])
    result.setdefault("generation_mode", "unknown")
    result.setdefault("model", "")
    result.setdefault("provider", "")
    result.setdefault("extra", {})
    return result


def get_visual_asset_debug(task_id: str, category: str, filename: str) -> Dict[str, Any]:
    """Return one local asset's prompt, references, and generation explanation."""
    require_reuse_feature()
    task_id = _validate_task_id(task_id)
    load_reuse_manifest(task_id)
    visual_asset_path(task_id, category, filename)
    return _visual_asset_debug_from_context(
        task_id,
        category,
        filename,
        _load_visual_source_data(task_id),
    )


def _stage_label(status: int) -> str:
    return {
        TaskStatus.PENDING: "pending",
        TaskStatus.PROCESSING: "processing",
        TaskStatus.COMPLETED: "completed",
        TaskStatus.FAILED: "failed",
    }.get(status, "unknown")


def list_visual_assets(task_id: str) -> Dict[str, Any]:
    """Return derived-task visual status and a local-file review gallery."""
    require_reuse_feature()
    task_id = _validate_task_id(task_id)
    manifest = load_reuse_manifest(task_id)
    row = _visual_task_row(task_id)
    if not row:
        raise ReusePipelineError(f"派生任务不存在: {task_id}")

    char_status = row["char_designer_status"]
    scene_status = row["scene_painter_status"]
    if TaskStatus.FAILED in {char_status, scene_status}:
        visual_state = "failed"
    elif char_status == scene_status == TaskStatus.COMPLETED:
        visual_state = "completed"
    elif (
        TaskStatus.PROCESSING in {char_status, scene_status}
        or row["overall_status"] == TaskStatus.PROCESSING
        or bool(row["celery_chain_id"])
    ):
        visual_state = "processing"
    else:
        visual_state = "pending"

    output_root = task_output_dir(task_id)
    source_data = _load_visual_source_data(task_id)
    assets: Dict[str, list] = {}
    for category, relative_dir in VISUAL_ASSET_CATEGORIES.items():
        directory = output_root / relative_dir
        items = []
        if directory.exists():
            for path in sorted(directory.iterdir(), key=lambda item: item.name):
                if not path.is_file() or path.suffix.lower() not in VISUAL_IMAGE_SUFFIXES:
                    continue
                stat = path.stat()
                encoded_name = quote(path.name, safe="")
                debug = _visual_asset_debug_from_context(
                    task_id,
                    category,
                    path.name,
                    source_data,
                )
                debug["image_url"] = (
                    f"/api/tasks/{task_id}/visual-assets/{category}/"
                    f"{encoded_name}?v={stat.st_mtime_ns}"
                )
                items.append(
                    {
                        "name": path.name,
                        "size": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime)
                        .astimezone()
                        .isoformat(),
                        "url": debug["image_url"],
                        "debug": debug,
                    }
                )
        assets[category] = items

    expected_counts = _expected_visual_asset_counts(source_data)
    generated_counts = {category: len(items) for category, items in assets.items()}
    generated_counts["characters"] = sum(
        1
        for item in assets.get("characters", [])
        if not re.match(r"^cg_.+_char_\d+\.", item["name"], re.I)
    )
    visual_progress_percentage, progress_breakdown = _visual_progress(
        visual_state,
        expected_counts,
        generated_counts,
    )
    style_config = load_visual_style_config(task_id, fetch_remote=False)

    return {
        "task_id": task_id,
        "source_task_id": manifest["source_task_id"],
        "project_name": row["project_name"],
        "image_model": row["image_model"],
        "visual_state": visual_state,
        "progress_percentage": row["progress_percentage"],
        "visual_progress_percentage": visual_progress_percentage,
        "progress_breakdown": progress_breakdown,
        "error_message": row["error_message"],
        "celery_chain_id": row["celery_chain_id"],
        "stages": {
            "story_planner": _stage_label(row["story_planner_status"]),
            "info_extractor": _stage_label(row["info_extractor_status"]),
            "char_designer": _stage_label(char_status),
            "scene_painter": _stage_label(scene_status),
            "audio_producer": _stage_label(row["audio_producer_status"]),
            "sound_arranger": _stage_label(row["sound_arranger_status"]),
            "game_builder": _stage_label(row["game_builder_status"]),
        },
        "assets": assets,
        "asset_count": sum(len(items) for items in assets.values()),
        "pipeline_scope": manifest["pipeline_scope"],
        REMOVE_CHARACTER_BACKGROUND_KEY: bool(
            style_config.get(REMOVE_CHARACTER_BACKGROUND_KEY, True)
        ),
    }


def visual_asset_path(task_id: str, category: str, filename: str) -> Path:
    """Resolve one whitelisted local visual asset without allowing traversal."""
    require_reuse_feature()
    task_id = _validate_task_id(task_id)
    load_reuse_manifest(task_id)
    if category not in VISUAL_ASSET_CATEGORIES:
        raise ReusePipelineError(f"不支持的资产分类: {category}")
    if not filename or Path(filename).name != filename:
        raise ReusePipelineError("非法资产文件名")
    path = task_output_dir(task_id) / VISUAL_ASSET_CATEGORIES[category] / filename
    if path.suffix.lower() not in VISUAL_IMAGE_SUFFIXES:
        raise ReusePipelineError("不支持的图片格式")
    if not path.exists() or not path.is_file():
        raise ReusePipelineError("图片不存在")
    return path
