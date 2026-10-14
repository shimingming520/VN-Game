"""场景景别图片处理与热更新。

景别修改会同名覆盖 outputs、built_games 对应节点和 OSS 中的剧情区域实例图
（如 scene_room_7_1.png）。裁剪源始终使用保留的二级场景原图
（scene_room_7.png），避免多次切换景别时重复累积裁剪。
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from PIL import Image


SHOT_CROP_RATIOS = {
    "LS": 1.0,
    "MS": 0.85,
    "CS": 0.70,
}


class SceneShotUpdateError(RuntimeError):
    """景别图片处理失败。"""


class SceneShotUploadError(SceneShotUpdateError):
    """景别图片上传失败。"""


@dataclass(frozen=True)
class SceneShotImageResult:
    scene_id: str
    original_scene_id: str
    shot_size: str
    crop_ratio: float
    crop_width: int
    crop_height: int
    output_width: int
    output_height: int
    local_path: str
    built_node_id: str
    built_image_path: str
    service_worker_path: Optional[str]
    service_worker_updated: bool
    oss_key: str
    mirror_uploaded: int


def extract_variant_scene_ids(scenes_data) -> set[str]:
    """从 scenes 两级结构中提取二级场景原图 ID。"""
    result: set[str] = set()
    if not isinstance(scenes_data, list):
        return result
    for location in scenes_data:
        if not isinstance(location, dict):
            continue
        for variant in location.get("variants", []):
            if not isinstance(variant, dict):
                continue
            scene_id = (variant.get("scene_id") or "").strip()
            if scene_id:
                result.add(scene_id)
    return result


def resolve_original_scene_id(scene_id: str, known_variant_ids: Iterable[str]) -> str:
    """把区域实例 ID 还原为已登记的二级原图 ID。

    完整 ID 优先匹配，因此 ``scene_room_7`` 不会被误删成一级场景 ``scene_room``；
    ``scene_room_7_2`` 则会命中 ``scene_room_7``。
    """
    known = set(known_variant_ids)
    if scene_id in known:
        return scene_id

    candidate = scene_id
    while "_" in candidate:
        base, suffix = candidate.rsplit("_", 1)
        if not suffix.isdigit():
            break
        candidate = base
        if candidate in known:
            return candidate
    return scene_id


def render_scene_shot(source_data: bytes, shot_size: str) -> tuple[bytes, int, int, int, int]:
    """中心裁取指定比例并缩放回原分辨率，返回 PNG bytes 和尺寸信息。"""
    shot = (shot_size or "").strip().upper()
    if shot not in SHOT_CROP_RATIOS:
        raise SceneShotUpdateError(f"不支持的场景景别: {shot_size}")
    if not source_data:
        raise SceneShotUpdateError("场景原图为空")

    ratio = SHOT_CROP_RATIOS[shot]
    if ratio == 1.0:
        with Image.open(io.BytesIO(source_data)) as probe:
            width, height = probe.size
        return source_data, width, height, width, height

    try:
        with Image.open(io.BytesIO(source_data)) as source:
            source.load()
            width, height = source.size
            crop_width = max(1, round(width * ratio))
            crop_height = max(1, round(height * ratio))
            left = (width - crop_width) // 2
            top = (height - crop_height) // 2
            cropped = source.crop((left, top, left + crop_width, top + crop_height))
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            output = cropped.resize((width, height), resampling)

            buffer = io.BytesIO()
            output.save(buffer, format="PNG")
            return buffer.getvalue(), width, height, crop_width, crop_height
    except SceneShotUpdateError:
        raise
    except Exception as exc:
        raise SceneShotUpdateError(f"场景图片裁剪失败: {exc}") from exc


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import time

    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temp_path.write_bytes(data)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _read_optional_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes() if path.exists() else None
    except OSError as exc:
        raise SceneShotUpdateError(f"读取待覆盖文件失败: {path}: {exc}") from exc


def _restore_file(path: Path, previous_data: Optional[bytes]) -> None:
    try:
        if previous_data is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, previous_data)
    except OSError:
        # 保留原始异常；回滚失败由调用方日志捕获更合适。
        pass


def _resolve_built_scene_paths(
    task_id: str,
    node_id: str,
    filename: str,
    built_games_path: str | Path,
) -> tuple[str, Path, Optional[Path]]:
    """定位节点游戏包中的同名场景图和 service worker。"""
    from services.api_gateway.core.cast_updater import (
        CastUpdateError,
        _resolve_built_script,
    )

    task_dir = Path(built_games_path) / task_id
    if not task_dir.is_dir():
        raise SceneShotUpdateError(f"已构建游戏目录不存在: {task_dir}")
    try:
        script_path, built_node_id, _source_node_id = _resolve_built_script(task_dir, node_id)
    except CastUpdateError as exc:
        raise SceneShotUpdateError(str(exc)) from exc

    game_dir = script_path.parent.parent
    built_image_path = game_dir / "assets" / "scenes" / filename
    service_worker_path = game_dir / "service-worker.js"
    if not service_worker_path.is_file():
        service_worker_path = None
    return built_node_id, built_image_path, service_worker_path


def _patch_scene_service_worker(content: str) -> str:
    """刷新缓存版本，并让新缓存首次拉取时绕过浏览器 HTTP 缓存。"""
    from services.api_gateway.core.cast_updater import _patch_service_worker

    patched = _patch_service_worker(content, prefix="scene")
    old_fetch = "fetch (event.request)"
    if old_fetch in patched:
        patched = patched.replace(
            old_fetch,
            "fetch (new Request(event.request, { cache: 'reload' }))",
            1,
        )
    return patched


def _default_main_uploader(task_id: str, node_id: str, filename: str, data: bytes) -> str:
    from common import oss_utils
    from common.node_utils import db_to_oss_node_id

    oss_node_id = db_to_oss_node_id(node_id, task_id)
    oss_key = oss_utils.oss_key("scenes", task_id, oss_node_id, filename)
    if not oss_utils.oss_write(oss_key, data, content_type="image/png"):
        raise SceneShotUploadError(f"场景景别图片上传 OSS 失败: {oss_key}")
    return oss_key


def _default_mirror_uploader(local_path: Path) -> int:
    from common.output_mirror import mirror_paths

    return mirror_paths([local_path], force=True)


def apply_scene_shot_image(
    task_id: str,
    node_id: str,
    scene_id: str,
    shot_size: str,
    known_variant_ids: Iterable[str],
    outputs_path: str | Path,
    built_games_path: str | Path,
    main_uploader: Optional[Callable[[str, str, str, bytes], str]] = None,
    mirror_uploader: Optional[Callable[[Path], int]] = None,
) -> SceneShotImageResult:
    """从二级原图生成景别实例图，并热更新 outputs、游戏包与 OSS。

    游戏包使用同一个图片文件名，因此不修改 script.js；只覆盖对应节点的
    ``assets/scenes/{scene_id}.png`` 并刷新 service-worker 缓存版本。在线构建
    默认会删除本地 assets，本函数会按相同路径重新落一份，OSS 主路径仍同步覆盖。
    任一步失败时恢复 outputs、游戏包图片与 service worker。
    """
    if not isinstance(scene_id, str) or not scene_id.strip():
        raise SceneShotUpdateError("beat 缺少有效的 scene_id")
    scene_id = scene_id.strip()
    if Path(scene_id).name != scene_id or "/" in scene_id or "\\" in scene_id:
        raise SceneShotUpdateError(f"非法 scene_id: {scene_id}")

    scene_dir = Path(outputs_path) / task_id / "images" / "scenes"
    original_scene_id = resolve_original_scene_id(scene_id, known_variant_ids)
    source_path = scene_dir / f"{original_scene_id}.png"
    target_path = scene_dir / f"{scene_id}.png"
    built_node_id, built_image_path, service_worker_path = _resolve_built_scene_paths(
        task_id=task_id,
        node_id=node_id,
        filename=target_path.name,
        built_games_path=built_games_path,
    )

    # 兼容旧任务中 beat 直接指向二级原图的情况：首次调整时把原图备份到
    # OUTPUTS_PATH 同级目录，避免后续 CS -> MS 再从已裁剪图片继续裁剪。
    if source_path == target_path:
        backup_path = (
            Path(outputs_path).parent
            / "scene_shot_originals"
            / task_id
            / f"{scene_id}.png"
        )
        if backup_path.exists():
            source_path = backup_path
        elif target_path.exists():
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_bytes(target_path.read_bytes())
            source_path = backup_path

    if not source_path.exists() or source_path.stat().st_size <= 0:
        raise SceneShotUpdateError(f"二级场景原图不存在: {source_path}")

    try:
        source_data = source_path.read_bytes()
    except OSError as exc:
        raise SceneShotUpdateError(f"读取二级场景原图失败: {source_path}: {exc}") from exc
    output_data, width, height, crop_width, crop_height = render_scene_shot(
        source_data,
        shot_size,
    )

    from services.api_gateway.core.cast_updater import _UPDATE_LOCK

    service_worker_updated = False

    upload_main = main_uploader or _default_main_uploader
    upload_mirror = mirror_uploader or _default_mirror_uploader
    with _UPDATE_LOCK:
        previous_target = _read_optional_bytes(target_path)
        previous_built = _read_optional_bytes(built_image_path)
        previous_sw = _read_optional_bytes(service_worker_path) if service_worker_path else None
        try:
            _atomic_write(target_path, output_data)
            _atomic_write(built_image_path, output_data)

            if service_worker_path is not None and previous_sw is not None:
                old_sw = previous_sw.decode("utf-8")
                new_sw = _patch_scene_service_worker(old_sw)
                if new_sw != old_sw:
                    _atomic_write(service_worker_path, new_sw.encode("utf-8"))
                    service_worker_updated = True

            oss_key = upload_main(task_id, node_id, target_path.name, output_data)
            mirror_uploaded = upload_mirror(target_path)
        except Exception as exc:
            _restore_file(target_path, previous_target)
            _restore_file(built_image_path, previous_built)
            if service_worker_path is not None:
                _restore_file(service_worker_path, previous_sw)
            if isinstance(exc, SceneShotUpdateError):
                raise
            if isinstance(exc, (OSError, UnicodeError)):
                raise SceneShotUpdateError(f"场景景别文件热更新失败: {exc}") from exc
            raise SceneShotUploadError(f"场景景别图片上传失败: {exc}") from exc

    shot = shot_size.strip().upper()
    return SceneShotImageResult(
        scene_id=scene_id,
        original_scene_id=original_scene_id,
        shot_size=shot,
        crop_ratio=SHOT_CROP_RATIOS[shot],
        crop_width=crop_width,
        crop_height=crop_height,
        output_width=width,
        output_height=height,
        local_path=str(target_path),
        built_node_id=built_node_id,
        built_image_path=str(built_image_path),
        service_worker_path=str(service_worker_path) if service_worker_path else None,
        service_worker_updated=service_worker_updated,
        oss_key=oss_key,
        mirror_uploaded=mirror_uploaded,
    )
