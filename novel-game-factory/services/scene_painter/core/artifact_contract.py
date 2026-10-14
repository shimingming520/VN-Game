"""Scene image validation, atomic publication, and final-output oracle."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import shutil
from typing import Callable, Dict, Iterator, Mapping, Optional

from PIL import Image
from sqlalchemy import text

from common.artifact_io import atomic_output_path
from common.config import OUTPUTS_PATH
from common.platform_db import platform_engine


class SceneImageArtifactError(RuntimeError):
    """Raised when a required scene-painter output is missing or damaged."""


OwnershipCheck = Callable[[], object]


def validate_image_file(path: str | Path) -> bool:
    image_path = Path(path)
    try:
        if not image_path.is_file() or image_path.stat().st_size <= 0:
            return False
        with Image.open(image_path) as image:
            image.verify()
        with Image.open(image_path) as image:
            image.load()
            width, height = image.size
            if width <= 0 or height <= 0:
                return False
            if not image.format:
                return False
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def require_valid_image(path: str | Path, *, label: str = "scene image") -> Path:
    image_path = Path(path)
    if not validate_image_file(image_path):
        raise SceneImageArtifactError(f"invalid {label}: {image_path}")
    return image_path


@contextmanager
def atomic_image_output(
    final_path: str | Path,
    *,
    ownership_check: Optional[OwnershipCheck] = None,
) -> Iterator[Path]:
    def _validator(path: str | Path) -> bool:
        if not validate_image_file(path):
            return False
        check = ownership_check
        if check is None:
            from common.progress_utils import ensure_inherited_stage_lease_owned

            check = ensure_inherited_stage_lease_owned
        if check() is False:
            return False
        return True

    with atomic_output_path(final_path, validator=_validator) as temporary:
        yield temporary


def atomic_copy_image(source: str | Path, target: str | Path) -> Path:
    source_path = require_valid_image(source, label="source scene image")
    target_path = Path(target)
    with atomic_image_output(target_path) as temporary:
        shutil.copy2(source_path, temporary)
    return target_path


def _decode_json(value: object) -> Dict:
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value) if isinstance(value, Mapping) else {}


def _load_platform_json(task_id: str, table: str) -> Dict:
    if table not in {"scene_prompts", "backgrounds", "cg"}:
        raise ValueError(f"unsupported artifact table: {table}")
    order = " ORDER BY create_time DESC" if table == "scene_prompts" else ""
    with platform_engine.connect() as connection:
        row = connection.execute(
            text(f"SELECT data FROM {table} WHERE task_id = :task_id{order} LIMIT 1"),
            {"task_id": task_id},
        ).fetchone()
    return _decode_json(row[0] if row else None)


def _safe_name(value: str) -> str:
    return re.sub(r"[^\w\-]", "_", value)


def validate_scene_output_contract(
    task_id: str,
    *,
    outputs_root: str | Path = OUTPUTS_PATH,
    include_cg: bool = True,
    allow_partial: bool = False,
) -> list[dict[str, str]]:
    """Validate the existing public scene/background/CG output contract.

    No new manifest or external path is introduced.  Expected files are
    derived from the same platform rows already consumed by the generators.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise SceneImageArtifactError("task_id must be a non-empty string")

    task_dir = Path(outputs_root).resolve() / task_id
    gaps: list[dict[str, str]] = []

    def check(path: Path, *, label: str, asset_type: str, asset_id: str) -> None:
        if validate_image_file(path):
            return
        if allow_partial:
            gaps.append(
                {
                    "asset_id": asset_id,
                    "asset_type": asset_type,
                    "path": str(path.relative_to(task_dir).as_posix()),
                    "reason_code": "not_generated",
                    "last_error": f"missing or invalid {label}",
                }
            )
            return
        require_valid_image(path, label=label)

    scenes = _load_platform_json(task_id, "scene_prompts")
    scenes = {
        scene_id: spec
        for scene_id, spec in scenes.items()
        if not (isinstance(scene_id, str) and scene_id.startswith("cg_"))
    }
    if not scenes:
        raise SceneImageArtifactError(f"scene_prompts is empty: task_id={task_id}")

    scene_dir = task_dir / "images" / "scenes"
    for scene_id, spec in scenes.items():
        check(
            scene_dir / f"{_safe_name(str(scene_id))}.png",
            label=f"scene {scene_id}",
            asset_type="scene",
            asset_id=str(scene_id),
        )
        try:
            copy_count = max(0, int((spec or {}).get("copy_count", 0) or 0))
        except (AttributeError, TypeError, ValueError):
            copy_count = 0
        for index in range(1, copy_count + 1):
            instance_id = f"{scene_id}_{index}"
            check(
                scene_dir / f"{_safe_name(instance_id)}.png",
                label=f"scene instance {instance_id}",
                asset_type="scene_instance",
                asset_id=instance_id,
            )

    backgrounds = _load_platform_json(task_id, "backgrounds")
    chapter_prompts = backgrounds.get("generated_prompts") or {}
    if not isinstance(chapter_prompts, Mapping) or not chapter_prompts:
        raise SceneImageArtifactError(f"background prompts are empty: task_id={task_id}")
    background_dir = task_dir / "images" / "backgrounds"
    for node_id in chapter_prompts:
        check(
            background_dir / f"{node_id}.png",
            label=f"chapter cover {node_id}",
            asset_type="chapter_cover",
            asset_id=str(node_id),
        )
    novel_cover_prompt = (
        (backgrounds.get("novel_cover_metadata") or {}).get("prompt")
        if isinstance(backgrounds.get("novel_cover_metadata") or {}, Mapping)
        else None
    )
    if str(novel_cover_prompt or "").strip():
        check(
            background_dir / "novel_cover.png",
            label="novel cover",
            asset_type="novel_cover",
            asset_id="novel_cover",
        )

    if not include_cg:
        return

    cg_data = _load_platform_json(task_id, "cg")
    cg_dir = task_dir / "images" / "cg"
    for cg_id, spec in cg_data.items():
        if not isinstance(spec, Mapping) or not str(spec.get("cg_prompt") or "").strip():
            continue
        check(
            cg_dir / f"{_safe_name(str(cg_id))}.png",
            label=f"CG {cg_id}",
            asset_type="cg",
            asset_id=str(cg_id),
        )
    return gaps
