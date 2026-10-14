"""场景 ID 纯工具：收集二级场景 ID、把实例 ID 还原为原图 ID。

被 scene（编号写入端）与 cg（提示词读取端）共享，保证两端使用同一套
ID 口径。仅依赖标准库，勿在此引入重依赖。
"""

from __future__ import annotations

from typing import Any


def collect_variant_scene_ids(locations: list[dict]) -> set[str]:
    """只收集二级场景图片 ID；一级底图不参与区域编号或复制。"""
    scene_ids: set[str] = set()
    for location in locations:
        if not isinstance(location, dict):
            continue
        for variant in location.get("variants", []):
            if not isinstance(variant, dict):
                continue
            variant_id = variant.get("scene_id")
            if variant_id:
                scene_ids.add(variant_id)
    return scene_ids


def resolve_base_scene_id(scene_id: Any, known_scene_ids: set[str]) -> str | None:
    """把 ``scene_x_7_2`` 还原为已登记的原图 ID ``scene_x_7``。

    先匹配完整 ID，避免把本来就带数字后缀的二级场景误当成实例。
    """
    if not isinstance(scene_id, str) or not scene_id:
        return None
    if scene_id in known_scene_ids:
        return scene_id

    candidate = scene_id
    while "_" in candidate:
        base, suffix = candidate.rsplit("_", 1)
        if not suffix.isdigit():
            break
        candidate = base
        if candidate in known_scene_ids:
            return candidate
    return None
