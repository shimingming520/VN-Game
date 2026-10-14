#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_character_position.py  ——  视觉小说多角色站位提取（整合版）

从 plots / characters 表读取数据，串联三阶段：
  阶段0 规则层：按 scene 边界 + 发言切换 + 旁白断点切分"同屏段"(segment)
  阶段A LLM   ：scene 级阵营前置扫描（在场判定 + P1/P2/P3 编组 + 变更事件）
  阶段B LLM   ：段级落位，生成 display_windows（三档 Left/Center/Right + ≤3 动态轮换）
  阶段C 规则层：按 segment 白名单剔除 scene 级状态泄漏和无立绘角色，再按每个
               window 自身 beat 范围内的实际发言者剔除本窗口未开口的角色，
               3人窗口降为2人，不足2人的窗口整体丢弃（回落为下游默认单人站场）

产出一份最终 JSON 写入 character_position 表（task_id 唯一，jsonb data）。
入库失败降级保存到 storage/outputs/{task_id}/character_position.json。

最终 JSON 结构：
{
  "<node>": {
    "segments": [
      {"seg_id","scene_id","beat_ids",
       "display_windows":[{"window_id","beat_ids","onstage","center","positions","trigger"}]}
    ]
  }
}
"""
import os
import sys

# 与 extract_plots_to_db.py 一致的路径注入
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
utils_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "utils")
for _p in (project_root, os.path.normpath(utils_dir)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import os
import json
import re
import asyncio
import traceback
from collections import OrderedDict, defaultdict

from sqlalchemy import create_engine
from sqlalchemy import text as sa_text
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_random_exponential
from logger_config import setup_logger

logger = setup_logger("Step_CharPosition", "step_char_position.log")

from common.config import DATABASE_URL, OUTPUTS_PATH
from common.artifact_io import load_local_json_artifact
from common.db_pool import worker_pool_kwargs
from common.llm_fallback import ModelFallback
from common.model_json import parse_model_json

try:
    from common.usage_tracker import tracker as _usage_tracker
    from common.usage_tracker import init_usage_table
    init_usage_table()
except Exception:
    _usage_tracker = None

# ================= 配置（对齐主 pipeline） =================
DB_URL = DATABASE_URL
global_engine = create_engine(
    DB_URL,
    pool_pre_ping=True,
    **worker_pool_kwargs(default_pool_size=10, default_max_overflow=20),
)

# plots/characters 已迁平台库(agent_platform); character_position 仍在业务库
from common.config import PLATFORM_DATABASE_URL
platform_engine = create_engine(
    PLATFORM_DATABASE_URL,
    pool_pre_ping=True,
    **worker_pool_kwargs(default_pool_size=10, default_max_overflow=20),
)

API_KEY = os.getenv("DASHSCOPE_API_KEY")
# BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# BASE_URL = "https://ai.farmmx.com/v1"
BASE_URL = os.getenv("PLOT_BASE_URL","http://sefutech-api.kube-test.svc:3000/v1")

MODEL_NAME = "qwen3.7-max"
# 主模型偶发不稳定时自动改用备选模型重发同一请求（同中转站、同调用参数）。
_model_fallback = ModelFallback(MODEL_NAME, name="CharPosition", logger=logger)

MAX_CONCURRENT_REQUESTS = 5
_API_MAX_RETRIES = 4

# 客户端不在导入时创建：AsyncOpenAI 会绑定创建时的事件循环。
# 在 Celery worker 中每个任务用独立的 asyncio.run() 新建循环，
# 复用绑在旧（已关闭）循环上的 client 会触发 "Event loop is closed"。
# 因此改为每次 main() 内重建并在结束时关闭。
_client = None

# ================= 常量 =================
DIALOGUE_TYPES = {"dialogue"}
NEUTRAL_TYPES = {"cg"}            # 整图演出：占一屏但无说话者
SYSTEM_TYPES = {"variable_update"}  # 纯系统 beat：完全排除出段体系

MIN_DIALOGUE_RUN = 3             # 连续 dialogue 达到此数才算"连续对话"
MAX_ONSTAGE = 3                  # 同屏硬上限

# 按同屏人数确定合法档位方案：
#   1 人 -> Center
#   2 人 -> Left_Center + Right_Center（两人居中对称，无单一 Center）
#   3 人 -> Left + Center + Right
SLOTS_BY_COUNT = {
    1: {"Center"},
    2: {"Left_Center", "Right_Center"},
    3: {"Left", "Center", "Right"},
}


# ============================================================
# 数据读取
# ============================================================
def fetch_task_context(target_task_id=None):
    """从 plots / characters 表读取站位所需上下文，读不到时降级到本地产物。
    返回 (task_id, plots_data, characters_data)。
    """
    task_id = target_task_id
    plots_data = None
    characters_data = {}
    try:
        with platform_engine.connect() as conn:
            if not task_id:
                logger.info("🔍 未指定 task_id，查找最新 plots 任务...")
                res = conn.execute(sa_text(
                    "SELECT task_id FROM plots ORDER BY id DESC LIMIT 1")).fetchone()
                if not res:
                    logger.error("❌ plots 表为空")
                    return None, None, None
                task_id = res[0]

            logger.info(f"🚀 锁定任务 Task ID: {task_id}")

            row_plots = conn.execute(
                sa_text("SELECT data FROM plots WHERE task_id = :tid"),
                {"tid": task_id}).fetchone()
            plots_data = row_plots[0] if row_plots else None

            row_char = conn.execute(
                sa_text("SELECT data FROM characters WHERE task_id = :tid"),
                {"tid": task_id}).fetchone()
            characters_data = row_char[0] if row_char else {}
    except Exception as e:
        logger.warning(f"⚠️ 读取数据库失败，将尝试本地降级产物: {e}")

    if not task_id:
        logger.error("❌ 无法确定 task_id，且数据库不可用")
        return None, None, None

    # 上一步入库失败时 plots 只存在于本地降级文件里，这里补读，避免站位整步空转。
    if not plots_data:
        plots_data = load_local_json_artifact(task_id, "plots.json", logger=logger)
    if not plots_data:
        logger.error(f"❌ 数据库和本地产物均未找到 plots 数据 (TaskID: {task_id})")
        return task_id, None, characters_data
    logger.info(f"📦 加载剧情: {len(plots_data)} 个节点")

    if characters_data:
        logger.info(f"✅ 加载角色库: {len(characters_data)} 个角色")
    else:
        logger.warning("⚠️ 未找到角色数据，主角/NPC 判定将退化")

    return task_id, plots_data, characters_data


# ============================================================
# 阶段 0：规则层段切分
# ============================================================
def build_rank_table(chars):
    """中文名 -> {rank, is_protagonist, other_names}。rank 前2档(distinct)= 主角团。"""
    ranks = sorted({v.get("important_rank", 0) for v in chars.values()}, reverse=True)
    top_tiers = set(ranks[:2])
    table = {}
    for name, v in chars.items():
        r = v.get("important_rank", 0)
        table[name] = {"rank": r, "is_protagonist": r in top_tiers,
                       "other_names": v.get("other_names", [])}
    return table


def build_drawable_names(chars):
    """可出图角色名集合（含别名）。

    只有 characters 表里记录的角色才会生成立绘，未记录的 NPC 若进入多人站位
    会导致一侧空缺。空集表示无角色数据，此时不做过滤（退化为旧行为）。
    """
    names = set()
    for name, v in (chars or {}).items():
        names.add(name)
        names.update(a for a in (v.get("other_names") or []) if a)
    return names


def segment_chapter(beats, drawable=None):
    """只切出【合格的多角色对话段】，其余 beat 全部丢弃（单人/旁白不输出）。

    合格条件（全部满足）：
      - 同一 scene 内
      - 连续 dialogue，中间不夹任何非 dialogue（含 cg/旁白/系统 beat 的位置都算中断）
      - 连续长度 >= MIN_DIALOGUE_RUN(5)
      - 不同 speaker >= 2（其中"有立绘的" speaker >= 2，否则同屏必然缺图）

    单人站场是下游默认状态，不需要本产物处理，故一律不切出。
    """
    segments = []
    run = []          # 当前连续 dialogue 累积
    run_scene = None

    def flush_run():
        nonlocal run, run_scene
        if len(run) >= MIN_DIALOGUE_RUN:
            speakers = {b.get("full_name") for b in run if b.get("full_name")}
            if drawable:
                speakers = {s for s in speakers if s in drawable}
            if len(speakers) >= 2:
                segments.append({
                    "scene_id": run_scene,
                    "beat_ids": [b.get("beat_id") for b in run],
                    "_speaker_seq": [b.get("full_name") for b in run],
                })
        run = []
        run_scene = None

    for b in beats:
        bt = b.get("beat_type")
        scene = b.get("scene_id")
        is_dlg = bt in DIALOGUE_TYPES

        # 任何非 dialogue，或 scene 切换，都中断连续 dialogue
        if not is_dlg or scene != run_scene:
            flush_run()
            if is_dlg:
                run = [b]
                run_scene = scene
        else:
            run.append(b)

    flush_run()
    return segments


def build_segment_record(seg, idx, rank_table):
    """整理多角色段记录 + 给 LLM 的 hints。（段必含 >=2 speaker）"""
    scene_id = seg["scene_id"]
    seq = [s for s in seg["_speaker_seq"] if s]
    entry_order = list(OrderedDict((s, None) for s in seq).keys())

    char_meta = {}
    for name in entry_order:
        meta = rank_table.get(name)
        if meta is None:
            char_meta[name] = {"rank": None, "is_protagonist": False, "is_npc": True}
        else:
            char_meta[name] = {"rank": meta["rank"],
                               "is_protagonist": meta["is_protagonist"], "is_npc": False}

    return {
        "seg_id": f"seg_{scene_id}_{idx:03d}" if scene_id else f"seg_none_{idx:03d}",
        "scene_id": scene_id,
        "beat_ids": seg["beat_ids"],
        "_hints": {
            "dialogue_speakers": entry_order,
            "entry_order": entry_order,
            "char_meta": char_meta,
        },
    }


def build_segments(plots, rank_table, drawable=None):
    """对所有节点切分，只保留含合格多角色段的节点。返回 {node: [segment_record, ...]}。"""
    out = {}
    for node, beats in plots.items():
        segs = segment_chapter(beats, drawable)
        if not segs:
            continue   # 无多角色段的节点不输出
        out[node] = [build_segment_record(s, i + 1, rank_table) for i, s in enumerate(segs)]
    return out


def build_name_to_id(plots, characters):
    """构建 full_name -> speaker_id 映射。
    主来源：plots 各 beat 的 (full_name, speaker_id)；
    别名兜底：characters 的 other_names 也指向同一 speaker_id，
    以防 LLM 输出别名或纳入未发言的在场者。
    """
    name2id = {}
    for beats in plots.values():
        for b in beats:
            fn, sid = b.get("full_name"), b.get("speaker_id")
            if fn and sid:
                name2id.setdefault(fn, sid)
    # 别名兜底：若主名已知 speaker_id，则其所有 other_names 也映射到该 id
    for name, v in characters.items():
        sid = name2id.get(name)
        if sid:
            for alias in v.get("other_names", []):
                name2id.setdefault(alias, sid)
    return name2id


def _to_id(name, name2id):
    """中文名转 speaker_id；查不到返回 None，由调用方剔除该角色。

    产物是 speaker_id 口径，下游按 ``{speaker_id}_{组合}_{情绪}`` 拼素材。
    保留中文名会让下游拼出一个必然取不到素材的路径，因此转不出 id 的角色
    只能丢弃，而不是原样带出。
    """
    sid = name2id.get(name)
    if sid is None:
        logger.warning(f"⚠️ full_name '{name}' 无对应 speaker_id，剔除该角色")
    return sid


def convert_windows_to_ids(display_windows, name2id):
    """把一个段的 display_windows 中 onstage/center/positions 的中文名转为 speaker_id。

    转换后按剩余人数重排档位：转不出 id 的角色被剔除，可能把 3 人窗口降为
    2 人，不足 2 人的窗口整体丢弃（回落下游单人默认态）。
    """
    out = []
    for w in display_windows:
        if not isinstance(w, dict):
            continue
        pos = w.get("positions") or {}
        if not isinstance(pos, dict):
            pos = {}
        pos = {k: v for k, v in pos.items() if isinstance(v, str)}
        onstage = []
        for n in (w.get("onstage") or []):
            sid = _to_id(n, name2id) if isinstance(n, str) else None
            if sid and sid not in onstage:
                onstage.append(sid)

        new_pos = {}
        for k, v in pos.items():
            sid = _to_id(k, name2id) if isinstance(k, str) else None
            if sid in onstage:
                new_pos[sid] = v

        if len(onstage) < 2:
            logger.info(
                f"🧹 {w.get('window_id')} 转 speaker_id 后不足2人，丢弃该窗口"
            )
            continue

        expected = SLOTS_BY_COUNT.get(len(onstage))
        if expected is None or sorted(new_pos.values()) != sorted(expected) \
                or set(new_pos) != set(onstage):
            onstage.sort(key=lambda s: SLOT_ORDER.get(new_pos.get(s), SLOT_ORDER["Center"]))
            slots = sorted(SLOTS_BY_COUNT[len(onstage)], key=lambda s: SLOT_ORDER[s])
            new_pos = dict(zip(onstage, slots))

        center = _to_id(w["center"], name2id) if w.get("center") else None
        out.append({
            "window_id": w.get("window_id"),
            "beat_ids": w.get("beat_ids", []),
            "onstage": onstage,
            "center": center if center in onstage else onstage[0],
            "positions": new_pos,
            "trigger": w.get("trigger", ""),
        })
    return out


# ============================================================
# 阶段 A：scene 级阵营前置扫描
# ============================================================
def load_char_profiles(chars):
    prof = {}
    for name, v in chars.items():
        cv = (v.get("clothing_variants") or [{}])[0]
        wb = (v.get("whole_body_variants") or [{}])[0]
        prof[name] = {"rank": v.get("important_rank", 0),
                      "other_names": v.get("other_names", []),
                      "occupation": cv.get("occupation", ""),
                      "gender": wb.get("gender", "")}
    return prof


def _scene_beats(beats, scene_id):
    return [b for b in beats if b.get("scene_id") == scene_id
            and b.get("beat_type") not in SYSTEM_TYPES]


def _dialogue_affinity(sbeats):
    seq = [b.get("full_name") for b in sbeats
           if b.get("beat_type") in DIALOGUE_TYPES and b.get("full_name")]
    pair = defaultdict(int)
    for a, b in zip(seq, seq[1:]):
        if a != b:
            pair[tuple(sorted((a, b)))] += 1
    return [{"pair": list(k), "turns": v} for k, v in sorted(pair.items(), key=lambda x: -x[1])]


def _find_mentioned_chars(sbeats, all_profiles):
    full_text = "".join((b.get("content") or "") for b in sbeats)
    hit = set()
    for name, prof in all_profiles.items():
        names = [name] + list(prof.get("other_names", []))
        if any(nm and nm in full_text for nm in names):
            hit.add(name)
    return hit


def build_stage_a_input(beats, scene_id, char_profiles):
    sbeats = _scene_beats(beats, scene_id)
    speakers = list(OrderedDict(
        (b.get("full_name"), None) for b in sbeats
        if b.get("beat_type") in DIALOGUE_TYPES and b.get("full_name")).keys())
    mentioned = _find_mentioned_chars(sbeats, char_profiles)
    candidates = list(OrderedDict((n, None) for n in (speakers + sorted(mentioned))).keys())
    profiles = {name: char_profiles.get(name, {"rank": None, "other_names": [],
                                               "occupation": "", "gender": ""})
                for name in candidates}

    lines = []
    for b in sbeats:
        t = b.get("beat_type")
        tag = "发言" if t in DIALOGUE_TYPES else "旁白"
        line = {"beat_id": b.get("beat_id"), "kind": tag,
                "speaker": (b.get("full_name") or "") if tag == "发言" else "",
                "text": b.get("content") or ""}
        if b.get("emotion"):
            line["emotion_hint"] = b["emotion"]
        lines.append(line)

    return {"scene_id": scene_id, "dialogue_speakers": speakers,
            "char_profiles": profiles, "affinity": _dialogue_affinity(sbeats), "beats": lines}


def _collect_segment_local_names(seg, beats, characters, faction_state=None):
    """Return characters that are evidenced by this segment itself.

    Stage A intentionally works at scene scope, but its ``onstage`` result must
    not leak into every segment in that scene.  This helper is the rule-layer
    boundary for Stage B: current segment speakers are always eligible; a
    silent character is eligible only when the segment text explicitly names
    that character (or one of its aliases).  Characters mentioned in another
    segment are therefore not carried forward accidentally.
    """
    beat_ids = {str(bid) for bid in (seg.get("beat_ids") or [])}
    segment_beats = [b for b in beats if str(b.get("beat_id")) in beat_ids]
    names = OrderedDict()

    for b in segment_beats:
        if b.get("beat_type") in DIALOGUE_TYPES and b.get("full_name"):
            names.setdefault(b["full_name"], None)

    # ``segment_chapter`` currently emits dialogue-only beat runs.  Do not
    # treat a character name mentioned inside a spoken line as proof that the
    # character is physically on screen (e.g. "the assassin's package").
    # If the segment format later includes narration/action beats, those beats
    # are considered below as the only source of silent on-screen characters.
    action_beats = [b for b in segment_beats
                    if b.get("beat_type") not in DIALOGUE_TYPES
                    and b.get("beat_type") not in SYSTEM_TYPES]
    text = "".join(str(b.get("content") or "") for b in action_beats)

    # Build canonical-name -> aliases from the character table.  Include names
    # present in Stage A's candidate pool as well, so this still works when a
    # character profile is partial or loaded from a different source.
    candidates = OrderedDict()
    for canonical, profile in (characters or {}).items():
        candidates.setdefault(canonical, set()).update(profile.get("other_names") or [])
    for canonical in (faction_state or {}).get("onstage", []):
        candidates.setdefault(canonical, set())
    for faction in (faction_state or {}).get("initial_factions", []):
        for canonical in faction.get("members", []) or []:
            candidates.setdefault(canonical, set())
    for event in (faction_state or {}).get("faction_events", []):
        for canonical in event.get("members_moved", []) or []:
            candidates.setdefault(canonical, set())

    for canonical, aliases in candidates.items():
        if canonical and canonical in text:
            names.setdefault(canonical, None)
            continue
        if any(alias and alias in text for alias in aliases):
            names.setdefault(canonical, None)

    return list(names)


STAGE_A_PROMPT = """你是视觉小说演出编排系统的「阵营扫描」模块。给你一个完整对话场景(文本块)，请初始化临时阵营组，并标记场景内的阵营变更事件。

# 阵营划分规则（满足任一即同阵营，按 P1>P2>P3 优先级）
- P1 物理：明确的保护、搀扶、背靠背、亲密接触。
- P2 空间：共同推门进入且未分开；处于同一狭小空间内。
- P3 组织：明确隶属同阵营；本场对话有共同利益；战斗或对峙的双方各自成阵营。
允许单人阵营。
细分：同阵营内若角色分属不同上下阶级，且同阶级角色≥2个，需细分子阵营（如教主与四大天王分开）。
强拆：若同一阵营>4人，按对话关联度(affinity，往返多者优先保留同组)强行拆分。
多重归属：某角色同时满足多种关系时，按 P1>P2>P3 取。

# 在场判定（先确定"谁在这一场画面里"，再编组）
只有"实际在场参与"的角色才纳入阵营。判定标准：
- 计入在场：本场有台词发言的角色；或被文本明确描写做出动作/反应/被施动的角色（如"她扶住他""某人脸颊飞红""某人冷笑"）。
- 排除在场（不进任何阵营）：仅作为"主角视角"叙事载体出现、本人无台词无动作无互动的纯观察者。典型如"凌妙妙察觉慕声不高兴"——这是借主角视角描写他人，凌妙妙本人未参与互动，属纯POV观察，不计入在场。
- 判断依据是"该角色是否在这一场画面中实际参与"，而非"是否被提及"。被提及≠在场。
- 在 onstage 字段列出最终判定为在场的全部角色。

# 阵营变更事件（在场景推进中检测，标注发生在哪个 beat 之后）
- emotion_split 共同情绪/反应：同一事件引发相反情绪/反应的角色，按情绪相近性重新分组。
- interest_shift 利益诉求变更：对话中出现新短期目标，具备共同目标者形成临时共同体（可使死敌临时结盟）。
- private_channel 排他性交流：通过压低声音/传音/眼神/暗号交流的角色临时编为一组。
- unbind 解绑：某角色位置改变或明确脱离原阵营，强制脱离并独立。

# 注意
- char_profiles 给出每个角色的别名(other_names)、rank(数字越大越重要)、职业。用别名表识别文中对同一角色的不同称呼。
- rank 不用于判断阵营归属，只在后续座次排布时参考；阵营归属一律依据文本中的 P1/P2/P3 证据。
- 旁白(kind=旁白)虽不发言，但常包含 P1/P2 的关键证据和情绪信号，务必据此判断。
- emotion_hint 仅作参考信号，对"表里不一/反讽"型角色可能失真，当文本语义与 emotion_hint 冲突时以文本为准。
- basis 字段必须引用具体规则代号(P1/P2/P3)和文本依据。

# 输入
{INPUT_JSON}

# 输出
只输出 JSON，不要任何解释或 markdown 包裹：
{
  "scene_id": "...",
  "onstage": ["实际在场角色名（已排除纯POV观察者）"],
  "initial_factions": [
    {"group_id": "F1", "members": ["角色名"], "basis": "P3-对峙主方，beat176致谢"}
  ],
  "faction_events": [
    {"after_beat": "179", "type": "emotion_split", "members_moved": ["慕声"], "to_group": "Fx", "detail": "..."}
  ]
}
"""


def _render(prompt_tmpl, payload):
    return prompt_tmpl.replace("{INPUT_JSON}", json.dumps(payload, ensure_ascii=False, indent=2))


def list_multi_speaker_scenes(beats):
    """节点内含多人对话(>=2发言者)的 scene_id，按出现顺序。"""
    order, agg = [], {}
    for b in beats:
        if b.get("beat_type") not in DIALOGUE_TYPES:
            continue
        sc, spk = b.get("scene_id"), b.get("full_name")
        if not sc or not spk:
            continue
        if sc not in agg:
            agg[sc] = set(); order.append(sc)
        agg[sc].add(spk)
    return [sc for sc in order if len(agg[sc]) >= 2]


# ============================================================
# 阶段 B：段级落位
# ============================================================
def build_stage_b_input(seg, faction_state, beats, characters, drawable=None):
    hints = seg.get("_hints", {})
    idset = set(seg.get("beat_ids", []))
    seg_beat_lines = []
    for b in beats:
        if b.get("beat_id") in idset and b.get("beat_type") not in SYSTEM_TYPES:
            t = b.get("beat_type")
            line = {"beat_id": b.get("beat_id"),
                    "kind": "发言" if t in DIALOGUE_TYPES else "旁白",
                    "speaker": (b.get("full_name") or "") if t in DIALOGUE_TYPES else "",
                    "text": b.get("content") or ""}
            if b.get("emotion"):
                line["emotion_hint"] = b["emotion"]
            seg_beat_lines.append(line)

    speakers = hints.get("dialogue_speakers", [])
    segment_local_names = _collect_segment_local_names(
        seg, beats, characters, faction_state=faction_state
    )

    # Stage A is scene-scoped. Restrict its result to evidence from this
    # segment before handing it to the placement model; otherwise a character
    # who spoke/acted elsewhere in the same scene is incorrectly rendered here.
    scene_onstage = (faction_state or {}).get("onstage", [])
    eligible = set(segment_local_names) | set(speakers)
    onstage = [n for n in scene_onstage if n in eligible]
    for speaker in speakers:
        if speaker not in onstage:
            onstage.append(speaker)

    if drawable:
        # 无立绘角色不能进 onstage_pool，否则会排出"一侧没图"的窗口
        onstage = [n for n in onstage if n in drawable]
        speakers = [n for n in speakers if n in drawable]
    related = list(OrderedDict((n, None) for n in (list(onstage) + list(speakers))).keys())
    profiles = {}
    for name in related:
        v = characters.get(name, {})
        cv = (v.get("clothing_variants") or [{}])[0]
        profiles[name] = {"rank": v.get("important_rank"),
                          "is_protagonist": hints.get("char_meta", {}).get(name, {}).get("is_protagonist", False),
                          "other_names": v.get("other_names", []),
                          "occupation": cv.get("occupation", "")}

    entry_order = hints.get("entry_order", [])
    factions = (faction_state or {}).get("initial_factions", [])
    faction_events = (faction_state or {}).get("faction_events", [])
    if drawable:
        entry_order = [n for n in entry_order if n in drawable]
        factions = [dict(f, members=[m for m in (f.get("members") or []) if m in drawable])
                    for f in factions]
        factions = [f for f in factions if f["members"]]
        faction_events = [
            dict(e, members_moved=[m for m in (e.get("members_moved") or []) if m in drawable])
            for e in faction_events
        ]
        faction_events = [e for e in faction_events if e["members_moved"]]

    return {"seg_id": seg.get("seg_id"), "scene_id": seg.get("scene_id"),
            "seg_kind": hints.get("seg_kind"), "onstage_pool": onstage,
            "segment_local_names": segment_local_names,
            "entry_order": entry_order,
            "factions": factions,
            "faction_events": faction_events,
            "char_profiles": profiles, "beats": seg_beat_lines}


STAGE_B_PROMPT = """你是视觉小说演出编排系统的「站位落位」模块。给你一个对话段(seg)及其阵营状态，请输出该段的 display_windows（显示窗口）。

# 背景规则
同屏角色最多 3 个，严禁超过。档位按同屏人数确定：
- 2 人同屏：使用 Left_Center 与 Right_Center（两人居中对称排布，不设单一 Center）。
- 3 人同屏：使用 Left、Center、Right（三档各一人）。

# Center / 戏剧核心
确定本窗口的戏剧核心角色——全场情绪/关系/视线/冲突围绕其变化。从以下维度确立：
- 情感中心：决定/改变当前气氛情绪的角色。
- 冲突中心：当前矛盾围绕其展开（被质问/被争夺/被保护者）。
- 关系中心：连接多个角色的交汇点（如调停者）。
- 关注中心：所有人视线/讨论集中的对象。
- 权力中心：当前关系中的绝对上位者。
无明确戏剧核心的日常段：按相关性文本比重 + 角色定位（绝对主角默认优先核心、BOSS身份）确定。
核心跟随戏剧核心，不是"谁正在说话谁就核心"。
排布规则：3 人窗口中，戏剧核心放 Center；2 人窗口中，戏剧核心放 Left_Center（更靠视觉重心一侧），另一人放 Right_Center。
center 字段始终填写该窗口的戏剧核心角色名（无论 2 人或 3 人）。

# 左右排布（反映剧情地位、阵营、敌我）
- 同一阵营的角色尽量同侧；对立/冲突的阵营分置两侧。
- 主角团成员(is_protagonist=true)倾向左侧（Left / Left_Center）。
- entry_order 给出入场顺序：先加入对话者倾向右侧，后加入者倾向左侧。
- 优先级：阵营归属 > 入场顺序（入场顺序仅在同阵营、无明显主次时用于排序）。

# display_windows（动态轮换）
- **每个 window 的 onstage 只能包含在该 window 自己的 beat_ids 范围内发言过的角色**（即这些 beat 里 kind=发言 的 speaker）。在本段别处说过话、但在这个 window 内一句没说的角色，必须排除，不得因为"他还在场"而保留。
- 若 onstage_pool ≤ 3：只输出 1 个 window，覆盖本段全部 beat，onstage 即全体。
- 若 onstage_pool > 3：按发言焦点把段切成多个 window。每个 window 显示「当前焦点相关的 ≤3 人」（戏剧核心 + 与之直接互动/冲突的角色）。焦点转移时开新 window。
  · 每个 window 必须 onstage ≤ 3，且每人都在该 window 的 beat 内发过言。
  · 若按焦点切出的 window 内只有 1 个发言者，就把相邻 beat 并入同一 window，凑够 2 个发言者；确实凑不够的，宁可让该 window 覆盖更大范围，也不要放入未发言角色。
  · 相邻 window 之间，未变化的角色应尽量保持同一档位（避免无谓抖动）。
  · 用 faction_events 判断焦点转移点（如 emotion_split 发生后焦点转向被拆出的角色）。
- 每个 window 的 beat_ids 必须连续且不重叠，合并起来恰好覆盖本段全部 beat。

# 注意
- emotion_hint 仅参考，表里不一型角色以文本为准。
- onstage_pool 是**当前 segment 的最终在场白名单**，不是整场 scene 的角色列表；只输出其中的角色，严禁引入未在场角色或本 scene 其他 segment 的角色。
- 当前实现的 segment 通常是连续 dialogue 段，因此默认只保留本段发言者；只有输入中明确给出当前段 narration/action 证据时，才保留不发言但确实在画面中的角色。
- onstage_pool 是段级范围，window 级还要再收一层：角色进入某 window 的 onstage 的充分条件是他在该 window 的 beat_ids 内有 kind=发言 的台词。
- `segment_local_names` 是规则层计算的 segment 局部证据集合，仅供核对；不得把不在该集合中的角色加入 onstage。
- positions 的键必须等于该 window 的 onstage。
- 档位取值：2 人窗口只能用 {Left_Center, Right_Center} 各一；3 人窗口只能用 {Left, Center, Right} 各一。

# 输入
{INPUT_JSON}

# 输出
只输出 JSON，不要解释或 markdown 包裹：
{
  "seg_id": "...",
  "display_windows": [
    {
      "window_id": "w1",
      "beat_ids": ["175","176","177","178"],
      "onstage": ["柳拂衣","慕瑶","郡守"],
      "center": "慕瑶",
      "positions": {"柳拂衣":"Left","慕瑶":"Center","郡守":"Right"},
      "trigger": "开场致谢，焦点在被致谢的捉妖团队"
    }
  ]
}
"""


# ============================================================
# 阶段 C：剔除无立绘/不属于当前段的角色，按剩余人数重排档位
# ============================================================
# 从左到右的视觉次序，用于降档时保持相对左右关系
SLOT_ORDER = {"Left": 0, "Left_Center": 1, "Center": 2, "Right_Center": 3, "Right": 4}


def build_window_speaker_map(beats):
    """beat_id -> full_name，仅收录 dialogue beat。

    窗口级白名单的数据源：多人站位只表示"谁在这几个 beat 里开口说话"，
    因此判据必须是 beat_type == dialogue 的实际发言者，旁白/系统 beat 的
    speaker 不算。
    """
    return {str(b.get("beat_id")): b.get("full_name")
            for b in (beats or [])
            if b.get("beat_type") in DIALOGUE_TYPES and b.get("full_name")}


def window_speakers(window, speaker_by_beat):
    """该窗口 beat_ids 范围内实际发言过的角色集合。"""
    if not speaker_by_beat:
        return None
    return {speaker_by_beat[str(bid)]
            for bid in (window.get("beat_ids") or [])
            if str(bid) in speaker_by_beat}


def sanitize_windows(seg_id, windows, drawable, allowed_names=None,
                     speaker_by_beat=None):
    """剔除无效角色，并按剩余人数重排档位。

    3 人 -> 剩 2 人：改用 Left_Center / Right_Center，保持原有左右相对次序，
                     避免角色在窗口切换时横跨屏幕。
    剩 <2 人：整窗丢弃 —— 这些 beat 回落到下游"单人居中"默认状态。

    ``allowed_names`` 是 segment 级白名单，由规则层根据当前段的发言和
    明确动作描写构建，用于防止 scene 级 onstage 状态向后续段落泄漏。

    ``speaker_by_beat`` 再收一层到 window 级：段级白名单是整段发言者的并集，
    当一段被切成多个 window 时，"在本段说过话但在这个 window 的 beat 范围内
    一句没说"的角色仍能合法留在 onstage 里。这类角色下游取不到其
    speaker_combination，会输出 ``角色__calm_MS(Right)`` 这种空组合串，
    或沿用上一场残留的服装出图。
    """
    if not drawable and allowed_names is None and not speaker_by_beat:
        return windows

    allowed = set(allowed_names) if allowed_names is not None else None
    out = []
    for w in windows:
        if not isinstance(w, dict):
            logger.warning(f"⚠️ {seg_id} 窗口结构异常({type(w).__name__})，丢弃")
            continue
        onstage = w.get("onstage", [])
        if not isinstance(onstage, list):
            onstage = []
        pos = w.get("positions", {})
        if not isinstance(pos, dict):
            pos = {}
        # 档位值必须是字符串：模型偶发回 list/dict，会让后续按档位排序时
        # 触发 unhashable，整步站位提取崩掉。
        pos = {k: v for k, v in pos.items() if isinstance(v, str)}
        # Deduplicate while preserving the model's visual order.  Duplicate
        # names otherwise make the slot count and positions map disagree.
        unique_onstage = list(OrderedDict(
            (n, None) for n in onstage if isinstance(n, str) and n
        ).keys())
        spoken = window_speakers(w, speaker_by_beat)
        kept = [
            n for n in unique_onstage
            if (not drawable or n in drawable)
            and (allowed is None or n in allowed)
            and (spoken is None or n in spoken)
        ]
        dropped = [n for n in unique_onstage if n not in kept]
        silent = [n for n in dropped if spoken is not None and n not in spoken]
        # 去重/去非法项本身也算改动：这类窗口必须走下面的重排分支。否则
        # ``dropped`` 为空 + 结构看似合法时会命中快路径，把脏 onstage
        # （重复名、甚至整段 JSON 被塞成一个元素）原样保留下来。
        onstage_was_dirty = len(unique_onstage) != len(onstage)

        if len(kept) > MAX_ONSTAGE:
            logger.warning(
                f"⚠️ {seg_id}/{w.get('window_id')} 清理后仍有 {len(kept)} 人，"
                f"超过同屏上限 {MAX_ONSTAGE}，丢弃该窗口"
            )
            continue

        expected_slots = SLOTS_BY_COUNT.get(len(kept))
        current_slots = [pos.get(n) for n in kept]
        slots_are_strings = all(isinstance(slot, str) for slot in current_slots)
        structurally_valid = (
            not dropped
            and not onstage_was_dirty
            and set(pos.keys()) == set(kept)
            and expected_slots is not None
            and slots_are_strings
            and sorted(current_slots) == sorted(expected_slots)
            and (not w.get("center") or w.get("center") in kept)
        )
        if structurally_valid:
            out.append(w)
            continue

        silent_note = f"（其中本窗口未发言 {silent}）" if silent else ""
        if len(kept) < 2:
            logger.info(
                f"🧹 {seg_id}/{w.get('window_id')} 剔除无效角色 {dropped}{silent_note} "
                f"后不足2人，丢弃该窗口"
            )
            continue

        kept.sort(key=lambda n: SLOT_ORDER.get(pos.get(n), SLOT_ORDER["Center"]))
        slots = sorted(SLOTS_BY_COUNT[len(kept)], key=lambda s: SLOT_ORDER[s])
        new_pos = dict(zip(kept, slots))
        center = w.get("center") if w.get("center") in kept else kept[0]
        logger.info(
            f"🧹 {seg_id}/{w.get('window_id')} 剔除无效角色 {dropped}{silent_note}，"
            f"降为 {len(kept)} 人窗口"
        )
        out.append({**w, "onstage": kept, "center": center, "positions": new_pos})
    return out


def validate_windows(seg, result):
    """硬约束校验，返回 (ok, problems)。"""
    problems = []
    # beat_id 在 plots 里是字符串，但模型常回整数。统一成字符串比较，否则
    # 合法窗口会被误判成"未覆盖本段 beat"。
    seg_beat_set = {str(b) for b in (seg.get("beat_ids") or [])}
    windows = (result or {}).get("display_windows", [])
    if not windows:
        return False, ["display_windows 为空"]

    covered = []
    for i, w in enumerate(windows):
        wid = w.get("window_id", f"w{i+1}")
        onstage = w.get("onstage") or []
        if not isinstance(onstage, list):
            onstage = []
        pos = w.get("positions") or {}
        if not isinstance(pos, dict):
            pos = {}
        center = w.get("center")
        n = len(onstage)

        if n > MAX_ONSTAGE:
            problems.append(f"{wid}: onstage {n} 人 > {MAX_ONSTAGE}")
        # onstage 可能混入非字符串脏数据，不能直接进 set
        if any(not isinstance(x, str) for x in onstage):
            problems.append(f"{wid}: onstage 含非字符串项")
        elif set(pos.keys()) != set(onstage):
            problems.append(f"{wid}: positions 键与 onstage 不一致")

        # 按人数校验档位：取值必须恰好等于该人数对应的档位集（各档位用一次）
        expected = SLOTS_BY_COUNT.get(n)
        if expected is None:
            problems.append(f"{wid}: 不支持的同屏人数 {n}")
        else:
            used = list(pos.values())
            if not all(isinstance(s, str) for s in used):
                problems.append(f"{wid}: positions 含非字符串档位")
            elif sorted(used) != sorted(expected):
                problems.append(f"{wid}: {n}人档位应为 {sorted(expected)}，实际 {sorted(used)}")

        # center 字段一致性：必须是 onstage 之一
        if center and center not in onstage:
            problems.append(f"{wid}: center 字段({center})不在 onstage 内")

        covered.extend(str(b) for b in (w.get("beat_ids") or []))

    if [b for b in covered if b not in seg_beat_set]:
        problems.append("窗口含本段之外的 beat")
    if len(covered) != len(set(covered)):
        problems.append("窗口 beat_ids 有重叠")
    if set(covered) != seg_beat_set:
        problems.append("窗口未完整覆盖本段 beat")
    return (len(problems) == 0), problems


# ============================================================
# LLM 调用
# ============================================================
def _strip_json_fence(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


@retry(stop=stop_after_attempt(_API_MAX_RETRIES),
       wait=wait_random_exponential(min=1, max=30))
async def _call_llm(prompt):
    resp, used_model = await _model_fallback.achat(
        _client,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    if _usage_tracker and getattr(resp, "usage", None):
        _usage_tracker.add_llm_usage(
            resp.usage, used_model, provider="openai_compatible"
        )
    return resp.choices[0].message.content


async def _scan_scene(beats, scene_id, char_profiles, sem):
    payload = build_stage_a_input(beats, scene_id, char_profiles)
    prompt = _render(STAGE_A_PROMPT, payload)
    async with sem:
        raw = await _call_llm(prompt)
    try:
        return parse_model_json(
            _strip_json_fence(raw), context=f"stage_a/{scene_id}", logger=logger
        )
    except Exception as e:
        logger.warning(f"⚠️ [阶段A] JSON 解析失败 scene={scene_id} err={e} raw={(raw or '')[:200]}")
        return None


async def _place_seg(seg, faction_state, beats, characters, sem, drawable=None):
    payload = build_stage_b_input(seg, faction_state, beats, characters, drawable)
    prompt = _render(STAGE_B_PROMPT, payload)
    async with sem:
        raw = await _call_llm(prompt)
    try:
        result = parse_model_json(
            _strip_json_fence(raw),
            context=f"stage_b/{seg.get('seg_id')}",
            logger=logger,
        )
    except Exception as e:
        logger.warning(f"⚠️ [阶段B] JSON 解析失败 seg={seg.get('seg_id')} err={e} raw={(raw or '')[:200]}")
        return None
    ok, problems = validate_windows(seg, result)
    if not ok:
        logger.warning(f"⚠️ [阶段B] 校验未过 seg={seg.get('seg_id')} problems={problems}")
    # 校验后再清理：清理会丢窗口，覆盖率不再完整（属预期，缺口回落单人默认态）
    # Final rule-layer guard: even if the placement model repeats a scene-level
    # character or hallucinates a mentioned character, a window may only use
    # the segment-local eligible set.  This is deliberately after validation;
    # sanitization can reduce a 3-person window to a valid 2-person window.
    allowed_names = set(payload.get("onstage_pool", []))
    result["display_windows"] = sanitize_windows(
        seg.get("seg_id"), result.get("display_windows", []), drawable,
        allowed_names=allowed_names,
        speaker_by_beat=build_window_speaker_map(beats))
    return result


# ============================================================
# 节点处理：串联三阶段
# ============================================================
async def process_node(node, beats, segments, char_profiles, characters, name2id, sem,
                       drawable=None):
    """处理单个节点，返回该节点的最终 segments（含 display_windows）。"""
    # 阶段 A：并发扫描所有多人 scene
    scenes = list_multi_speaker_scenes(beats)
    a_results = await asyncio.gather(*[_scan_scene(beats, sc, char_profiles, sem) for sc in scenes])
    faction_map = {sc: r for sc, r in zip(scenes, a_results) if r is not None}
    logger.info(f"  [{node}] 阶段A 完成: {len(faction_map)}/{len(scenes)} scene 编组成功")

    # 阶段 B：所有段都是合格多角色段，全部送落位
    async def _one(seg):
        fs = faction_map.get(seg.get("scene_id"))
        result = await _place_seg(seg, fs, beats, characters, sem, drawable)
        return seg.get("seg_id"), result

    placed = dict(await asyncio.gather(*[_one(s) for s in segments]))
    logger.info(f"  [{node}] 阶段B 完成: {sum(1 for v in placed.values() if v)}/{len(segments)} 段落位成功")

    # 合成最终段记录（去掉 _hints，挂 display_windows）。落位失败的段跳过不输出。
    final_segs = []
    for s in segments:
        placed_result = placed.get(s["seg_id"])
        if not (placed_result and placed_result.get("display_windows")):
            logger.warning(f"⚠️ 段 {s['seg_id']} 落位失败/为空，跳过不输出")
            continue
        final_segs.append({
            "seg_id": s["seg_id"], "scene_id": s["scene_id"],
            "beat_ids": s["beat_ids"],
            "display_windows": convert_windows_to_ids(
                placed_result["display_windows"], name2id),
        })
    return final_segs


# ============================================================
# 入库 / 降级
# ============================================================
def save_character_position(task_id, data):
    """写入 character_position 表；失败降级到本地。"""
    try:
        with global_engine.begin() as conn:
            sql = sa_text("""
                INSERT INTO character_position (task_id, data, create_time, update_time)
                VALUES (:tid, :data, NOW(), NOW())
                ON CONFLICT (task_id) DO UPDATE SET
                    data = EXCLUDED.data,
                    update_time = NOW()
                RETURNING id
            """)
            row = conn.execute(sql, {"tid": task_id,
                                     "data": json.dumps(data, ensure_ascii=False)}).fetchone()
            if row is None:
                logger.warning("⚠️ [CharPosition] RETURNING id 未返回，可能缺少唯一约束")
                return None
            logger.info(f"💾 [CharPosition] 入库成功! DB_ID: {row[0]}")
            return row[0]
    except Exception as e:
        logger.error(f"❌ [CharPosition] 入库失败: {e}")
        try:
            fallback_dir = os.path.join(OUTPUTS_PATH, str(task_id))
            os.makedirs(fallback_dir, exist_ok=True)
            fallback_path = os.path.join(fallback_dir, "character_position.json")
            with open(fallback_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"📁 [CharPosition] 已降级保存到本地: {fallback_path}")
        except Exception as fe:
            logger.error(f"❌ [CharPosition] 本地降级保存也失败: {fe}")
        return None


# ================= 主入口 =================
async def main(target_task_id=None):
    """提取多角色站位。

    站位属于增强信息：缺失时下游按单人格式渲染该 beat。因此这里的任何失败
    （含上游数据缺失）都只告警并返回 None，绝不抛异常中断流水线。
    """
    global _client
    try:
        task_id, plots, characters = fetch_task_context(target_task_id)
    except Exception as e:
        logger.warning(f"⚠️ [CharPosition] 读取上游数据失败，跳过站位提取: {e}")
        return None
    if not task_id or not plots:
        logger.warning("⚠️ [CharPosition] 未获取到 plots 数据，跳过站位提取")
        return None
    if not characters:
        logger.warning("⚠️ [CharPosition] characters 为空，仍尝试按 plots 提取")

    if _usage_tracker:
        try:
            _usage_tracker.set_context(task_id, "info_extractor")
        except Exception:
            pass

    logger.info("--- [CharPosition] 开始多角色站位提取 ---")

    _client = None
    final = {}
    try:
        rank_table = build_rank_table(characters)
        char_profiles = load_char_profiles(characters)
        name2id = build_name_to_id(plots, characters)
        drawable = build_drawable_names(characters)
        if not drawable:
            logger.warning("⚠️ 无角色数据，跳过无立绘角色过滤")
        segments_by_node = build_segments(plots, rank_table, drawable)

        if not segments_by_node:
            logger.info("本任务无任何合格多角色对话段，产物为空。")

        _client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)
        sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        for node, node_segments in segments_by_node.items():
            logger.info(f"▶ 处理节点 {node}（{len(node_segments)} 个多角色段）")
            try:
                node_segs = await process_node(
                    node, plots[node], node_segments, char_profiles, characters, name2id, sem,
                    drawable)
            except Exception as e:
                # 单节点失败不牵连其他节点，缺失的节点由下游按单人格式渲染。
                logger.warning(f"⚠️ 节点 {node} 站位提取失败，跳过该节点: {e}")
                continue
            if node_segs:                       # 落位后仍有内容才输出该 node
                final[node] = {"segments": node_segs}
    except Exception as e:
        logger.warning(f"⚠️ [CharPosition] 站位提取失败，本任务不产出站位数据: {e}")
        logger.warning(traceback.format_exc())
        return None
    finally:
        if _client is not None:
            try:
                await _client.close()
            except Exception:
                pass

    try:
        save_character_position(task_id, final)
    except Exception as e:
        logger.warning(f"⚠️ [CharPosition] 站位入库失败，本任务不产出站位数据: {e}")
        return None
    logger.info(f"--- [CharPosition] 全部完成，{len(final)} 个节点含多角色站位 ---")
    return task_id


if __name__ == "__main__":
    tid = asyncio.run(main(target_task_id="test_fb89e1c4"))
    print(f"done. task_id={tid}")
