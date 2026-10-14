"""
剧情表场景对齐器 - 从数据库读取 plots 和 scenes，回填 scene_id 后写回数据库。

该模块为流水线第三步：
  - 从 plots 表读取 {node_id: [beats...]}
  - 从 scenes 表读取合并后的场景列表（两级结构）
  - 让 LLM 为每个 beat 分配最合适的 scene_id（含兜底新建变体）
  - 将回填结果写回 plots 表
  - 将含新建变体的场景列表更新写回 scenes 表

用法：
    python core/scene_to_plot.py --task-id xxx
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

try:
    from info_extractor.utils.story_tree_json_parse import load_story_tree_for_player
except ImportError:
    from utils.story_tree_json_parse import load_story_tree_for_player  # type: ignore
from sqlalchemy.exc import OperationalError, DBAPIError

from collections.abc import Mapping
from typing import Any

try:
    from info_extractor.utils.scene_id_restore import (
        collect_variant_scene_ids,
        resolve_base_scene_id,
    )
except ImportError:
    from utils.scene_id_restore import (  # type: ignore
        collect_variant_scene_ids,
        resolve_base_scene_id,
    )

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

BEAT_CONTENT_CAP = 80

TIME_ENUM = ["dawn", "morning", "noon", "afternoon", "dusk", "night"]
WEATHER_ENUM = ["clear", "cloudy", "rain", "snow", "fog", "storm"]

SERVICE_DIR = Path(__file__).resolve().parent.parent
PROMPT_DIR = SERVICE_DIR / "prompts" / "scene_prompts"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scene_to_plot")


# ============================================================
# 场景连续区域编号
# ============================================================
# 把所有剧情节点按故事树顺序线性化，再按 beat 顺序识别同一场景的连续区域。
# 节点边界始终视为断点：上一章最后一个 beat 和下一章第一个 beat 即使使用
# 同一场景，也会分配不同的实例后缀。


def ordered_plot_node_ids(plots: Mapping[str, Any], story_tree: dict | None) -> list[str]:
    """返回确定性的剧情节点顺序。

    优先按故事树从根节点做前序深度优先遍历，子节点沿 ``children`` 数组顺序；
    未连接到故事树、但存在于 plots 中的节点，最后按 plots 原始插入顺序补齐。
    """
    if not isinstance(story_tree, dict):
        return list(plots.keys())

    nodes = story_tree.get("nodes")
    root_id = story_tree.get("root_id")
    if not isinstance(nodes, dict) or not root_id or root_id not in nodes:
        return list(plots.keys())

    ordered: list[str] = []
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited or node_id not in nodes:
            return
        visited.add(node_id)
        if node_id in plots:
            ordered.append(node_id)

        node = nodes.get(node_id) or {}
        for child in node.get("children", []):
            if not isinstance(child, dict):
                continue
            child_id = child.get("child_id")
            if child_id:
                visit(child_id)

    visit(root_id)

    for node_id in plots:
        if node_id not in visited:
            ordered.append(node_id)

    return ordered


def normalize_plot_scene_ids(plots: dict, locations: list[dict]) -> int:
    """将二级场景实例 ID 还原为二级原图 ID，保证重复执行幂等。"""
    known_scene_ids = collect_variant_scene_ids(locations)
    changed = 0
    for beats in plots.values():
        if not isinstance(beats, list):
            continue
        for beat in beats:
            base_scene_id = resolve_base_scene_id(beat.get("scene_id"), known_scene_ids)
            if base_scene_id and beat.get("scene_id") != base_scene_id:
                beat["scene_id"] = base_scene_id
                changed += 1
    return changed


def assign_scene_regions(
    plots: dict,
    locations: list[dict],
    node_ids: list[str],
) -> dict[str, int]:
    """为连续场景区域分配实例后缀，并把复制次数写回 scenes 结构。

    例如原图 ``scene_a_7`` 在剧情中出现三个连续区域，则 beats 会分别指向
    ``scene_a_7_1``、``scene_a_7_2``、``scene_a_7_3``，原图 ID 保留在 scenes
    中，其 ``copy_count`` 为 3。
    """
    known_scene_ids = collect_variant_scene_ids(locations)
    region_counts: dict[str, int] = {}

    for node_id in node_ids:
        beats = plots.get(node_id)
        if not isinstance(beats, list):
            continue

        # 每个节点代表一个章节；跨节点无条件断开连续区域。
        previous_scene_id: str | None = None
        current_instance_id: str | None = None

        for beat in beats:
            base_scene_id = resolve_base_scene_id(beat.get("scene_id"), known_scene_ids)
            if not base_scene_id:
                previous_scene_id = None
                current_instance_id = None
                continue

            if base_scene_id != previous_scene_id:
                next_region = region_counts.get(base_scene_id, 0) + 1
                region_counts[base_scene_id] = next_region
                current_instance_id = f"{base_scene_id}_{next_region}"

            beat["scene_id"] = current_instance_id
            previous_scene_id = base_scene_id

    # 一级底图只供二级场景图生图参考，永远不复制；copy_count 仅写在二级场景上。
    for location in locations:
        location["copy_count"] = 0
        for variant in location.get("variants", []):
            variant["copy_count"] = region_counts.get(variant.get("scene_id"), 0)

    return region_counts


def _load_prompt(filename: str) -> str:
    with open(PROMPT_DIR / filename, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["content"].strip()


# ============================================================
# 数据库连接（带失效连接自动剔除 + 断连重试）
# ============================================================
# 该库所在的 PostgreSQL 会间歇性地在处理请求前后关闭连接
# （"server closed the connection unexpectedly"）。
#   - pool_pre_ping：取连接时先探活，自动丢弃失效连接后重建
#   - db_retry：整段事务遇到连接类错误时重跑，读写均为幂等，重跑安全

def _make_engine():
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def _make_platform_engine():
    """plots 表已迁平台库(agent_platform);scenes 仍在业务库"""
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
# 数据库读写
# ============================================================

@db_retry
def fetch_plots_from_db(task_id: str) -> dict:
    """从 plots 表读取 {node_id: [beats...]}。"""
    engine = _make_platform_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM plots WHERE task_id = :tid"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        logger.error(f"❌ plots 表中未找到 task_id={task_id}")
        return {}
    data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    logger.info(f"✅ 读取 plots，节点数={len(data)}")
    return data


@db_retry
def fetch_scenes_from_db(task_id: str) -> list:
    """从 scenes 表读取合并后的场景列表。"""
    engine = _make_engine()
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
        # 兼容旧格式：{scene_id: {image_prompt, source_node}}
        logger.warning("⚠️ scenes 表中数据为旧格式（dict），期望 list")
        return []
    logger.info(f"✅ 读取 scenes，场景点数={len(data)}")
    return data


@db_retry
def fetch_story_tree_from_db(task_id: str) -> dict:
    """读取故事树，用于确定所有剧情节点的稳定先后顺序。"""
    engine = _make_platform_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM story_tree_json WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id},
        ).fetchone()
    if not row:
        logger.warning(f"⚠️ story_tree_json 中未找到 task_id={task_id}，将使用 plots 原顺序")
        return {}
    story_tree = load_story_tree_for_player(
        row[0], task_id=task_id, db_engine=engine, logger=logger
    )
    if not isinstance(story_tree.get("nodes"), dict):
        logger.warning(
            f"⚠️ task_id={task_id} 的 story_tree_json 没有可用语言版本，"
            "将使用 plots 原顺序"
        )
        return {}
    return story_tree


@db_retry
def save_plots_to_db(task_id: str, plots: dict) -> None:
    """将回填了 scene_id 的 plots 写回数据库。"""
    engine = _make_platform_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE plots SET data = :data, update_time = NOW() WHERE task_id = :tid"),
            {"data": json.dumps(plots, ensure_ascii=False), "tid": task_id}
        )
    logger.info(f"💾 [Plots] 回填完成，已写回数据库")


@db_retry
def save_scenes_to_db(task_id: str, locations: list) -> None:
    """将含新建变体的场景列表更新写回 scenes 表。"""
    engine = _make_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE scenes SET data = :data, update_time = NOW() WHERE task_id = :tid"),
            {"data": json.dumps(locations, ensure_ascii=False), "tid": task_id}
        )
    logger.info(f"💾 [Scenes] 含新建变体，已写回数据库")


# ============================================================
# 场景索引
# ============================================================

def name_to_pinyin(name: str) -> str:
    py = "".join(lazy_pinyin(name or ""))
    py = "".join(ch for ch in py if ch.isalnum()).lower()
    return py or "scene"


def parse_ref(ref: str):
    parts = ref.rsplit("_", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return parts[0], int(parts[1]), int(parts[2])
    return ref, None, None


def robust_json_extract(text: str) -> str:
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start:end + 1]
    return text.strip()


def parse_json_loose(text: str) -> dict:
    raw = robust_json_extract(text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("⚠️ 标准 JSON 解析失败，改用 json_repair 兜底修复")
        return json_repair.loads(raw)


class SceneIndex:
    def __init__(self, locations: list):
        self.locations = locations
        self.by_scene_id = {loc["scene_id"]: loc for loc in locations if loc.get("scene_id")}
        self.by_name = {}
        for loc in locations:
            for nm in [loc.get("name")] + list(loc.get("other_names", [])):
                if nm:
                    self.by_name.setdefault(nm, loc)

    def candidates_for_node(self, node_id: str) -> list:
        cands = []
        for loc in self.locations:
            for v in loc.get("variants", []):
                node_ranges = []
                for r in v.get("refs", []):
                    nid, s, e = parse_ref(r)
                    if nid == node_id and s is not None:
                        node_ranges.append((s, e))
                if not node_ranges:
                    continue
                node_ranges.sort()
                cands.append({
                    "scene_id": v["scene_id"],
                    "location_name": loc.get("name", ""),
                    "location_scene_id": loc.get("scene_id", ""),
                    "location_detail": loc.get("detail", ""),
                    "position": v.get("position", ""),
                    "time": v.get("time", ""),
                    "weather": v.get("weather", ""),
                    "light": v.get("light", ""),
                    "atmosphere": v.get("atmosphere", ""),
                    "info": v.get("info", ""),
                    "shot": v.get("shot", ""),
                    "increment": v.get("increment", v.get("detail", "")),
                    "line_ranges": node_ranges,
                })
        return cands

    def _next_variant_id(self, loc: dict) -> str:
        base = loc["scene_id"]
        prefix = base + "_"
        max_n = 0
        for v in loc.get("variants", []):
            sid = v.get("scene_id", "")
            if sid.startswith(prefix):
                tail = sid[len(prefix):]
                if tail.isdigit():
                    max_n = max(max_n, int(tail))
        return f"{prefix}{max_n + 1}"

    def _fresh_location_id(self, name: str) -> str:
        base = "scene_" + name_to_pinyin(name)
        sid = base
        n = 1
        while sid in self.by_scene_id:
            n += 1
            sid = f"{base}{n}"
        return sid

    def new_variant(self, spec: dict, node_id: str, line_span, log_prefix: str = "") -> str:
        loc_sid = (spec.get("location_scene_id") or "").strip()
        loc_name = (spec.get("location") or "").strip()

        loc = None
        if loc_sid and loc_sid in self.by_scene_id:
            loc = self.by_scene_id[loc_sid]
        elif loc_name and loc_name in self.by_name:
            loc = self.by_name[loc_name]

        if loc is None:
            new_loc_id = self._fresh_location_id(loc_name or "scene")
            loc = {
                "name": loc_name or new_loc_id,
                "info": spec.get("info", ""),
                "detail": spec.get("detail", ""),
                "variants": [],
                "other_names": [loc_name] if loc_name else [],
                "scene_id": new_loc_id,
            }
            self.locations.append(loc)
            self.by_scene_id[new_loc_id] = loc
            if loc_name:
                self.by_name.setdefault(loc_name, loc)
            logger.info(f"{log_prefix}🆕 新建场景点「{loc['name']}」({new_loc_id})")

        vid = self._next_variant_id(loc)
        refs = []
        if line_span and line_span[0] is not None:
            refs = [f"{node_id}_{line_span[0]}_{line_span[1]}"]

        def _norm(val, enum, default):
            v = (val or "").strip().lower()
            return v if v in enum else default

        variant = {
            "position": (spec.get("position") or "").strip(),
            "info": (spec.get("info") or "").strip(),
            "shot": (spec.get("shot") or "").strip(),
            "increment": (spec.get("increment") or spec.get("detail") or "").strip(),
            "time": _norm(spec.get("time"), TIME_ENUM, "night"),
            "weather": _norm(spec.get("weather"), WEATHER_ENUM, "clear"),
            "light": (spec.get("light") or "").strip(),
            "atmosphere": (spec.get("atmosphere") or "").strip(),
            "refs": refs,
            "scene_id": vid,
            "_created_by": "align",
        }
        loc.setdefault("variants", []).append(variant)
        self.by_scene_id[vid] = loc
        logger.info(f"{log_prefix}🆕 新建变体 {vid}（{variant['time']}/{variant['weather']} @ {loc['name']}）")
        return vid


# ============================================================
# beat 渲染 & 分段应用
# ============================================================

def render_candidates(cands: list) -> str:
    if not cands:
        return "（本节点在场景表中暂无候选变体，请一律走兜底新建）"
    lines = []
    for c in cands:
        ranges = "，".join(f"{s}-{e}" for s, e in c["line_ranges"])
        lines.append(
            f"- scene_id={c['scene_id']}  场景点=「{c['location_name']}」({c['location_scene_id']})\n"
            f"    原文行区间: {ranges}  | time={c['time']} weather={c['weather']} position={c['position']} atmosphere={c['atmosphere']}\n"
            f"    通用detail: {c['location_detail']}\n"
            f"    变体info: {c['info']}\n"
            f"    变体画面shot: {c['shot']}\n"
            f"    变体增量increment: {c['increment']}"
        )
    return "\n".join(lines)


def render_beats(beats: list) -> str:
    lines = []
    for i, b in enumerate(beats):
        sl = b.get("_src_lines") or [None, None]
        lr = f"L{sl[0]}-{sl[1]}" if sl[0] is not None else "L?"
        content = (b.get("content") or "").replace("\n", " ").strip()
        if len(content) > BEAT_CONTENT_CAP:
            content = content[:BEAT_CONTENT_CAP] + "…"
        who = b.get("speaker_name") or b.get("full_name") or ""
        lines.append(f"#{i} [{b.get('beat_id','')}] {lr} [{b.get('beat_type','')}]{('/' + who) if who else ''}: {content}")
    return "\n".join(lines)


def span_lines(beats: list, frm: int, to: int):
    starts, ends = [], []
    for b in beats[frm:to + 1]:
        sl = b.get("_src_lines")
        if sl and sl[0] is not None:
            starts.append(sl[0])
            ends.append(sl[1])
    if not starts:
        return (None, None)
    return (min(starts), max(ends))


@retry(stop=stop_after_attempt(5), wait=wait_random_exponential(min=1, max=60))
async def align_node(client, semaphore, sys_prompt, user_template, node_id, beats, cands):
    user_message = (
        user_template
        .replace("{node_id}", node_id)
        .replace("{candidates}", render_candidates(cands))
        .replace("{beats}", render_beats(beats))
        .replace("{beat_count}", str(len(beats)))
        .replace("{last_index}", str(len(beats) - 1))
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
    return parsed.get("segments", [])


def apply_segments(beats, segments, node_id, index: SceneIndex, log_prefix=""):
    n = len(beats)
    assigned = [None] * n

    norm = []
    for seg in segments:
        try:
            frm = max(0, int(seg.get("from")))
            to = min(n - 1, int(seg.get("to")))
        except (TypeError, ValueError):
            continue
        if frm > to:
            continue
        norm.append((frm, to, seg))
    norm.sort(key=lambda x: (x[0], x[1]))

    created = 0
    for frm, to, seg in norm:
        sid = seg.get("scene_id")
        if not sid:
            nv = seg.get("new_variant") or {}
            sid = index.new_variant(nv, node_id, span_lines(beats, frm, to), log_prefix)
            created += 1
        for i in range(frm, to + 1):
            if assigned[i] is None:
                assigned[i] = sid

    last = None
    for i in range(n):
        if assigned[i] is None:
            assigned[i] = last
        else:
            last = assigned[i]
    nxt = None
    for i in range(n - 1, -1, -1):
        if assigned[i] is None:
            assigned[i] = nxt
        else:
            nxt = assigned[i]

    filled = 0
    for i, b in enumerate(beats):
        if assigned[i]:
            b["scene_id"] = assigned[i]
            filled += 1
    return filled, created


# ============================================================
# 变体使用情况标记
# ============================================================

def mark_active_variants(plots: dict, node_ids: list, locations: list) -> None:
    """根据 plots 回填的 scene_id，为每个二级场景变体标记 is_active。

    beat 的 scene_id 指向变体的 scene_id。凡被任一 beat 引用的变体，
    is_active=True，否则 False。最后在终端打印使用情况。
    """
    used_ids = set()
    for nid in node_ids:
        for b in plots[nid]:
            sid = b.get("scene_id")
            if sid:
                used_ids.add(sid)

    total_variants = 0
    active_variants = 0
    unused = []
    for loc in locations:
        for v in loc.get("variants", []):
            total_variants += 1
            is_active = v.get("scene_id") in used_ids
            v["is_active"] = is_active
            if is_active:
                active_variants += 1
            else:
                unused.append((loc.get("name", ""), v.get("scene_id", "")))

    logger.info("=" * 50)
    logger.info(f"📊 二级场景变体使用情况：共 {total_variants} 个，实际使用 {active_variants} 个，未使用 {len(unused)} 个")
    if unused:
        logger.info("⚠️ 以下变体未被任何 plot 使用：")
        for loc_name, sid in unused:
            logger.info(f"    - {sid}（场景点「{loc_name}」）")
    else:
        logger.info("✅ 所有二级场景变体均已被使用")
    logger.info("=" * 50)


# ============================================================
# 主流程
# ============================================================

async def main(task_id: str) -> None:
    if not API_KEY:
        logger.error("❌ 未设置 GPT_IMAGE_API_KEY（或 OPENAI_API_KEY）。")
        return

    plots = fetch_plots_from_db(task_id)
    if not plots:
        return

    locations = fetch_scenes_from_db(task_id)
    if not locations:
        return

    index = SceneIndex(locations)
    logger.info(f"📥 plots 节点数={len(plots)}，scenes 场景点数={len(locations)}")

    sys_prompt = _load_prompt("scene_to_plot_sys_zh.yaml")
    user_template = _load_prompt("scene_to_plot_user_zh.yaml")

    node_ids = [nid for nid, beats in plots.items() if isinstance(beats, list) and beats]

    async with AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL) as client:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async def _seg_one(node_id):
            beats = plots[node_id]
            cands = index.candidates_for_node(node_id)
            try:
                segments = await align_node(client, semaphore, sys_prompt, user_template, node_id, beats, cands)
            except Exception as e:
                logger.error(f"  ❌ node {node_id} 分段失败: {e}")
                segments = []
            return node_id, segments

        seg_results = await asyncio.gather(*[_seg_one(nid) for nid in node_ids])

    total_filled = 0
    total_created = 0
    for node_id, segments in seg_results:
        beats = plots[node_id]
        if not segments:
            logger.warning(f"  ⚠️ node {node_id} 无有效分段，{len(beats)} 个 beat 未回填 scene_id")
            continue
        filled, created = apply_segments(beats, segments, node_id, index, log_prefix=f"  [{node_id}] ")
        total_filled += filled
        total_created += created
        logger.info(f"  ✅ node {node_id}: {len(segments)} 段，回填 {filled}/{len(beats)} 个 beat，新建变体 {created} 个")

    total_beats = sum(len(plots[nid]) for nid in node_ids)
    logger.info(
        f"✅ 对齐完成！回填 {total_filled}/{total_beats} 个 beat 的 scene_id，"
        f"兜底新建 {total_created} 个变体。"
    )

    # 重复执行时，先把上一次产生的 scene_x_7_1 还原到原图 scene_x_7。
    normalized = normalize_plot_scene_ids(plots, locations)
    if normalized:
        logger.info(f"🔁 已还原 {normalized} 个旧场景实例 ID，准备重新划分连续区域")

    mark_active_variants(plots, node_ids, locations)

    story_tree = fetch_story_tree_from_db(task_id)
    ordered_node_ids = ordered_plot_node_ids(plots, story_tree)
    region_counts = assign_scene_regions(plots, locations, ordered_node_ids)
    total_regions = sum(region_counts.values())
    logger.info(
        f"✅ 场景连续区域识别完成：{len(region_counts)} 张原图，共 {total_regions} 个区域；"
        "节点边界强制断开，实例后缀按故事树 DFS 顺序全局递增"
    )
    for scene_id, count in region_counts.items():
        logger.info(f"    - {scene_id}: copy_count={count}")

    save_plots_to_db(task_id, plots)
    save_scenes_to_db(task_id, locations)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="剧情表场景对齐器（读写数据库）")
    parser.add_argument("--task-id", type=str, required=True, help="任务 task_id")
    args = parser.parse_args()
    asyncio.run(main(args.task_id))
