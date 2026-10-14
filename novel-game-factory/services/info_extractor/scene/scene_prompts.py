"""
场景提示词生成器 - 从 scenes 表读取合并场景，生成 image_prompt，写入 scene_prompts 表。

该模块为流水线第四步（在 scene_to_plot.py 回填剧情表之后执行）：
  - 从 scenes 表读取合并后的两级场景结构（一级场景点 + 二级变体）
  - 为每个 scene_id（一级底图 + 每个变体）调用 LLM 生成英文 image_prompt
  - 将结果 {scene_id: {source_node, image_prompt, copy_count}} 写入 scene_prompts 表

用法：
    python core/scene_prompts.py --task-id xxx
"""

from __future__ import annotations

import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

service_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if service_dir not in sys.path:
    sys.path.insert(0, service_dir)

import json
import asyncio
import argparse
import logging
from pathlib import Path

import yaml
from pypinyin import lazy_pinyin
from dotenv import load_dotenv
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_random_exponential
from sqlalchemy import create_engine, text

load_dotenv(dotenv_path=Path(project_root) / ".env", override=True)

from common.config import DATABASE_URL

try:
    from common.usage_tracker import tracker as _usage_tracker
except ImportError:
    _usage_tracker = None

API_KEY = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
BASE_URL = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com").rstrip("/")
if not BASE_URL.endswith("/v1"):
    BASE_URL = f"{BASE_URL}/v1"
MODEL_NAME = "gpt-5.6-sol"
MAX_CONCURRENT_REQUESTS = 5
# 生成后核验+补空的最大轮数（含首轮全量生成）。
# 每轮只对 image_prompt 仍为空的 scene_id 重新调 LLM，用尽仍空则告警放行。
MAX_VERIFY_ATTEMPTS = 3

SERVICE_DIR = Path(__file__).resolve().parent.parent
PROMPT_DIR = SERVICE_DIR / "prompts" / "scene_prompts"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scene_prompts")


def _load_prompt(filename: str) -> str:
    with open(PROMPT_DIR / filename, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["content"].strip()


# ============================================================
# 数据库读写
# ============================================================

def fetch_scenes_from_db(task_id: str) -> list:
    """从 scenes 表读取合并后的两级场景结构。"""
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM scenes WHERE task_id = :tid"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        logger.error(f"❌ scenes 表中未找到 task_id={task_id}")
        return []
    data = row[0] if isinstance(row[0], (dict, list)) else json.loads(row[0])
    if isinstance(data, dict):
        logger.warning("⚠️ scenes 表数据为旧 dict 格式，期望 list")
        return []
    logger.info(f"✅ 读取 scenes，场景点数={len(data)}")
    return data


def ensure_scene_prompts_table(engine) -> None:
    """确保 scene_prompts 表存在（首次运行自动建表）。"""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS scene_prompts (
                id          SERIAL PRIMARY KEY,
                task_id     VARCHAR(255) NOT NULL UNIQUE,
                data        JSONB,
                create_time TIMESTAMP NOT NULL DEFAULT NOW(),
                update_time TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))


def save_to_scene_prompts_table(task_id: str, scene_table: dict) -> int | None:
    """将 {scene_id: {source_node, image_prompt, copy_count}} 写入平台库。"""
    from common.config import PLATFORM_DATABASE_URL
    engine = create_engine(PLATFORM_DATABASE_URL)
    ensure_scene_prompts_table(engine)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "SELECT setval('scene_prompts_id_seq', COALESCE((SELECT MAX(id) FROM scene_prompts), 1), "
                "(SELECT MAX(id) FROM scene_prompts) IS NOT NULL)"
            ))
            sql = text("""
                INSERT INTO scene_prompts (task_id, data, create_time, update_time)
                VALUES (:tid, :data, NOW(), NOW())
                ON CONFLICT (task_id) DO UPDATE SET
                    data = EXCLUDED.data,
                    update_time = NOW()
                RETURNING id
            """)
            result = conn.execute(sql, {
                "tid": task_id,
                "data": json.dumps(scene_table, ensure_ascii=False),
            })
            new_id = result.fetchone()[0]
            logger.info(f"💾 [ScenePrompts] 入库成功! DB_ID={new_id}, 条目数={len(scene_table)}")
            return new_id
    except Exception as e:
        logger.error(f"❌ [ScenePrompts] 入库失败: {e}")
        return None


# ============================================================
# 构建 specs（摊平两级结构）
# ============================================================

def node_from_ref(ref: str) -> str:
    parts = ref.rsplit("_", 2)
    return parts[0] if len(parts) == 3 else ref


def first_source_node(refs: list) -> str:
    for r in refs:
        if r:
            return node_from_ref(r)
    return ""


def _copy_count(record: dict) -> int:
    """读取 scene_to_plot 写入的复制次数，异常旧数据按 0 处理。"""
    try:
        return max(0, int(record.get("copy_count", 0) or 0))
    except (TypeError, ValueError):
        return 0


def build_specs(locations: list) -> list:
    """将两级结构摊平为待生成记录列表，并透传原图复制次数。"""
    specs = []
    for loc in locations:
        name = loc.get("name", "")
        detail = loc.get("detail", "")
        variants = loc.get("variants", [])

        loc_node = first_source_node(variants[0].get("refs", [])) if variants else ""
        specs.append({
            "key": loc["scene_id"],
            "source_node": loc_node,
            "copy_count": _copy_count(loc),
            "fields": {
                "location_name": name,
                "location_detail": detail,
                "position": "整体空间",
                "time": "",
                "weather": "",
                "shot": "以中景~远景平视展现整个大空间，交代其固定通用结构，作为可复用的通用底图",
                "variant_increment": "（无，这是大空间通用底图）",
                "light": "",
                "atmosphere": "",
            },
        })

        for v in variants:
            # 未被任何 plot 使用的变体（is_active=False）不生成提示词，最终不出图。
            # is_active 由 scene_to_plot 的 mark_active_variants 标注；缺字段时默认生成，
            # 避免旧数据或未跑对齐的数据被误删。
            if v.get("is_active", True) is False:
                logger.info(f"⏭️  跳过未使用变体 {v.get('scene_id')}（is_active=False）")
                continue
            specs.append({
                "key": v["scene_id"],
                "source_node": first_source_node(v.get("refs", [])),
                "copy_count": _copy_count(v),
                "fields": {
                    "location_name": name,
                    "location_detail": detail,
                    "position": v.get("position", ""),
                    "time": v.get("time", ""),
                    "weather": v.get("weather", ""),
                    "shot": v.get("shot", ""),
                    "variant_increment": v.get("increment", ""),
                    "light": v.get("light", ""),
                    "atmosphere": v.get("atmosphere", ""),
                },
            })
    return specs


# ============================================================
# LLM 生成 image_prompt
# ============================================================

@retry(stop=stop_after_attempt(5), wait=wait_random_exponential(min=1, max=60))
async def gen_image_prompt(client, semaphore, sys_prompt, user_template, fields: dict) -> str:
    user_message = user_template
    for k, v in fields.items():
        user_message = user_message.replace("{" + k + "}", v or "（无）")

    async with semaphore:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.6,
        )
    if _usage_tracker and getattr(response, "usage", None):
        _usage_tracker.add_llm_usage(
            response.usage, MODEL_NAME, provider="openai_compatible"
        )
    return response.choices[0].message.content.strip().strip('"').strip()


# ============================================================
# 主流程
# ============================================================

async def main(task_id: str) -> None:
    if not API_KEY:
        logger.error("❌ 未设置 GPT_IMAGE_API_KEY（或 OPENAI_API_KEY）。")
        return

    locations = fetch_scenes_from_db(task_id)
    if not locations:
        return

    specs = build_specs(locations)
    logger.info(f"🧱 摊平为 {len(specs)} 条记录（{len(locations)} 个场景点）")

    sys_prompt = _load_prompt("scene_prompts_sys_zh.yaml")
    user_template = _load_prompt("scene_prompts_user_zh.yaml")

    # 先按 specs 顺序建好整表骨架（image_prompt 先留空），后续按 key 回填。
    table = {
        spec["key"]: {
            "source_node": spec["source_node"],
            "image_prompt": "",
            "copy_count": spec["copy_count"],
        }
        for spec in specs
    }
    spec_by_key = {spec["key"]: spec for spec in specs}

    async with AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL) as client:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async def _gen(spec):
            try:
                prompt = await gen_image_prompt(client, semaphore, sys_prompt, user_template, spec["fields"])
            except Exception as e:
                logger.error(f"  ❌ {spec['key']} 生成 image_prompt 失败: {e}")
                prompt = ""
            return spec["key"], prompt

        # 循环核验+补空：首轮生成全部，之后每轮只对 image_prompt 仍为空的
        # scene_id 重新调 LLM（避免重生成已成功的，省 API 与时间）；每轮结束
        # 落库一次。用尽 MAX_VERIFY_ATTEMPTS 轮仍有空则告警放行，不阻断本步，
        # 空洞交由下游 char_designer 预检暴露。
        for attempt in range(1, MAX_VERIFY_ATTEMPTS + 1):
            pending = [
                spec_by_key[k]
                for k, v in table.items()
                if not str(v.get("image_prompt") or "").strip()
            ]
            if not pending:
                break
            logger.info(
                f"🔁 第 {attempt}/{MAX_VERIFY_ATTEMPTS} 轮场景提示词生成："
                f"待生成 {len(pending)}/{len(table)} 条"
            )
            results = await asyncio.gather(*[_gen(s) for s in pending])
            for key, prompt in results:
                if str(prompt or "").strip():
                    table[key]["image_prompt"] = prompt
            save_to_scene_prompts_table(task_id, table)

    empty_keys = [
        k for k, v in table.items()
        if not str(v.get("image_prompt") or "").strip()
    ]
    if empty_keys:
        logger.warning(
            f"⚠️ 场景提示词生成 {MAX_VERIFY_ATTEMPTS} 轮后仍有 {len(empty_keys)} 条 "
            f"image_prompt 为空 (task_id={task_id})，放行交下游预检: "
            f"{empty_keys[:20]}{' ...' if len(empty_keys) > 20 else ''}"
        )
    else:
        logger.info(f"✅ image_prompt 全部生成完成！共 {len(table)} 条，无空洞")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="场景提示词生成器（写入 scene_prompts 表）")
    parser.add_argument("--task-id", type=str, required=True, help="任务 task_id")
    args = parser.parse_args()
    asyncio.run(main(args.task_id))
