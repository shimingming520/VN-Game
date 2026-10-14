"""
人物状态组合描述生成器 (Combination Description Generator)
为 characters 表中每个角色的 valid_status_combinations 填充 description 字段，
描述基于对应的身体变体（whole_body_variants）和服装变体（clothing_variants），
不包含性格变体。

数据流：characters 表 -> 生成描述 -> 更新 characters 表

# 自动选最新任务
python core/extract_combination_description.py

# 指定任务
python novel-game-factory/services/char_designer/core/extract_combination_description.py --task-id

"""

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from common.config import DATABASE_URL, PLATFORM_DATABASE_URL
from sqlalchemy import create_engine, text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _parse_combination(combination: str) -> Tuple[str, int, int, int]:
    """解析 combination 字符串，返回 (name, personality_id, whole_body_id, clothing_id)。
    使用 rsplit 从右侧分割，兼容名字中含连字符的情况。
    """
    parts = combination.rsplit("-", 3)
    if len(parts) != 4:
        raise ValueError(f"无效的 combination 格式: {combination}")
    name, p_id, b_id, c_id = parts
    return name, int(p_id), int(b_id), int(c_id)


def _build_description(body: Dict[str, Any], clothing: Dict[str, Any]) -> str:
    """根据身体变体和服装变体拼装描述文本。"""
    parts: List[str] = []

    # ── 身份信息 ──────────────────────────────────────────────────────
    identity = [
        p for p in [
            body.get("age_category", ""),
            body.get("gender", ""),
            body.get("race", ""),
        ] if p
    ]
    if identity:
        parts.append(", ".join(identity))

    # ── 外貌 ──────────────────────────────────────────────────────────
    appearance = body.get("appearance", {})
    head = appearance.get("head", {})
    body_info = appearance.get("body", {})

    head_parts: List[str] = []
    if head.get("hair_color"):
        hair = head["hair_color"]
        if head.get("hair_style"):
            hair = f"{head['hair_style']} {hair} hair"
        else:
            hair = f"{hair} hair"
        head_parts.append(hair)
    if head.get("eye_color"):
        head_parts.append(f"{head['eye_color']} eyes")
    if head.get("skin_color"):
        head_parts.append(f"{head['skin_color']} skin")
    if head.get("facial_description"):
        head_parts.append(head["facial_description"])
    if head_parts:
        parts.append(", ".join(head_parts))

    if body_info.get("body_type"):
        parts.append(f"{body_info['body_type']} build")
    if body_info.get("body_description"):
        parts.append(body_info["body_description"])

    # ── 服装 ──────────────────────────────────────────────────────────
    if clothing.get("occupation"):
        parts.append(f"occupation: {clothing['occupation']}")
    if clothing.get("outfit_discription"):
        parts.append(f"wearing {clothing['outfit_discription']}")

    accessories = clothing.get("accessories") or []
    if isinstance(accessories, list) and accessories:
        parts.append(f"accessories: {', '.join(accessories)}")

    weapons = clothing.get("weapons") or []
    if isinstance(weapons, list) and weapons:
        parts.append(f"weapons: {', '.join(weapons)}")

    armor = clothing.get("armor") or []
    if isinstance(armor, list) and armor:
        parts.append(f"armor: {', '.join(armor)}")

    return ". ".join(parts)


def _find_latest_task_id() -> str:
    engine = create_engine(PLATFORM_DATABASE_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT task_id FROM characters ORDER BY id DESC LIMIT 1")
        ).fetchone()
    if not row:
        raise RuntimeError("characters 表中未找到任何任务")
    logger.info(f"自动选取最新任务: {row[0]}")
    return row[0]


def apply_combination_descriptions(data: Dict[str, Any]) -> int:
    """就地为角色数据字典填充每个 valid_status_combinations 的 description / height。

    纯内存操作，不涉及数据库。data 结构为 {char_key: char_data}。

    Returns:
        更新的 combination 总数
    """
    total_updated = 0

    for char_key, char_data in data.items():
        if not isinstance(char_data, dict):
            continue

        body_variants: List[Dict] = char_data.get("whole_body_variants", [])
        clothing_variants: List[Dict] = char_data.get("clothing_variants", [])
        combinations: List[Dict] = char_data.get("valid_status_combinations", [])

        body_map = {
            v["whole_body_id"]: v
            for v in body_variants
            if isinstance(v, dict) and "whole_body_id" in v
        }
        clothing_map = {
            v["clothing_id"]: v
            for v in clothing_variants
            if isinstance(v, dict) and "clothing_id" in v
        }

        for combo in combinations:
            if not isinstance(combo, dict):
                continue
            combination_str = combo.get("combination", "")
            if not combination_str:
                continue

            try:
                _, _, b_id, c_id = _parse_combination(combination_str)
            except (ValueError, IndexError):
                logger.warning(f"跳过无效 combination: {combination_str}")
                continue

            body = body_map.get(b_id, {})
            clothing = clothing_map.get(c_id, {})
            combo["description"] = _build_description(body, clothing)
            combo["height"] = str(body.get("height", "")).strip()
            total_updated += 1

    return total_updated


def build_combination_descriptions(task_id: Optional[str] = None) -> int:
    """
    为指定 task_id 的所有角色的 valid_status_combinations 生成并写入 description。
    task_id 为 None 时自动选取最新任务。

    Returns:
        更新的 combination 总数
    """
    if task_id is None:
        task_id = _find_latest_task_id()

    engine = create_engine(PLATFORM_DATABASE_URL)

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM characters WHERE task_id = :tid"),
            {"tid": task_id},
        ).fetchone()

    if not row:
        raise RuntimeError(f"未找到 task_id={task_id} 的角色数据")

    data = row[0]
    if isinstance(data, str):
        data = json.loads(data)

    total_updated = apply_combination_descriptions(data)

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE characters SET data = cast(:d as jsonb), update_time = NOW() "
                "WHERE task_id = :tid"
            ),
            {"d": json.dumps(data, ensure_ascii=False), "tid": task_id},
        )

    logger.info(f"已更新 {total_updated} 个 combination 描述 (task_id={task_id})")
    return total_updated


if __name__ == "__main__":
    import argparse

    try:
        from dotenv import load_dotenv
        dotenv_path = _project_root / "novel-game-factory" / ".env"
        if dotenv_path.exists():
            load_dotenv(dotenv_path=dotenv_path)
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="为 valid_status_combinations 生成描述（覆盖写入）")
    parser.add_argument("--task-id", default=None, help="任务 ID，不指定则自动选取最新任务")
    args = parser.parse_args()

    try:
        count = build_combination_descriptions(args.task_id)
        print(f"完成，共更新 {count} 个 combination 描述")
        sys.exit(0)
    except Exception as e:
        logger.error(f"执行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
