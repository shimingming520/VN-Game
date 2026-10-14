"""把场景原图复制成剧情连续区域实例。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from common.visual_checkpoint import (
    canonical_digest,
    file_sha256,
    mark_failed as checkpoint_mark_failed,
    mark_running as checkpoint_mark_running,
    mark_success as checkpoint_mark_success,
    reusable_or_adopt_existing,
)

try:
    from services.scene_painter.core.artifact_contract import (
        atomic_copy_image,
        validate_image_file,
    )
except ImportError:
    from artifact_contract import atomic_copy_image, validate_image_file  # type: ignore


logger = logging.getLogger(__name__)


def safe_scene_name(scene_id: str) -> str:
    return re.sub(r"[^\w\-]", "_", scene_id)


def parse_copy_count(scene_info: Any) -> int:
    if not isinstance(scene_info, dict):
        return 0
    try:
        return max(0, int(scene_info.get("copy_count", 0) or 0))
    except (TypeError, ValueError):
        return 0


def copy_scene_instances(scenes_data: dict, output_dir: str | Path) -> tuple[int, list[tuple[str, str]]]:
    """按 ``copy_count`` 复制原图，保留原文件并生成 ``_<序号>`` 实例。

    每次都会覆盖已有实例，确保原图重新生成后，下游使用的实例同步更新。
    返回 ``(成功复制张数, [(实例ID, 错误), ...])``。
    """
    output_path = Path(output_dir)
    task_id = output_path.resolve().parents[1].name
    copied = 0
    failures: list[tuple[str, str]] = []

    for scene_id, scene_info in scenes_data.items():
        copy_count = parse_copy_count(scene_info)
        if copy_count <= 0:
            continue

        safe_name = safe_scene_name(scene_id)
        source = output_path / f"{safe_name}.png"
        if not validate_image_file(source):
            error = f"原图不存在、为空或损坏: {source}"
            logger.error("[场景实例复制] %s: %s", scene_id, error)
            failures.extend((f"{scene_id}_{index}", error) for index in range(1, copy_count + 1))
            continue

        for index in range(1, copy_count + 1):
            instance_id = f"{scene_id}_{index}"
            target = output_path / f"{safe_scene_name(instance_id)}.png"
            dependency_digest = canonical_digest(
                {"source": source.name, "sha256": file_sha256(source)}
            )
            input_digest = canonical_digest(
                {"scene_id": scene_id, "instance_index": index}
            )
            try:
                decision = reusable_or_adopt_existing(
                    task_id,
                    "scene_painter",
                    "copy_scene_instance",
                    instance_id,
                    input_digest=input_digest,
                    generator_version="scene-instance-copy-v2-resume",
                    dependency_digest=dependency_digest,
                    output_paths=[target],
                    validator=validate_image_file,
                )
                if decision.reusable:
                    copied += 1
                    continue
                checkpoint_mark_running(
                    task_id,
                    "scene_painter",
                    "copy_scene_instance",
                    instance_id,
                    input_digest=input_digest,
                    generator_version="scene-instance-copy-v2-resume",
                    dependency_digest=dependency_digest,
                )
                atomic_copy_image(source, target)
                checkpoint_mark_success(
                    task_id,
                    "scene_painter",
                    "copy_scene_instance",
                    instance_id,
                    input_digest=input_digest,
                    generator_version="scene-instance-copy-v2-resume",
                    dependency_digest=dependency_digest,
                    output_paths=[target],
                    validator=validate_image_file,
                    result_mode="local_copy",
                )
                copied += 1
                logger.info("[场景实例复制] %s -> %s", source.name, target.name)
            except Exception as exc:
                try:
                    checkpoint_mark_failed(
                        task_id,
                        "scene_painter",
                        "copy_scene_instance",
                        instance_id,
                        input_digest=input_digest,
                        generator_version="scene-instance-copy-v2-resume",
                        dependency_digest=dependency_digest,
                        error=str(exc),
                    )
                except Exception:
                    logger.exception("记录场景实例复制失败 checkpoint 时发生异常")
                failures.append((instance_id, str(exc)))
                logger.error("[场景实例复制] %s 失败: %s", instance_id, exc)

    return copied, failures
