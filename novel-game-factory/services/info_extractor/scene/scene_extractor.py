"""
场景提取器 - 合并「原始场景提取」与「场景去重归并」两个阶段，通过数据库解耦。

两个阶段互不共享内存，仅通过 scenes 表交接数据：
  - raw   阶段 (run_extract_raw): 从故事树/正文提取原始场景列表 -> 写入 scenes.raw 列
  - merge 阶段 (run_merge):       从 scenes.raw 列读取 -> 语义聚类归并 -> 写入 scenes.data 列

用法：
    python scene_extractor.py --task-id xxx                # 完整链路 (raw -> merge)
    python scene_extractor.py --task-id xxx --stage raw    # 仅提取原始场景写入 raw 列
    python scene_extractor.py --task-id xxx --stage merge  # 仅从 raw 列读取归并写入 data 列
"""

import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

service_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if service_dir not in sys.path:
    sys.path.insert(0, service_dir)

import re
import json
import asyncio
import argparse
import logging
from pathlib import Path

import yaml
import json_repair
from pypinyin import lazy_pinyin
from dotenv import load_dotenv
from openai import AsyncOpenAI
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
    wait_fixed,
    retry_if_exception_type,
)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, DBAPIError

try:
    from info_extractor.utils.story_tree_json_parse import load_story_tree_for_player
except ImportError:
    from utils.story_tree_json_parse import load_story_tree_for_player  # type: ignore

load_dotenv(dotenv_path=Path(project_root) / ".env", override=True)

from common.config import DATABASE_URL

try:
    from common.usage_tracker import tracker as _usage_tracker
except ImportError:
    _usage_tracker = None

# ============================================================
API_KEY = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
BASE_URL = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com").rstrip("/")
if not BASE_URL.endswith("/v1"):
    BASE_URL = f"{BASE_URL}/v1"
MODEL_NAME = "gpt-5.6-sol"

MAX_CONCURRENT_REQUESTS = 5

CHUNK_SIZE = 3500
CHUNK_OVERLAP = 500
# ============================================================

TIME_ENUM = ["dawn", "morning", "noon", "afternoon", "dusk", "night"]
WEATHER_ENUM = ["clear", "cloudy", "rain", "snow", "fog", "storm"]

SERVICE_DIR = Path(__file__).resolve().parent.parent
PROMPT_DIR = SERVICE_DIR / "prompts" / "scene_prompts"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scene_extractor")


def _load_prompt(filename: str) -> str:
    with open(PROMPT_DIR / filename, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["content"].strip()


# ============================================================
# 数据库连接（带失效连接自动剔除 + 断连重试）
# ============================================================
# 该库所在的 PostgreSQL 会间歇性地在处理请求前后关闭连接
# （"server closed the connection unexpectedly"）。
#   - pool_pre_ping：取连接时先探活，自动丢弃失效连接后重建
#   - db_retry：整段事务遇到连接类错误时重跑，写入均为幂等 upsert，重跑安全

def _make_engine():
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def _make_platform_engine():
    """story_tree_json/story_tree_md 已迁平台库(agent_platform);scenes 仍在业务库"""
    from common.config import PLATFORM_DATABASE_URL
    return create_engine(PLATFORM_DATABASE_URL, pool_pre_ping=True)


# 只对连接类错误重试（业务/SQL 错误不应重试，需立即暴露）
db_retry = retry(
    retry=retry_if_exception_type((OperationalError, DBAPIError)),
    stop=stop_after_attempt(5),
    wait=wait_fixed(2),
    reraise=True,
)


# ============================================================
# JSON 解析工具
# ============================================================

def robust_json_extract(raw_text: str) -> str:
    match = re.search(r"```json\s*(.*?)\s*```", raw_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1:
        return raw_text[start:end + 1]
    return raw_text.strip()


def parse_json_loose(raw_text: str) -> dict:
    raw = robust_json_extract(raw_text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("⚠️ 标准 JSON 解析失败，改用 json_repair 兜底修复（属正常兜底，不影响结果）")
        repaired = json_repair.loads(raw)
        logger.info("✅ json_repair 兜底修复成功，数据完整")
        return repaired


def name_to_pinyin(name: str) -> str:
    py = "".join(lazy_pinyin(name or ""))
    py = "".join(ch for ch in py if ch.isalnum()).lower()
    return py or "scene"


# ============================================================
# 数据库读写
# ============================================================

@db_retry
def fetch_story_tree_from_db(task_id: str) -> dict:
    """从 story_tree_json 表读取 story_tree.json 内容。"""
    engine = _make_platform_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM story_tree_json WHERE task_id = :tid"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        logger.error(f"❌ story_tree_json 表中未找到 task_id={task_id} 的记录")
        return {}
    data = load_story_tree_for_player(
        row[0], task_id=task_id, db_engine=engine, logger=logger
    )
    if not isinstance(data.get("nodes"), dict):
        logger.error(
            f"❌ task_id={task_id} 的 story_tree_json 没有可用的语言版本"
        )
        return {}
    logger.info(f"✅ 读取 story_tree，节点数={len(data.get('nodes', {}))}")
    return data


@db_retry
def fetch_node_mds_from_db(task_id: str) -> dict:
    """从 story_tree_md 表读取所有节点的 md 内容，返回 {name: content}。"""
    engine = _make_platform_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name, data FROM story_tree_md WHERE task_id = :tid ORDER BY id"),
            {"tid": task_id}
        ).fetchall()
    result = {row[0]: row[1] for row in rows}
    logger.info(f"✅ 读取 story_tree_md，节点 md 数={len(result)}")
    return result


@db_retry
def _do_save_raw(task_id: str, payload: str) -> None:
    engine = _make_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "SELECT setval('scenes_id_seq', COALESCE((SELECT MAX(id) FROM scenes), 1), "
            "(SELECT MAX(id) FROM scenes) IS NOT NULL)"
        ))
        conn.execute(
            text("""
                INSERT INTO scenes (task_id, data, raw, create_time, update_time)
                VALUES (:tid, '{}'::jsonb, :raw, NOW(), NOW())
                ON CONFLICT (task_id) DO UPDATE SET
                    raw = EXCLUDED.raw,
                    update_time = NOW()
            """),
            {"tid": task_id, "raw": payload}
        )


def save_raw_to_scenes_table(task_id: str, raw_scenes: list) -> bool:
    """将 extract_raw 提取的原始场景列表写入 scenes 表的 raw 列。

    在归并之前运行，行可能尚不存在，因此按 task_id upsert：
    行不存在则新建（data 留空对象），已存在则仅更新 raw 列。
    遇到数据库断连会自动重试（见 db_retry）。返回是否成功。
    """
    payload = json.dumps(raw_scenes, ensure_ascii=False)
    try:
        _do_save_raw(task_id, payload)
        logger.info(f"💾 [Scenes.raw] 原始场景入库成功! 场景数={len(raw_scenes)}")
        return True
    except Exception as e:
        logger.error(f"❌ [Scenes.raw] 入库失败（已重试）: {e}")
        return False


@db_retry
def fetch_raw_from_scenes_table(task_id: str) -> list:
    """从 scenes 表的 raw 列读取 extract_raw 提取的原始场景列表。"""
    engine = _make_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT raw FROM scenes WHERE task_id = :tid"),
            {"tid": task_id}
        ).fetchone()
    if not row or row[0] is None:
        logger.error(f"❌ scenes 表中未找到 task_id={task_id} 的 raw 数据")
        return []
    data = row[0] if isinstance(row[0], list) else json.loads(row[0])
    logger.info(f"✅ 从 scenes.raw 读取原始场景，共 {len(data)} 条")
    return data


@db_retry
def _do_save_merged(task_id: str, payload: str) -> int:
    engine = _make_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "SELECT setval('scenes_id_seq', COALESCE((SELECT MAX(id) FROM scenes), 1), "
            "(SELECT MAX(id) FROM scenes) IS NOT NULL)"
        ))
        sql = text("""
            INSERT INTO scenes (task_id, data, create_time, update_time)
            VALUES (:tid, :data, NOW(), NOW())
            ON CONFLICT (task_id) DO UPDATE SET
                data = EXCLUDED.data,
                update_time = NOW()
            RETURNING id
        """)
        result = conn.execute(sql, {"tid": task_id, "data": payload})
        return result.fetchone()[0]


def save_to_scenes_table(task_id: str, merged_data: list) -> int | None:
    """将合并后的场景列表写入 scenes 表的 data 列。

    遇到数据库断连会自动重试（见 db_retry）。成功返回 DB_ID，最终失败返回 None。
    """
    payload = json.dumps(merged_data, ensure_ascii=False)
    try:
        new_id = _do_save_merged(task_id, payload)
        logger.info(f"💾 [Scenes] 合并结果入库成功! DB_ID={new_id}, 场景点数={len(merged_data)}")
        return new_id
    except Exception as e:
        logger.error(f"❌ [Scenes] 入库失败（已重试）: {e}")
        return None


# ============================================================
# 阶段一：原始场景提取（写入 scenes.raw）
# ============================================================

def ordered_node_ids(story_tree: dict) -> list:
    nodes = story_tree.get("nodes", {})
    root_id = story_tree.get("root_id")
    if not root_id or root_id not in nodes:
        return list(nodes.keys())

    ordered = []
    visited = set()

    def dfs(node_id):
        if node_id in visited or node_id not in nodes:
            return
        visited.add(node_id)
        ordered.append(node_id)
        for child in nodes[node_id].get("children", []):
            child_id = child.get("child_id")
            if child_id:
                dfs(child_id)

    dfs(root_id)
    for nid in nodes:
        if nid not in visited:
            ordered.append(nid)
    return ordered


def resolve_md_content(node_mds: dict, node_id: str, node_info: dict) -> str | None:
    """从内存中定位节点对应的 md 内容。优先用 content_file 字段中的名称，其次用 node_id。"""
    content_file = (node_info or {}).get("content_file", "")
    if content_file:
        name = Path(content_file).stem
        if name in node_mds:
            return node_mds[name]
    if node_id in node_mds:
        return node_mds[node_id]
    return None


def _number_lines(lines: list, start_line: int) -> str:
    """把行列表渲染为带绝对行号的文本，供 LLM 直接标注场景起止行。

    格式：`123│正文内容`。行号为该行在节点 md 中的绝对行号，与 refs 同坐标系。
    """
    return "\n".join(f"{start_line + off}│{ln}" for off, ln in enumerate(lines))


def chunk_text(md_text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list:
    lines = md_text.split("\n")
    chunks = []
    cur_lines = []
    cur_len = 0
    cur_start = 1

    def _emit(block_lines, start, end):
        if "\n".join(block_lines).strip():
            chunks.append({
                "text": "\n".join(block_lines).strip(),
                "numbered_text": _number_lines(block_lines, start),
                "start_line": start,
                "end_line": end,
            })

    for idx, line in enumerate(lines, start=1):
        line_len = len(line) + 1
        if cur_len + line_len > size and cur_lines:
            _emit(cur_lines, cur_start, idx - 1)
            back = []
            back_len = 0
            for prev in reversed(cur_lines):
                if back_len >= overlap:
                    break
                back.insert(0, prev)
                back_len += len(prev) + 1
            cur_lines = back
            cur_len = back_len
            cur_start = idx - len(back)

        cur_lines.append(line)
        cur_len += line_len

    if cur_lines:
        _emit(cur_lines, cur_start, len(lines))
    return chunks


@retry(stop=stop_after_attempt(5), wait=wait_random_exponential(min=1, max=60))
async def extract_scenes_from_chunk(
    client, semaphore, sys_prompt, user_template,
    node_id, node_title, node_summary, prev_scene_names, chunk_index, chunk_total, chunk,
):
    novel_text = chunk["numbered_text"]
    start_line, end_line = chunk["start_line"], chunk["end_line"]
    prev_scenes_str = "、".join(prev_scene_names) if prev_scene_names else "（暂无，这是本节点的第一段）"
    user_message = (
        user_template
        .replace("{node_title}", node_title)
        .replace("{node_summary}", node_summary)
        .replace("{prev_scenes}", prev_scenes_str)
        .replace("{chunk_index}", str(chunk_index))
        .replace("{chunk_total}", str(chunk_total))
        .replace("{novel_text}", novel_text)
    )

    async with semaphore:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.4,
        )
    if _usage_tracker and getattr(response, "usage", None):
        _usage_tracker.add_llm_usage(
            response.usage, MODEL_NAME, provider="openai_compatible"
        )
    raw = response.choices[0].message.content
    parsed = json.loads(robust_json_extract(raw))
    scenes = parsed.get("scenes", [])

    def _norm_enum(val, enum, default):
        v = (val or "").strip().lower()
        return v if v in enum else default

    def _scene_ref(s: dict) -> str:
        """取 LLM 返回的场景级 start_line/end_line，钳制到本 chunk 边界。

        LLM 看到的是带绝对行号的正文，理应返回本场景真实起止行；
        若缺失或越界（幻觉），回退到整块行号，保证 refs 始终有效。
        """
        try:
            s_line = int(s.get("start_line"))
            e_line = int(s.get("end_line"))
        except (TypeError, ValueError):
            return f"{node_id}_{start_line}_{end_line}"
        if s_line > e_line:
            s_line, e_line = e_line, s_line
        s_line = max(start_line, min(s_line, end_line))
        e_line = max(start_line, min(e_line, end_line))
        return f"{node_id}_{s_line}_{e_line}"

    cleaned = []
    for s in scenes:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        cleaned.append({
            "name": name,
            "position": (s.get("position") or "").strip(),
            "info": (s.get("info") or "").strip(),
            "shot": (s.get("shot") or "").strip(),
            "detail": (s.get("detail") or "").strip(),
            "time": _norm_enum(s.get("time"), TIME_ENUM, "morning"),
            "weather": _norm_enum(s.get("weather"), WEATHER_ENUM, "clear"),
            "light": (s.get("light") or "").strip(),
            "atmosphere": (s.get("atmosphere") or "").strip(),
            "refs": [_scene_ref(s)],
        })
    return cleaned


async def process_node(client, semaphore, sys_prompt, user_template, node_id, node_info, md_text,
                       scenes_in_order, seen_names):
    node_title = (node_info or {}).get("title", node_id)
    node_summary = (node_info or {}).get("summary", "")

    chunks = chunk_text(md_text)
    logger.info(f"📄 节点 {node_id} 《{node_title}》: {len(md_text)} 字符 -> {len(chunks)} 块")

    for idx, chunk in enumerate(chunks, start=1):
        prev_names = list(seen_names)
        try:
            scenes = await extract_scenes_from_chunk(
                client, semaphore, sys_prompt, user_template,
                node_id, node_title, node_summary, prev_names, idx, len(chunks), chunk,
            )
        except Exception as e:
            logger.error(f"  ❌ 节点 {node_id} 第 {idx}/{len(chunks)} 块提取失败: {e}")
            continue

        for sc in scenes:
            scenes_in_order.append(sc)
            seen_names.add(sc["name"])

        logger.info(f"  ✅ 第 {idx}/{len(chunks)} 块完成，全局累计场景 {len(scenes_in_order)} 条")


async def run_extract_raw(task_id: str) -> list:
    """阶段一：从数据库读取故事树和 md，提取原始场景列表并写入 scenes.raw 列。

    Returns:
        原始场景列表 scenes_in_order。
    """
    if not API_KEY:
        logger.error("❌ 未设置 GPT_IMAGE_API_KEY（或 OPENAI_API_KEY）。")
        return []

    story_tree = fetch_story_tree_from_db(task_id)
    if not story_tree:
        return []

    node_mds = fetch_node_mds_from_db(task_id)
    nodes = story_tree.get("nodes", {})
    node_ids = ordered_node_ids(story_tree)
    logger.info(f"🚀 任务: {task_id} | 《{story_tree.get('book_title', '')}》")
    logger.info(f"📚 遍历顺序: {' -> '.join(node_ids)}")

    sys_prompt = (
        _load_prompt("scene_extractor_raw_sys_zh.yaml")
        .replace("{time_enum}", " / ".join(TIME_ENUM))
        .replace("{weather_enum}", " / ".join(WEATHER_ENUM))
    )
    user_template = _load_prompt("scene_extractor_raw_user_zh.yaml")

    scenes_in_order = []
    seen_names = set()

    async with AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL) as client:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        for node_id in node_ids:
            node_info = nodes.get(node_id, {})
            md_text = resolve_md_content(node_mds, node_id, node_info)
            if not md_text:
                logger.warning(f"⚠️ 节点 {node_id} 未找到 md 内容，跳过。")
                continue
            await process_node(client, semaphore, sys_prompt, user_template, node_id, node_info, md_text,
                               scenes_in_order, seen_names)

    logger.info(f"✅ 原始场景提取完成！共 {len(scenes_in_order)} 个场景")
    if not save_raw_to_scenes_table(task_id, scenes_in_order):
        # 写库失败（重试后仍失败），返回空表示阶段失败，避免 merge 读到旧数据
        return []
    return scenes_in_order


# ============================================================
# 阶段二：场景去重归并（从 scenes.raw 读取，写入 scenes.data）
# ============================================================

def assign_scene_ids(results: list) -> None:
    used = {}
    for loc in results:
        base = name_to_pinyin(loc.get("name", ""))
        n = used.get(base, 0)
        used[base] = n + 1
        key = base if n == 0 else f"{base}{n + 1}"
        loc["scene_id"] = f"scene_{key}"
        for i, v in enumerate(loc.get("variants", []), start=1):
            v["scene_id"] = f"scene_{key}_{i}"


@retry(stop=stop_after_attempt(5), wait=wait_random_exponential(min=1, max=60))
async def cluster_scenes(client, scenes):
    sys_prompt = _load_prompt("scene_extractor_cluster_sys_zh.yaml")
    user_template = _load_prompt("scene_extractor_cluster_user_zh.yaml")

    lines = []
    for i, s in enumerate(scenes):
        lines.append(f"[{i}] name: {s['name']}\n    info: {s['info']}")
    scene_list = "\n".join(lines)
    user_message = user_template.replace("{scene_list}", scene_list)

    response = await client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.1,
    )
    if _usage_tracker and getattr(response, "usage", None):
        _usage_tracker.add_llm_usage(
            response.usage, MODEL_NAME, provider="openai_compatible"
        )
    parsed = parse_json_loose(response.choices[0].message.content)
    return parsed.get("groups", [])


def validate_and_fix_groups(groups, total):
    assigned = set()
    fixed = []
    for g in groups:
        idxs = []
        for i in g.get("indices", []):
            if isinstance(i, int) and 0 <= i < total and i not in assigned:
                assigned.add(i)
                idxs.append(i)
        if idxs:
            fixed.append({"canonical_name": g.get("canonical_name", "").strip(), "indices": sorted(idxs)})
    for i in range(total):
        if i not in assigned:
            fixed.append({"canonical_name": "", "indices": [i]})
    fixed.sort(key=lambda g: min(g["indices"]))
    return fixed


def _tw_buckets(group_scenes):
    order = []
    buckets = {}
    for i, s in enumerate(group_scenes):
        key = (s.get("time", "morning"), s.get("weather", "clear"))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(i)
    return [(k[0], k[1], buckets[k]) for k in order]


def merge_refs(scenes):
    seen = set()
    out = []
    for s in scenes:
        for r in s.get("refs", []):
            if r not in seen:
                seen.add(r)
                out.append(r)
    return out


def _variant_from_indices(scene_list, idxs, text_src):
    members = [scene_list[i] for i in idxs]
    time_v = members[0].get("time", "morning")
    weather_v = members[0].get("weather", "clear")
    member_pos = "；".join(dict.fromkeys(s["position"] for s in members if s.get("position")))
    # shot（画面）继承自成员记录：LLM 未给出时，取成员里最长的一条 shot 作代表
    member_shot = max((s.get("shot", "") for s in members), key=len, default="")
    if text_src is None:
        info = "；".join(dict.fromkeys(s["info"] for s in members if s.get("info")))
        # 兜底：无 LLM 变体时，increment 由成员 detail 聚合（此时尚未减去一级，供后续参考）
        increment = "；".join(dict.fromkeys(s["detail"] for s in members if s.get("detail")))
        light = next((s["light"] for s in members if s.get("light")), "")
        atmosphere = next((s["atmosphere"] for s in members if s.get("atmosphere")), "")
        position = member_pos
        shot = member_shot
    else:
        info = (text_src.get("info") or "").strip()
        increment = (text_src.get("increment") or "").strip()
        light = (text_src.get("light") or "").strip()
        atmosphere = (text_src.get("atmosphere") or "").strip()
        position = (text_src.get("position") or "").strip() or member_pos
        shot = (text_src.get("shot") or "").strip() or member_shot
    return {
        "position": position,
        "info": info,
        "shot": shot,
        "increment": increment,
        "time": time_v,
        "weather": weather_v,
        "light": light,
        "atmosphere": atmosphere,
        "refs": merge_refs(members),
        "_min_index": min(idxs),
    }


@retry(stop=stop_after_attempt(5), wait=wait_random_exponential(min=1, max=60))
async def merge_location(client, semaphore, canonical_name, group_scenes):
    sys_prompt = _load_prompt("scene_extractor_merge_sys_zh.yaml")
    user_template = _load_prompt("scene_extractor_merge_user_zh.yaml")

    rec_lines = []
    for i, s in enumerate(group_scenes):
        rec_lines.append(
            f"[{i}] time={s.get('time','')} weather={s.get('weather','')} position={s.get('position','')}\n"
            f"    info: {s.get('info','')}\n"
            f"    shot: {s.get('shot','')}\n"
            f"    detail: {s.get('detail','')}\n"
            f"    light: {s.get('light','')}  atmosphere: {s.get('atmosphere','')}"
        )
    records_text = "\n".join(rec_lines)

    buckets = _tw_buckets(group_scenes)
    buckets_text = "\n".join(f"time={t} weather={w}: {idxs}" for t, w, idxs in buckets)

    user_message = (
        user_template
        .replace("{canonical_name}", canonical_name)
        .replace("{records}", records_text)
        .replace("{buckets}", buckets_text)
    )

    async with semaphore:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.2,
        )
    if _usage_tracker and getattr(response, "usage", None):
        _usage_tracker.add_llm_usage(
            response.usage, MODEL_NAME, provider="openai_compatible"
        )
    parsed = parse_json_loose(response.choices[0].message.content)

    total = len(group_scenes)
    assigned = set()
    variants = []
    for lv in parsed.get("variants", []):
        idxs = [i for i in lv.get("members", [])
                if isinstance(i, int) and 0 <= i < total and i not in assigned]
        if not idxs:
            continue
        sub_order, sub = [], {}
        for i in idxs:
            s = group_scenes[i]
            k = (s.get("time", "morning"), s.get("weather", "clear"))
            if k not in sub:
                sub[k] = []
                sub_order.append(k)
            sub[k].append(i)
            assigned.add(i)
        for k in sub_order:
            variants.append(_variant_from_indices(group_scenes, sub[k], lv))

    leftover = [i for i in range(total) if i not in assigned]
    if leftover:
        lo_order, lo = [], {}
        for i in leftover:
            s = group_scenes[i]
            k = (s.get("time", "morning"), s.get("weather", "clear"))
            if k not in lo:
                lo[k] = []
                lo_order.append(k)
            lo[k].append(i)
        for k in lo_order:
            variants.append(_variant_from_indices(group_scenes, lo[k], None))

    variants.sort(key=lambda v: v["_min_index"])
    for v in variants:
        v.pop("_min_index", None)

    return {
        "name": (parsed.get("name") or canonical_name).strip(),
        "info": (parsed.get("info") or "").strip(),
        "detail": (parsed.get("detail") or "").strip(),
        "variants": variants,
    }


def fallback_merge(canonical_name, group_scenes):
    variants = []
    for _t, _w, idxs in _tw_buckets(group_scenes):
        variants.append(_variant_from_indices(group_scenes, idxs, None))
    variants.sort(key=lambda v: v["_min_index"])
    for v in variants:
        v.pop("_min_index", None)
    return {
        "name": canonical_name,
        "info": "；".join(dict.fromkeys(s["info"] for s in group_scenes if s.get("info"))),
        "detail": "；".join(dict.fromkeys(s["detail"] for s in group_scenes if s.get("detail"))),
        "variants": variants,
    }


async def run_merge(task_id: str, keep_aliases: bool = True) -> list:
    """阶段二：从 scenes.raw 列读取原始场景，归并后写入 scenes.data 列。

    Args:
        task_id: 任务 ID，用于读写 scenes 表。
        keep_aliases: 是否保留 other_names 字段。

    Returns:
        合并后的 locations 列表（已写入数据库）。
    """
    if not API_KEY:
        logger.error("❌ 未设置 GPT_IMAGE_API_KEY（或 OPENAI_API_KEY）。")
        return []

    scenes = fetch_raw_from_scenes_table(task_id)
    if not scenes:
        logger.error("❌ 没有可归并的原始场景数据（scenes.raw 为空）")
        return []

    logger.info(f"📥 接收 {len(scenes)} 条原始场景")

    async with AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL) as client:
        logger.info("--- 阶段一：语义聚类 ---")
        raw_groups = await cluster_scenes(client, scenes)
        groups = validate_and_fix_groups(raw_groups, len(scenes))
        logger.info(f"🧩 聚为 {len(groups)} 组（{len(scenes)} 条 -> {len(groups)} 个场景）")

        logger.info("--- 阶段二：逐场景点两级归并 ---")
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async def _merge_one(group):
            group_scenes = [scenes[i] for i in group["indices"]]
            canonical = group["canonical_name"] or group_scenes[0]["name"]
            try:
                merged = await merge_location(client, semaphore, canonical, group_scenes)
            except Exception as e:
                logger.error(f"  ❌ 场景点「{canonical}」LLM 合并失败，改用兜底拼装: {e}")
                merged = fallback_merge(canonical, group_scenes)
            if keep_aliases:
                merged["other_names"] = sorted({scenes[i]["name"] for i in group["indices"]})
            merged["_min_index"] = min(group["indices"])
            return merged

        results = await asyncio.gather(*[_merge_one(g) for g in groups])
        results.sort(key=lambda r: r.pop("_min_index"))
        assign_scene_ids(results)

    total_variants = sum(len(r.get("variants", [])) for r in results)
    logger.info(f"✅ 合并完成，完整提取成功！{len(scenes)} 条 -> {len(results)} 个场景点 / {total_variants} 个变体")

    if save_to_scenes_table(task_id, results) is None:
        # 写库失败（重试后仍失败），返回空表示归并阶段失败，
        # 避免归并结果丢失后 step7 仍继续跑 scene_to_plot / scene_prompts
        logger.error("❌ 归并结果写库失败，返回空以中断后续步骤")
        return []
    return results


# ============================================================
# 完整链路
# ============================================================

async def run_all(task_id: str, keep_aliases: bool = True) -> list:
    """完整链路：raw 提取 -> 写库 -> 从库读取 -> 归并 -> 写库。两阶段仅通过 scenes.raw 交接。"""
    raw_scenes = await run_extract_raw(task_id)
    if not raw_scenes:
        logger.error("❌ 原始场景提取为空，终止归并")
        return []
    return await run_merge(task_id, keep_aliases=keep_aliases)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="场景提取器（raw 提取 + merge 归并，数据库解耦）")
    parser.add_argument("--task-id", type=str, required=True, help="任务 task_id")
    parser.add_argument("--stage", choices=["raw", "merge", "all"], default="all",
                        help="执行阶段: raw=仅提取原始场景写入 raw 列; "
                             "merge=仅从 raw 列读取归并写入 data 列; all=完整链路 (默认)")
    parser.add_argument("--no-aliases", action="store_true", help="归并时不保留 other_names 字段")
    args = parser.parse_args()

    if args.stage == "raw":
        asyncio.run(run_extract_raw(args.task_id))
    elif args.stage == "merge":
        asyncio.run(run_merge(args.task_id, keep_aliases=not args.no_aliases))
    else:
        asyncio.run(run_all(args.task_id, keep_aliases=not args.no_aliases))
