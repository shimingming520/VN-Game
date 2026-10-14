"""
信息提取器主程序 (Info Extractor Main Pipeline)
将原先分散在各服务中的信息提取代码聚合到 info_extractor 下统一运行。

默认执行顺序（完整流程）：
    先完成“故事树元数据”阶段；随后并行启动“场景提取与归并”长链和“角色与剧情
    基础链”；两条链在“场景对齐”阶段汇合，之后执行状态组合与下游素材并行组，
    最后进行“统一换名”和“最终聚合”。

业务步骤编号仍保留在代码和 CLI 参数中，用于兼容旧调用；默认日志使用以下
执行阶段名称，不再按旧的 1~17 顺序展示：
    执行阶段 1：故事树元数据
    执行阶段 2A：场景提取与归并
    执行阶段 2B：角色与剧情基础链
    执行阶段 2C：封面素材
    执行阶段 3A：角色排名与改名映射
    执行阶段 3B：场景对齐
    执行阶段 4：角色语体
    执行阶段 5：人物状态组合
    执行阶段 6A：人物提示词
    执行阶段 6B：多角色站位
    执行阶段 6C：场景提示词
    执行阶段 6D：CG 文字信息
    执行阶段 6E：声音描述
    执行阶段 7：统一换名
    执行阶段 8：最终聚合

================================================================================
使用说明 (Usage)
================================================================================

# 1. 自动处理最新任务（完整流程）
python novel-game-factory/services/info_extractor/main.py

# 2. 指定任务 ID
python novel-game-factory/services/info_extractor/main.py --task-id TASK_ID

# 3. 强制重新处理（即使数据已存在）
python novel-game-factory/services/info_extractor/main.py --force --task-id TASK_ID

# 4. 使用不同的 LLM 提供商
python novel-game-factory/services/info_extractor/main.py --provider qwen

# 5. 仅执行剧情处理阶段（beats + 排名 + 改名 + NPC + 语体表 + 状态组合）
#    前置依赖 characters / name_mappings 需已存在
python novel-game-factory/services/info_extractor/main.py --stage plot --task-id

# 6. 仅执行场景处理阶段（场景提取、对齐及提示词）
python novel-game-factory/services/info_extractor/main.py --stage scene --task-id

# 7. 专门提取场景：强制重新提取整条场景链，重试直到 scene_prompts 表写入成功
python novel-game-factory/services/info_extractor/main.py --stage scene-force --task-id

# 8. 仅构建 CG 区间与 CG 文字提示词
python novel-game-factory/services/info_extractor/main.py --stage cg --task-id

# 9. 从完整流程的第 N 个兼容断点开始跑到结尾（仅 --stage all 可用，N 为 1~17）
#    示例：从人物提示词阶段对应的兼容断点开始跑到结尾
python novel-game-factory/services/info_extractor/main.py --start-step 9 --task-id TASK_ID --force

================================================================================
命令行参数说明
================================================================================
  --task-id   任务ID（如果不提供则自动查找最新任务）
  --provider  LLM提供商 (openai/claude/qwen，默认: qwen)
  --model     模型名称（默认根据提供商选择）
  --stage     执行阶段 (all/plot/scene/scene-force/cg，默认: all)
                all         = 完整流程
                plot        = 仅剧情处理阶段（剧情 beats + 排名 + 改名 + NPC + 角色语体表 + 状态组合）
                scene       = 仅场景处理阶段（场景提取、对齐及提示词）
                scene-force = 专门提取场景，强制重新提取，重试直到 scene_prompts 表写入成功
                cg          = 仅构建 cg 表并生成 CG 文字提示词
  --force        强制重新处理，即使数据已存在
  --scene-retries 场景强制提取的最大重试次数（仅 scene-force 阶段，默认: 3）
  --start-step   从完整流程的兼容断点 N 开始跑到结尾（1~17，仅 --stage all 时可用）
  --verbose      显示详细日志

================================================================================
"""

import os
import sys
import json
import time
import asyncio
import logging
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# ============================================================
# 路径配置：必须在导入子模块之前完成。
# 各子模块的导入方式不统一（部分用相对导入，部分用裸导入），
# 这里一次性补齐所有需要的搜索路径，保持各模块原样不动。
#   - novel-game-factory/        -> common.config / common.usage_tracker
#   - services/                  -> info_extractor.* 包导入 / services.char_designer.*
#   - info_extractor/            -> scene 模块的 `from utils.xxx import ...`
#   - info_extractor/utils/      -> plot/atmosphere 的 `from logger_config import ...`
# ============================================================
current_dir = Path(__file__).resolve().parent            # .../services/info_extractor
services_dir = current_dir.parent                         # .../services
project_root_path = services_dir.parent                   # .../novel-game-factory
utils_dir = current_dir / "utils"

for path in (str(project_root_path), str(services_dir), str(current_dir), str(utils_dir)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=project_root_path / ".env")
    load_dotenv(dotenv_path=services_dir / ".env")
except ImportError:
    pass
load_dotenv(override=True)
# 数据库默认配置（与 char_designer/main.py 保持一致，可被环境变量覆盖）
os.environ.setdefault("DB_HOST", "172.16.50.17")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_NAME", "extreme")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "change_me")

# 加载 .env（atmosphere/plot 在导入时即读取 LLM API Key 并初始化客户端，
# 因此必须在导入这些模块之前完成 .env 加载）
# try:
#     from dotenv import load_dotenv
#     load_dotenv(dotenv_path=project_root_path / ".env")
#     load_dotenv(dotenv_path=services_dir / ".env")
# except ImportError:
#     pass

# 清理代理环境变量，避免 API 连接问题
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("all_proxy", None)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from common.config import DATABASE_URL, OUTPUTS_PATH, PLATFORM_DATABASE_URL
from common.progress_utils import StageLeaseLostError

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None


def _safe_usage_flush() -> bool:
    """用量落库只能告警，绝不能改变业务步骤的成功/失败结果。"""
    if not _usage_tracker:
        return True
    try:
        flushed = bool(_usage_tracker.flush())
        if not flushed:
            logger.warning("⚠️ usage_tracker.flush 未成功落库，已缓存并继续业务流程")
        return flushed
    except Exception as exc:
        logger.warning(f"⚠️ usage_tracker.flush 失败，已忽略且不影响流程: {exc}")
        return False


def _safe_usage_set_context(*args, **kwargs):
    """设置用量上下文失败时禁用本轮统计，但继续业务流程。"""
    if not _usage_tracker:
        return None
    try:
        return _usage_tracker.set_context(*args, **kwargs)
    except Exception as exc:
        logger.warning(f"⚠️ usage_tracker.set_context 失败，已忽略且不影响流程: {exc}")
        try:
            _usage_tracker.clear_context()
        except Exception as clear_exc:
            logger.warning(
                f"⚠️ usage_tracker.clear_context 失败，已忽略: {clear_exc}"
            )
        return None

from sqlalchemy import create_engine, text

# characters / plots / story_tree_json 等跨模块共享表已迁移到平台库。
# 主管道的数据库辅助函数必须和各子步骤使用同一个库，否则会出现
# “写入成功后立即查询不到同一 task_id” 的跨库读写问题。
DB_URL = PLATFORM_DATABASE_URL

# ============================================================
# 导入各子模块（路径与 .env 已就绪）
# ============================================================
# character
from info_extractor.character.extract_raw import StoryTreeCharacterExtractor02
from info_extractor.character.extract_deduplicate import StoryTreeLLMDeduplicationAgent
from info_extractor.character.extract_combination_description import build_combination_descriptions
from info_extractor.character.beat_stats import run as beat_stats_run
from info_extractor.character.assign_combinations import (
    get_assignment_status,
    run as assign_combinations_run,
)
from info_extractor.character.char_prompt_builder import build_and_save_prompts
from info_extractor.character.name_mappings import run as name_mappings_run
# plot
from info_extractor.plot.extract_plots_to_db import main as extract_plots_main
from info_extractor.plot.ranked_name_mappings import (
    process_task as ranked_name_mappings_process_task,
    has_invalid_english_order,
)
from info_extractor.plot.add_npc_to_name_mappings import (
    SKIP_BEAT_TYPES as npc_mappings_skip_beat_types,
    SKIP_TOKENS as npc_mappings_skip_tokens,
    update_name_mappings as add_npc_to_name_mappings,
)
from info_extractor.plot.extract_character_styles import (
    main as extract_character_styles_main,
    saved_character_styles_are_current,
)
from info_extractor.plot.extract_character_position import main as extract_char_position_main
from info_extractor.plot.name_anonymizer import process_task as name_anonymizer_process_task
# scene
from info_extractor.scene.update_story_tree import StoryTreeUpdater
from info_extractor.scene.scene_extractor import run_extract_raw as scene_extract_raw_main
from info_extractor.scene.scene_extractor import run_merge as scene_extract_merge_main
from info_extractor.scene.scene_prompts import main as scene_prompts_main
from info_extractor.scene.scene_to_plot import main as scene_to_plot_main
from info_extractor.scene.cover_extractor import CoverExtractorFromDB
# cg
from info_extractor.cg.cg_build import cg_data_ready, run as cg_build_run
from info_extractor.cg.cg_prompt import main as cg_prompt_main
# atmosphere
# from info_extractor.atmosphere.special_effects import process_task as atmosphere_process_task
# sound
from info_extractor.sound.extract_sound_to_db import (
    SoundCheckpointPersistenceError,
    SoundExtractionIncompleteError,
    SoundInputUnavailableError,
    SoundPersistenceError,
    process_task as sound_extract_process_task,
    validate_public_sound_data,
)
# aggregation
from info_extractor.aggregation.integrate_material import process_ultimate
from info_extractor.utils.story_tree_json_parse import (
    load_story_tree_for_player,
)


# ============================================================
# 数据库辅助函数
# ============================================================

def _load_artifact(
    table: str, task_id: str, database_url: str = DB_URL
) -> Optional[Any]:
    """读取某张表的 data 字段。查不到或读失败都返回 None。"""
    try:
        engine = create_engine(database_url)
        with engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT data FROM {table} WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id},
            ).fetchone()
    except Exception as e:
        logger.warning(f"读取 {table} 数据失败，按需要重新生成: {e}")
        return None
    if not row or row[0] is None:
        return None
    data = row[0]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            logger.warning(f"{table} 内容不是合法 JSON，将重新生成: {e}")
            return None
    return data


def _artifact_is_reusable(
    table: str,
    task_id: str,
    *,
    validator: Callable[[Any], Optional[str]],
    database_url: str = DB_URL,
) -> bool:
    """重跑同一任务时判断已有产物能否直接复用。

    三道关卡：记录存在 -> data 非空 -> 结构符合该产物的约定。
    validator 返回 None 表示通过，返回字符串表示不可复用的原因。
    只校验产物自身结构，不比对上游是否变化（那由各步自己的 force/哈希判断）。
    """
    data = _load_artifact(table, task_id, database_url=database_url)
    if data is None:
        return False
    reason = validator(data)
    if reason:
        logger.info(f"♻️ 已有 {table} 不可复用，将重新生成: {reason}")
        return False
    return True


def _require_mapping(data: Any, label: str) -> Optional[str]:
    if not isinstance(data, dict):
        return f"{label} 不是对象（实际 {type(data).__name__}）"
    return None


def _validate_name_mappings_original(data: Any) -> Optional[str]:
    """Step 2 产物：original_name.conversion 必须有内容。"""
    reason = _require_mapping(data, "name_mappings")
    if reason:
        return reason
    original = data.get("original_name")
    if not isinstance(original, dict):
        return "缺少 original_name 段"
    conversion = original.get("conversion")
    if not isinstance(conversion, dict) or not conversion:
        return "original_name.conversion 为空"
    if not isinstance(data.get("change_name"), dict):
        return "缺少 change_name 段（Step 5/6 需要写入该段）"
    return None


def _validate_name_mappings_changed(data: Any) -> Optional[str]:
    """Step 5 产物：change_name 的改名表与三语显示名都要齐。"""
    reason = _validate_name_mappings_original(data)
    if reason:
        return reason
    change = data.get("change_name") or {}
    if not isinstance(change.get("new-name"), dict) or not change.get("new-name"):
        return "change_name.new-name 为空"
    if not isinstance(change.get("conversion"), dict) or not change.get("conversion"):
        return "change_name.conversion 为空"
    for locale in ("zh-CN", "en-US", "ja-JP"):
        if not isinstance(change.get(locale), dict) or not change.get(locale):
            return f"change_name.{locale} 为空"
    if has_invalid_english_order(change):
        return "change_name.en-US 含明确的中文姓名顺序反转"
    return None


def _validate_plots(data: Any) -> Optional[str]:
    """Step 3 产物：至少一个节点，且 beat 带 beat_id/beat_type。"""
    reason = _require_mapping(data, "plots")
    if reason:
        return reason
    if not data:
        return "没有任何剧情节点"
    for node_id, beats in data.items():
        if not isinstance(beats, list):
            return f"节点 {node_id} 的 beats 不是数组"
        for beat in beats:
            if not isinstance(beat, dict):
                return f"节点 {node_id} 存在非对象 beat"
            if beat.get("beat_id") is None or not beat.get("beat_type"):
                return f"节点 {node_id} 存在缺少 beat_id/beat_type 的 beat"
    return None


def _validate_character_styles(data: Any) -> Optional[str]:
    """Step 7 产物：work/characters 两段齐全且 characters 非空。"""
    reason = _require_mapping(data, "character_styles")
    if reason:
        return reason
    if not isinstance(data.get("work"), dict):
        return "缺少 work 段"
    characters = data.get("characters")
    if not isinstance(characters, dict) or not characters:
        return "characters 为空"
    return None


def _validate_character_position(data: Any) -> Optional[str]:
    """Step 10 产物：空 {} 合法（本书确实没有多人同屏段），非空则结构要对。"""
    reason = _require_mapping(data, "character_position")
    if reason:
        return reason
    for node_id, node_info in data.items():
        if not isinstance(node_info, dict):
            return f"节点 {node_id} 结构异常"
        segments = node_info.get("segments")
        if not isinstance(segments, list):
            return f"节点 {node_id} 缺少 segments 数组"
        for segment in segments:
            if not isinstance(segment, dict):
                return f"节点 {node_id} 存在非对象 segment"
            if not isinstance(segment.get("display_windows"), list):
                return f"节点 {node_id} 的 segment 缺少 display_windows"
    return None


def _validate_sound(data: Any) -> Optional[str]:
    """Step 15 公开产物校验；完整结构才允许整步跳过。"""
    return validate_public_sound_data(data)


def _npc_mappings_are_complete(task_id: str) -> bool:
    """Step 6 是否已跑过：plots 里出现的角色都能在 name_mappings 里查到。

    这一步的产物和 Step 2/5 共用 name_mappings 表，光看表里有记录分不出它有没有
    跑过，所以改为直接比对 plots 的角色名单是否已被 name_mappings 覆盖。
    """
    mappings = _load_artifact("name_mappings", task_id)
    if _validate_name_mappings_original(mappings) is not None:
        return False
    plots = _load_artifact("plots", task_id)
    if _validate_plots(plots) is not None:
        return False

    original_conversion = (mappings.get("original_name") or {}).get("conversion") or {}
    change = mappings.get("change_name") or {}
    change_conversion = change.get("conversion") or {}
    renamed = change.get("new-name") or {}

    # 复用 Step 6 自身的过滤规则，两处各写一份迟早会漂移。
    npc_skip_tokens = npc_mappings_skip_tokens
    npc_skip_beat_types = npc_mappings_skip_beat_types

    missing = []
    for beats in plots.values():
        for beat in beats:
            if str(beat.get("beat_type") or "") in npc_skip_beat_types:
                continue
            full_name = str(beat.get("full_name") or "").strip()
            if not full_name or full_name.lower() in npc_skip_tokens:
                continue
            if str(beat.get("speaker_id") or "").strip().lower() in npc_skip_tokens:
                continue
            if full_name in original_conversion:
                continue
            mapped = str(renamed.get(full_name) or full_name)
            if mapped in change_conversion or full_name in change_conversion:
                continue
            missing.append(full_name)

    if missing:
        unique_missing = sorted(set(missing))
        logger.info(
            f"♻️ name_mappings 尚未覆盖 {len(unique_missing)} 个剧情角色，需要补全 NPC: "
            f"{unique_missing[:8]}{'...' if len(unique_missing) > 8 else ''}"
        )
        return False
    return True


def _count_exists(table: str, task_id: str, database_url: str = DB_URL) -> bool:
    """检查某张表是否已存在指定 task_id 的数据"""
    try:
        engine = create_engine(database_url)
        with engine.connect() as conn:
            sql = text(f"SELECT COUNT(*) FROM {table} WHERE task_id = :tid")
            result = conn.execute(sql, {"tid": task_id}).fetchone()
            return result[0] > 0 if result else False
    except Exception as e:
        logger.error(f"检查 {table} 数据存在性失败: {e}")
        return False


def _story_tree_titled(task_id: str) -> bool:
    """检查 story_tree_json 是否已生成过书名（update_story_tree 已跑过）"""
    try:
        engine = create_engine(DB_URL)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM story_tree_json WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id}
            ).fetchone()
        if not row or not row[0]:
            return False
        selected = load_story_tree_for_player(
            row[0],
            task_id=task_id,
            db_engine=engine,
            logger=logger,
        )
        return bool(str(selected.get("book_title") or "").strip())
    except Exception as e:
        logger.error(f"检查 story_tree 书名状态失败: {e}")
        return False


def check_beat_stats_done(task_id: str) -> bool:
    """检查 beat_stats 是否已跑过：characters 中存在 important_rank != 1 的角色"""
    try:
        engine = create_engine(DB_URL)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM characters WHERE task_id = :tid"),
                {"tid": task_id}
            ).fetchone()
        if not row:
            return False
        data = row[0]
        if isinstance(data, str):
            data = json.loads(data)
        for char_entry in data.values():
            entry = char_entry[0] if isinstance(char_entry, list) and char_entry else char_entry
            if isinstance(entry, dict) and entry.get("important_rank", 1) != 1:
                return True
        return False
    except Exception as e:
        logger.error(f"检查 beat_stats 完成状态失败: {e}")
        return False


def save_to_characters_table(
    task_id: str,
    characters_data: Dict[str, Any],
    raw_data: Optional[Dict[str, Any]] = None,
) -> bool:
    """将角色数据保存到数据库的 characters 表。

    data 存去重后的角色数据，raw 存去重前的原始提取结果（可选）。
    """
    if not characters_data:
        logger.warning("角色数据为空，跳过数据库保存")
        return False

    engine = create_engine(DB_URL)
    try:
        with engine.begin() as conn:
            sql = text("""
                INSERT INTO characters (task_id, data, raw, create_time, update_time)
                VALUES (:tid, :data, :raw, NOW(), NOW()) ON CONFLICT (task_id)
                DO UPDATE SET data = EXCLUDED.data, raw = EXCLUDED.raw, update_time = NOW()
            """)
            conn.execute(sql, {
                "tid": task_id,
                "data": json.dumps(characters_data),
                "raw": json.dumps(raw_data) if raw_data else None,
            })
            logger.info(f"💾 [Characters] 入库成功! TaskID: {task_id}")
            return True
    except Exception as e:
        logger.error(f"❌ [Characters] 入库失败: {e}")
        return False


def find_latest_task_from_db() -> Optional[str]:
    """从数据库查找最新任务ID"""
    try:
        engine = create_engine(DB_URL)
        with engine.connect() as conn:
            sql = text("SELECT task_id FROM story_tree_json ORDER BY id DESC LIMIT 1")
            res = conn.execute(sql).fetchone()
            if res:
                logger.info(f"🔍 从数据库找到最新任务ID: {res[0]}")
                return res[0]
            logger.info("🔍 数据库中未找到任何任务")
            return None
    except Exception as e:
        logger.error(f"❌ 从数据库查找任务失败: {e}")
        return None


# ============================================================
# 主管道
# ============================================================

# ============================================================
# 单步函数：每个函数对应流水线中的一个步骤，返回 True 表示成功。
# run_pipeline 按 stage 组合调用这些步骤，便于单独执行某一阶段。
# ============================================================

def step1_characters(task_id: str, provider: str, model: Optional[str], force: bool) -> bool:
    """Step 1: 角色数据提取与去重"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 2B：角色数据提取与去重")
    print("=" * 60)

    if not force and _count_exists("characters", task_id):
        logger.info("✅ 角色数据已存在，跳过提取和去重步骤")
        return True

    if force:
        logger.info("🔨 强制重新提取模式")
    try:
        logger.info("📂 正在提取角色特征...")
        extractor = StoryTreeCharacterExtractor02(provider=provider, model=model)
        raw_character_data = extractor.extract_to_memory(story_tree_path="", task_id=task_id)
        if not raw_character_data:
            logger.error("❌ 未提取到角色数据")
            return False
        logger.info(f"✅ 提取完成，共 {len(raw_character_data)} 个角色")

        logger.info("🤖 正在合并去重角色数据...")
        deduplicator = StoryTreeLLMDeduplicationAgent(
            characters_json_dir="outputs/characters_json",
            provider=provider,
            model=model,
            cleanup_raw=False,
        )
        merged_characters = deduplicator.merge_in_memory(raw_character_data)
        if not merged_characters:
            logger.error("❌ 合并去重失败")
            return False
        logger.info(f"✅ 合并完成，共 {len(merged_characters)} 个角色")

        logger.info("✂️ 正在精简人名...")
        merged_characters = deduplicator.simplify_names(merged_characters)

        logger.info("💾 正在上传到数据库...")
        if not save_to_characters_table(task_id, merged_characters, raw_data=raw_character_data):
            logger.error("❌ 数据库上传失败")
            return False
        logger.info("✅ 数据库上传成功")

        logger.info("📝 正在生成状态组合描述...")
        combo_count = build_combination_descriptions(task_id)
        logger.info(f"✅ 组合描述生成完成，共 {combo_count} 个")
    except Exception as e:
        logger.error(f"❌ 执行阶段 2B·角色提取失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        _safe_usage_flush()
        return False

    _safe_usage_flush()
    return True


def step2_name_mappings(task_id: str, force: bool) -> bool:
    """Step 2: 生成原名 ID 映射，change_name 留给 Step 5。"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 2B：生成原名 ID 映射（name_mappings）")
    print("=" * 60)

    if not force and _artifact_is_reusable(
        "name_mappings", task_id, validator=_validate_name_mappings_original
    ):
        logger.info("✅ 原名映射已存在且结构完整，跳过生成步骤")
        return True

    try:
        mappings = name_mappings_run(task_id=task_id, force=force)
        original_count = len(
            (mappings.get("original_name") or {}).get("conversion") or {}
        ) if isinstance(mappings, dict) else 0
        if isinstance(mappings, dict):
            logger.info(
                f"✅ 原名映射生成完成，original_name.conversion {original_count} 条，"
                "change_name 已预留为空"
            )
        else:
            logger.warning("⚠️ 原名映射未返回有效结果，继续后续阶段")
    except Exception as e:
        logger.warning(f"⚠️ 执行阶段 2B·原名映射失败，继续后续阶段: {e}")
        import traceback
        logger.warning(traceback.format_exc())

    _safe_usage_flush()
    return True


def step3_plot(task_id: str, force: bool) -> bool:
    """Step 3: 提取剧情 beats 到数据库。

    与其他步骤不同，本步失败必须中断整条流水线：plots 是后续 14 步的根数据，
    缺了它 Step 4~17 只能空转，最终产出一个缺剧情的半成品任务。
    """
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 2B：提取剧情 Beats 到数据库（plot）")
    print("=" * 60)

    if not force and _artifact_is_reusable(
        "plots", task_id, validator=_validate_plots
    ):
        logger.info("✅ 剧情 Beats 数据已存在且结构完整，跳过提取步骤")
        return True

    try:
        asyncio.run(extract_plots_main(target_task_id=task_id))
    except Exception as e:
        logger.error(f"❌ 执行阶段 2B·剧情 Beats 失败，plots 无产出，流程终止: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

    logger.info("✅ 剧情 Beats 提取完成")
    return True


def step4_beat_stats(task_id: str, force: bool) -> bool:
    """Step 4: 统计台词并更新 important_rank。"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 3A：统计人物台词并更新 important_rank")
    print("=" * 60)

    if not force and check_beat_stats_done(task_id):
        logger.info("✅ important_rank 已更新，跳过统计步骤")
        return True

    try:
        beat_stats_run(task_id=task_id, exclude_narrator=True)
        logger.info("✅ important_rank 更新完成")
    except Exception as e:
        logger.warning(
            f"⚠️ 执行阶段 3A 的台词统计失败，后续排名映射将按已有 important_rank 或 rank=1 继续: {e}"
        )
        import traceback
        logger.warning(traceback.format_exc())
    return True


def step5_ranked_name_mappings(task_id: str, force: bool) -> bool:
    """Step 5: 按 important_rank 生成并回填 change_name。"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 3A：按 important_rank 生成角色改名映射")
    print("=" * 60)

    if not force and _artifact_is_reusable(
        "name_mappings", task_id, validator=_validate_name_mappings_changed
    ):
        logger.info("✅ 改名映射已存在且三语显示名完整，跳过生成步骤")
        return True

    try:
        result = ranked_name_mappings_process_task(task_id=task_id, force=force)
        logger.info(
            f"✅ 改名映射生成完成，实际改名 {result.get('renamed_count', 0)} 个角色"
        )
    except Exception as e:
        # 改名是增强步骤。新模块内部已经逐角色降级；连数据源都不可用时也不
        # 应阻断后续流程，NPC 与最终换名步骤会基于当前可用映射继续。
        logger.warning(f"⚠️ 执行阶段 3A·排名改名映射失败，保留空/已有 change_name 并继续: {e}")
        import traceback
        logger.warning(traceback.format_exc())

    _safe_usage_flush()
    return True


def step6_npc_mappings(task_id: str, force: bool = False) -> bool:
    """Step 6: 把剧情中出现的 NPC 角色补进 name_mappings。"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 3A：补全 NPC 人名映射（add_npc_to_name_mappings）")
    print("=" * 60)

    if not force and _npc_mappings_are_complete(task_id):
        logger.info("✅ 剧情角色均已在 name_mappings 中，跳过 NPC 补全步骤")
        return True

    try:
        result = add_npc_to_name_mappings(target_task_id=task_id)
        if result:
            logger.info("✅ NPC 人名映射补全完成")
        else:
            logger.warning("⚠️ NPC 映射未补充新角色，继续后续阶段")
    except Exception as e:
        logger.warning(f"⚠️ 执行阶段 3A·NPC 映射失败，继续后续阶段: {e}")
        import traceback
        logger.warning(traceback.format_exc())

    _safe_usage_flush()
    return True


def step7_character_styles(task_id: str, force: bool) -> bool:
    """Step 7: 从原文提取角色语体表 (character_styles)

    依赖 story_tree_md 原文与 characters 档案，且必须排在两步之后：
      - Step 4 写入 important_rank 后才能按角色重要度区分语体精细度
      - Step 5 生成改名、Step 6 补全 NPC 后 change_name 才是完整角色名单
    产出的语体表供多语言字幕翻译控制敬语与人称。
    """
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 4：角色语体")
    print("=" * 60)

    styles_are_current = False
    if not force:
        # 先做便宜的结构校验：不通过就不必再去读原文算哈希。
        if not _artifact_is_reusable(
            "character_styles", task_id, validator=_validate_character_styles
        ):
            styles_are_current = False
        else:
            try:
                styles_are_current = saved_character_styles_are_current(task_id)
            except Exception as e:
                logger.warning(
                    f"⚠️ 检查已有角色语体表失败，将按需要重新提取且不阻断流程: {e}"
                )

    if styles_are_current:
        logger.info("✅ 角色语体表完整且与当前原文/名称映射一致，跳过提取步骤")
    else:
        try:
            # 语体表用于字幕翻译时控制敬语与人称，缺失只是降低翻译精细度，
            # 不足以让整条流水线报废，因此失败只告警不终止。
            if not asyncio.run(extract_character_styles_main(target_task_id=task_id)):
                logger.warning("⚠️ extract_character_styles 未产出结果（不影响后续步骤）")
            else:
                logger.info("✅ 角色语体表提取完成")
        except Exception as e:
            logger.warning(f"⚠️ 执行阶段 4·角色语体失败，将缺少角色语体表: {e}")
            import traceback
            logger.warning(traceback.format_exc())

    _safe_usage_flush()
    return True


def step8_assign_combinations(task_id: str, provider: str, force: bool) -> bool:
    """Step 8: 为 beats 分配人物状态组合"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 5：为 beats 分配人物状态组合")
    print("=" * 60)

    try:
        if not force:
            status = get_assignment_status(task_id)
            if status["complete"]:
                logger.info(
                    f"✅ 执行阶段 5 已完整执行，跳过："
                    f"{status['assigned']}/{status['total']} 个可见 beat 已有合法组合，"
                    f"实际启用 {status['active']} 个状态组合"
                )
                return True
            logger.info(
                f"执行阶段 5 检查：可见 beat={status['total']}，"
                f"missing={status['missing']} invalid={status['invalid']} "
                f"unmapped={status['unmapped']} "
                f"non_visual={status['non_visual']} "
                f"is_active_mismatch={status['activity_mismatches']}，需要执行"
            )
        assigned_count = assign_combinations_run(task_id=task_id, provider=provider, force=force)
        logger.info(f"✅ 状态组合分配完成，共分配 {assigned_count} 个 beats")
    except Exception as e:
        logger.error(f"❌ 执行阶段 5·人物状态组合失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False
    return True


def step9_char_prompts(task_id: str, force: bool) -> bool:
    """Step 9: 构造人物文生图提示词"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 6A：构造人物文生图提示词")
    print("=" * 60)

    # 是否跳过/重建交给 build_and_save_prompts 内部的覆盖检查决定：
    # 仅凭 character_prompts 行是否存在无法发现“状态组合已更新但提示词还是旧
    # 组合”的错位，会把过期快照带到 char_designer 预检才报错。builder 会校验
    # active + plots 组合覆盖度，缺失则自动重建。
    try:
        prompts = build_and_save_prompts(task_id, force=force)
        logger.info(f"✅ 提示词构造完成/复用，共 {len(prompts)} 个人物")
    except Exception as e:
        logger.error(f"❌ 执行阶段 6A·人物提示词失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        _safe_usage_flush()
        return False

    _safe_usage_flush()
    return True


def step10_char_position(task_id: str, force: bool) -> bool:
    """Step 10: 提取多角色站位 (character_position)

    依赖 plots（Step 3 的 beats）与 characters（Step 6 的 important_rank），
    串联规则切段 + 阵营扫描 + 段级落位，产出 display_windows 入库 character_position。
    """
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 6B：多角色站位")
    print("=" * 60)

    # character_position 在业务库；空 {} 是合法产物（本书确实没有多人同屏段）。
    if not force and _artifact_is_reusable(
        "character_position",
        task_id,
        validator=_validate_character_position,
        database_url=DATABASE_URL,
    ):
        logger.info("✅ 多角色站位数据已存在且结构完整，跳过提取步骤")
    else:
        try:
            result = asyncio.run(extract_char_position_main(target_task_id=task_id))
            if result:
                logger.info("✅ 多角色站位提取完成")
            else:
                logger.warning("⚠️ 执行阶段 6B 未产出多角色站位，继续后续阶段")
        except Exception as e:
            # 站位是增强信息而非必需：缺失时第 17 步按单人格式输出该 beat，
            # 因此这里只告警不终止流程。
            logger.warning(f"⚠️ 执行阶段 6B 失败，将缺少多角色站位数据（不影响后续阶段）: {e}")
            import traceback
            logger.warning(traceback.format_exc())

    _safe_usage_flush()
    return True


def step11_story_tree(task_id: str, force: bool) -> bool:
    """Step 11: 更新故事树（书名/简介/章节标题）"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 1：故事树元数据（书名/简介/章节标题）")
    print("=" * 60)

    if not force and _story_tree_titled(task_id):
        logger.info("✅ 故事树已生成书名，跳过 LLM 重算，仅从数据库导出文件")
        try:
            StoryTreeUpdater().export_story_tree(task_id)
            logger.info("✅ 故事树文件已导出")
        except Exception as e:
            logger.error(f"❌ 执行阶段 1·故事树导出失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    else:
        try:
            StoryTreeUpdater().update_story_tree(task_id)
            logger.info("✅ 故事树更新完成")
        except Exception as e:
            logger.error(f"❌ 执行阶段 1·故事树元数据失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    _safe_usage_flush()
    return True


async def _run_scene_prepare_async(task_id: str, force: bool) -> bool:
    """运行 Step 12 的前置长链：extract_raw -> extract_merge。

    这两个阶段只依赖故事树和原文，不依赖 plots，因此在完整流程开始时即可
    启动。它们被单独拆出后，主流程可以让它与 Step 1~3 并行执行。
    """
    # 保持原 Step 12 的复用判定：已有 scenes 行时跳过整个场景提取链。
    if not force and _count_exists("scenes", task_id):
        logger.info("✅ 执行阶段 2A·场景提取与归并数据已存在，跳过")
        return True

    logger.info("  ▶ 场景提取：提取原始场景 (extract_raw) -> scenes.raw")
    scenes = await scene_extract_raw_main(task_id)
    if not scenes:
        logger.error("❌ extract_raw 未提取到场景")
        return False
    logger.info(f"  ✅ 原始场景 {len(scenes)} 条已写入 scenes.raw")

    logger.info("  ▶ 场景归并：从 scenes.raw 读取并写入 scenes.data (extract_merge)")
    locations = await scene_extract_merge_main(task_id)
    if not locations:
        logger.error("❌ extract_merge 归并失败")
        return False
    logger.info(f"  ✅ 归并为 {len(locations)} 个场景点")
    return True


async def _run_scene_align_async(task_id: str) -> bool:
    """运行 Step 12.3：将 plots beats 对齐到已归并的 scenes。"""
    logger.info("  ▶ 场景对齐：将 beats 关联到场景 (scene_to_plot)")
    await scene_to_plot_main(task_id)
    return True


async def _run_scene_prompt_async(task_id: str) -> bool:
    """运行 Step 12.4：从 scenes 生成场景生图提示词。"""
    logger.info("  ▶ 场景提示词：生成场景生图提示词 (scene_prompts)")
    await scene_prompts_main(task_id)
    return True


def step12_prepare(task_id: str, force: bool) -> bool:
    """Step 12 前置部分：只执行 12.1~12.2。"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 2A：场景提取与归并")
    print("=" * 60)
    try:
        ok = asyncio.run(_run_scene_prepare_async(task_id, force))
        if not ok:
            return False
        _safe_usage_flush()
        return True
    except Exception as e:
        logger.error(f"❌ 执行阶段 2A·场景提取与归并失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        _safe_usage_flush()
        return False


def step12_align(task_id: str) -> bool:
    """Step 12.3：场景与剧情 Beats 对齐。"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 3B：场景对齐（beats → scene）")
    print("=" * 60)
    try:
        asyncio.run(_run_scene_align_async(task_id))
        _safe_usage_flush()
        return True
    except Exception as e:
        logger.error(f"❌ 执行阶段 3B·场景对齐失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        _safe_usage_flush()
        return False


def step12_prompts(task_id: str) -> bool:
    """Step 12.4：生成场景生图提示词。"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 6C：场景提示词")
    print("=" * 60)
    try:
        asyncio.run(_run_scene_prompt_async(task_id))
        _safe_usage_flush()
        return True
    except Exception as e:
        logger.error(f"❌ 执行阶段 6C·场景提示词失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        _safe_usage_flush()
        return False


def step12_scenes(task_id: str, force: bool) -> bool:
    """Step 12：场景提取与生图提示词生成（兼容的完整 4 阶段入口）。

    extract_raw -> extract_merge -> scene_to_plot -> scene_prompts
    完整流程会调用拆分后的 step12_prepare/step12_align/step12_prompts，
    以便将前两段长耗时任务提前并行；单独 --stage scene 仍保持原有语义。
    """
    print("\n" + "=" * 60)
    logger.info("🔹 独立场景阶段：场景提取、对齐与提示词")
    print("=" * 60)

    if not force and _count_exists("scenes", task_id):
        logger.info("✅ 场景数据已存在，跳过生成步骤")
        return True

    try:
        ok = asyncio.run(_run_scene_prepare_async(task_id, force))
        if ok:
            ok = asyncio.run(_run_scene_align_async(task_id))
        if ok:
            ok = asyncio.run(_run_scene_prompt_async(task_id))
        if not ok:
            return False
        logger.info("✅ 场景提示词生成完成")
    except Exception as e:
        logger.error(f"❌ 独立场景阶段执行失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

    _safe_usage_flush()
    return True


def step12_scene_force(task_id: str, max_retries: int = 3) -> bool:
    """专门提取场景：强制重新跑整条场景链，重试直到 scene_prompts 表写入成功。

    与 step12_scenes 的区别：
      - 始终 force（忽略 scenes 已存在的跳过判断），保证重新提取；
      - 成功判据不是链条无异常，而是 scene_prompts 表确实写入了当前 task 的数据；
      - 未写入则重试，最多 max_retries 次。
    """
    print("\n" + "=" * 60)
    logger.info(f"🔹 独立场景阶段（强制模式）：重试直到 scene_prompts 写入（最多 {max_retries} 次）")
    print("=" * 60)

    for attempt in range(1, max_retries + 1):
        logger.info(f"🔁 独立场景阶段强制提取尝试 {attempt}/{max_retries}")
        ok = step12_scenes(task_id, force=True)
        if not ok:
            logger.warning(f"⚠️ 第 {attempt} 次独立场景阶段执行失败，准备重试")
            continue
        if _count_exists("scene_prompts", task_id):
            logger.info(f"✅ scene_prompts 已写入数据库 (第 {attempt} 次尝试成功)")
            return True
        logger.warning(f"⚠️ 第 {attempt} 次独立场景阶段完成但 scene_prompts 表仍无数据，准备重试")

    logger.error(f"❌ 已重试 {max_retries} 次，scene_prompts 仍未写入数据库")
    return False


def step14_cover(task_id: str, force: bool) -> bool:
    """Step 14: 提取封面视觉元素并生成封面提示词"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 2C：封面素材")
    print("=" * 60)

    if not force and _count_exists("backgrounds", task_id):
        logger.info("✅ 封面数据已存在，跳过提取步骤")
    else:
        api_key = (
            os.getenv("GPT_IMAGE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        if not api_key:
            logger.error("❌ 未设置 GPT_IMAGE_API_KEY（或 OPENAI_API_KEY），无法运行封面提取")
            return False
        try:
            extractor = CoverExtractorFromDB(api_key)
            backgrounds_data = extractor.extract(task_id)
            if not backgrounds_data:
                logger.error("❌ 封面数据提取失败")
                return False
            logger.info(f"✅ 封面提取完成，共 {len(backgrounds_data)} 个节点")
        except Exception as e:
            logger.error(f"❌ 执行阶段 2C·封面素材失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    _safe_usage_flush()
    return True


def step13_cg(task_id: str, force: bool) -> bool:
    """Step 13: 按 plots.cg 区间构建 cg 表并生成全部文字字段。"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 6D：CG 文字信息")
    print("=" * 60)

    if not force and cg_data_ready(task_id):
        logger.info("✅ CG 文字信息已完成，跳过构建与提示词生成")
        return True
    try:
        count = cg_build_run(task_id=task_id)
        if count <= 0:
            logger.info("plots 中没有 CG 区间，跳过 CG 提示词生成")
            return True
        asyncio.run(cg_prompt_main(task_id))
        logger.info(f"✅ CG 文字信息生成完成，共 {count} 条")
        return True
    except Exception as e:
        logger.error(f"❌ 执行阶段 6D·CG 文字信息失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def step15_sound(task_id: str, force: bool = False) -> bool:
    """Step 15: 提取 BGM/SFX 描述到数据库（atmosphere 之前）"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 6E：声音描述")
    print("=" * 60)

    try:
        result = asyncio.run(
            sound_extract_process_task(task_id=task_id, force=force)
        )
        if result != task_id:
            raise SoundExtractionIncompleteError(
                f"Step15 返回异常 task_id: {result!r}，期望 {task_id!r}"
            )
        logger.info("✅ 声音描述提取完成")
        return True
    except (StageLeaseLostError, SoundCheckpointPersistenceError):
        # 无法确认当前执行权或最终检查点时必须向上抛出，不能让旧执行继续发布。
        raise
    except (
        SoundInputUnavailableError,
        SoundExtractionIncompleteError,
        SoundPersistenceError,
    ) as exc:
        logger.error(
            "❌ 步骤15业务失败，后续步骤停止: %s",
            exc,
            exc_info=True,
        )
        return False
    finally:
        _safe_usage_flush()


# def step_atmosphere(task_id: str, force: bool) -> bool:
#     """提取氛围特效（当前未启用）"""
#     print("\n" + "=" * 60)
#     logger.info("🔹 提取氛围特效 (atmosphere)")
#     print("=" * 60)
#
#     if not force and _count_exists("atmosphere", task_id):
#         logger.info("✅ 氛围特效数据已存在，跳过提取步骤")
#     else:
#         try:
#             asyncio.run(atmosphere_process_task(task_id=task_id))
#             logger.info("✅ 氛围特效提取完成")
#         except Exception as e:
#             logger.error(f"❌ 氛围特效步骤执行失败: {e}")
#             import traceback
#             logger.error(traceback.format_exc())
#             return False
#
#     if _usage_tracker:
#         _usage_tracker.flush()
#     return True


_NAME_ANONYMIZER_CHANGED = False


def step16_name_anonymizer(task_id: str, force: bool) -> bool:
    """Step 16: 对前 15 步产生的全部相关产物统一换名。"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 7：统一换名")
    print("=" * 60)

    # 无论 force 与否都执行，以 name_mappings 为唯一事实来源，对全部产物
    # 做同一遍主名、别称、正文提及和 speaker_id 替换。
    _ = force
    global _NAME_ANONYMIZER_CHANGED
    try:
        result = name_anonymizer_process_task(
            task_id=task_id,
            outputs_path=OUTPUTS_PATH,
            logger=logger,
        )
        _NAME_ANONYMIZER_CHANGED = bool(result.get("changed"))
        logger.info(
            f"✅ 全面换名完成，替换规则 {result.get('replacement_count', 0)} 条，"
            f"更新产物: {', '.join(result.get('changed_artifacts') or []) or '无'}"
        )
    except Exception as e:
        _NAME_ANONYMIZER_CHANGED = False
        logger.error(
            f"⚠️ 执行阶段 7 异常，已跳过剩余换名但不阻断流程，"
            f"将继续执行阶段 8: {e}"
        )
        import traceback
        logger.error(traceback.format_exc())
        return True

    return True


def step17_ultimate(task_id: str, force: bool) -> bool:
    """Step 17: 整合 plots + sound 为 ultimate 数据（最后一步）"""
    print("\n" + "=" * 60)
    logger.info("🔹 执行阶段 8：最终聚合")
    print("=" * 60)

    if (
        not force
        and not _NAME_ANONYMIZER_CHANGED
        and _count_exists("ultimate", task_id, database_url=DATABASE_URL)
    ):
        logger.info("✅ ultimate 数据已存在，跳过整合步骤")
    else:
        try:
            if _NAME_ANONYMIZER_CHANGED and not force:
                logger.info("🔄 统一换名产物已变更，将重建 ultimate")
            db_saved, local_saved = process_ultimate(task_id)
            if db_saved:
                logger.info("✅ ultimate 数据整合完成")
            elif local_saved:
                # 本地 {task_id}.json 是 game_builder/api_gateway 的首选读取来源，
                # 它写成功就意味着下游仍能拿到完整产物，不必让整条流水线报废。
                logger.warning(
                    "⚠️ ultimate 未写入业务库，已降级为本地文件产物，流程继续"
                )
            else:
                logger.error("❌ ultimate 既未入库也未落盘，下游将缺少总调度产物")
        except Exception as e:
            logger.error(f"❌ 执行阶段 8·最终聚合异常，已跳过 ultimate 整合但不阻断流程: {e}")
            import traceback
            logger.error(traceback.format_exc())

    _safe_usage_flush()
    return True


# stage -> 需要执行的步骤序列。每个步骤是 (名称, 调用函数) 二元组。
# 调用函数统一接收关键字参数 task_id/provider/model/force，各自取用所需。
def _build_stage_steps(stage: str, provider: str, model: Optional[str], force: bool,
                       scene_retries: int = 3, start_step: Optional[int] = None):
    def _s1(tid):    return step1_characters(tid, provider, model, force)
    def _s2(tid):    return step2_name_mappings(tid, force)
    def _s3(tid):    return step3_plot(tid, force)
    def _s4(tid):    return step4_beat_stats(tid, force)
    def _s5(tid):    return step5_ranked_name_mappings(tid, force)
    def _s6(tid):    return step6_npc_mappings(tid, force)
    def _s7(tid):    return step7_character_styles(tid, force)
    def _s8(tid):    return step8_assign_combinations(tid, provider, force)
    def _s9(tid):    return step9_char_prompts(tid, force)
    def _s10(tid):   return step10_char_position(tid, force)
    def _s11(tid):   return step11_story_tree(tid, force)
    def _s12(tid):   return step12_scenes(tid, force)
    def _s12f(tid):  return step12_scene_force(tid, max_retries=scene_retries)
    def _s13(tid):   return step13_cg(tid, force)
    def _s14(tid):   return step14_cover(tid, force)
    def _s15(tid):   return step15_sound(tid, force)
    # def _satm(tid):  return step_atmosphere(tid, force)
    def _s16(tid):   return step16_name_anonymizer(tid, force)
    def _s17(tid):   return step17_ultimate(tid, force)

    full = [
        ("角色数据提取与去重", _s1), ("原名映射", _s2), ("剧情基础数据", _s3),
        ("台词统计", _s4), ("排名与改名映射", _s5), ("NPC 映射", _s6),
        ("角色语体", _s7), ("人物状态组合", _s8), ("人物提示词", _s9),
        ("多角色站位", _s10), ("故事树元数据", _s11), ("场景提取与归并", _s12),
        ("CG 文字信息", _s13), ("封面素材", _s14),
        # ("Atmosphere", _satm),
        ("声音描述", _s15), ("统一换名", _s16), ("最终聚合", _s17),
    ]
    # plot 阶段：beats -> important_rank -> 改名 -> NPC -> 语体表 -> 状态组合。
    # 前置的 characters / name_mappings 需已由完整流程或单独步骤生成。
    plot = [
        ("剧情基础数据", _s3), ("台词统计", _s4), ("排名与改名映射", _s5),
        ("NPC 映射", _s6), ("角色语体", _s7), ("人物状态组合", _s8),
    ]
    # scene 阶段：仅执行完整场景处理链。
    scene = [
        ("场景提取与归并、对齐及提示词", _s12),
    ]
    # scene-force 阶段：强制重新提取场景，重试直到 scene_prompts 表写入成功。
    scene_force = [
        ("场景强制提取（重试模式）", _s12f),
    ]
    cg = [("CG 文字信息", _s13)]
    selected = {"all": full, "plot": plot, "scene": scene,
                "scene-force": scene_force, "cg": cg}[stage]
    # --start-step N：仅对完整流程有效，从兼容断点 N 切片跑到结尾（full 里索引 = N - 1）。
    if start_step is not None:
        if stage != "all":
            raise ValueError("--start-step 仅在 --stage all（默认）时可用")
        if not 1 <= start_step <= len(full):
            raise ValueError(f"--start-step 超出范围: 应为 1~{len(full)}")
        selected = full[start_step - 1:]
    return selected


def _run_step_sequence(task_id: str, steps) -> bool:
    """在线程中执行一条有明确依赖的同步步骤链。"""
    for name, fn in steps:
        logger.info("▶️ [执行阶段] 开始 %s", name)
        try:
            ok = fn(task_id)
        except Exception as exc:
            logger.error("❌ [执行阶段] %s 异常: %s", name, exc)
            import traceback
            logger.error(traceback.format_exc())
            return False
        if not ok:
            logger.error("❌ [执行阶段] %s 执行失败", name)
            return False
        logger.info("✅ [执行阶段] 完成 %s", name)
    return True


def _run_pipeline_parallel(
    task_id: str,
    provider: str,
    model: Optional[str],
    force: bool,
) -> bool:
    """运行完整流程的依赖并行版本。

    调度原则：
      1. 先完成 Step 11，再同时启动 Step 12.1~12.2 和 Step 1~3；这样最长的
         场景长链从 Step 11 完成后立即开始，同时避免 Step 11 与角色/剧情基础链
         读取故事树时互相影响。
      2. Step 12.3 等待 plots (Step 3) 和 scenes.data (Step 12.2)。
      3. Step 12.3 与 Step 8 都会整体更新 plots，二者严格串行。
      4. Step 5/6 会更新同一条 name_mappings，严格串行；Step 7 可在 Step 6
         后独立运行，但为避免读取 characters 时撞上 Step 8 的整行更新，仍让
         Step 8 等 Step 7 完成。
      5. Step 16/17 是最终汇合点，任何产物步骤未完成都不能提前执行。

    各步骤仍使用原有函数和幂等检查；线程只负责重叠等待时间，不改变单步语义。
    """
    # 线程数固定且很小：主流程只有两个长链并行，后续最多 5 个独立产物。
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="info-pipeline") as pool:
        # 执行阶段 1 是场景长链的前置步骤。先同步完成它，再同时释放
        # 执行阶段 2A 和执行阶段 2B。这样符合“场景链是最长步骤，
        # 但必须使用 Step 11 更新后的故事树”的调度要求。
        if not step11_story_tree(task_id, force):
            logger.error("❌ 执行阶段 1·故事树元数据失败，流程终止")
            return False

        scene_prepare_future = pool.submit(step12_prepare, task_id, force)

        # 基础链必须保持 1 -> 2 -> 3。
        base_future = pool.submit(
            _run_step_sequence,
            task_id,
            [
                ("执行阶段 2B·角色数据提取与去重", lambda tid: step1_characters(tid, provider, model, force)),
                ("执行阶段 2B·原名映射", lambda tid: step2_name_mappings(tid, force)),
                ("执行阶段 2B·剧情基础数据", lambda tid: step3_plot(tid, force)),
            ],
        )

        # 执行阶段 4B 只依赖执行阶段 1，因此与阶段 2A、阶段 2B 并行，
        # 不被场景归并拖住。
        cover_future = pool.submit(step14_cover, task_id, force)

        # Step 3 是后续剧情链的根；失败时不要让其他分支继续制造半成品。
        if not base_future.result():
            logger.error("❌ 执行阶段 2B·角色与剧情基础链失败，流程终止")
            return False

        # Step 4~6 改写 characters/name_mappings，按依赖严格串行。
        plot_rank_future = pool.submit(
            _run_step_sequence,
            task_id,
            [
                ("执行阶段 3A·台词统计", lambda tid: step4_beat_stats(tid, force)),
                ("执行阶段 3A·排名与改名映射", lambda tid: step5_ranked_name_mappings(tid, force)),
                ("执行阶段 3A·NPC 映射", lambda tid: step6_npc_mappings(tid, force)),
            ],
        )

        # Step 12.3 会整体更新 plots，必须等 Step 3 完成（已在上面确认）。
        # 它还必须等场景归并写入 scenes.data。
        if not scene_prepare_future.result():
            logger.error("❌ 执行阶段 2A·场景提取与归并失败，无法进入执行阶段 3B")
            return False
        if not step12_align(task_id):
            logger.error("❌ 执行阶段 3B·场景对齐失败，流程终止")
            return False

        # Step 7 只读已完成的排名/映射；Step 8 会整体更新 characters 和 plots，
        # 因此先完成 Step 7，再执行 Step 8，避免整行 JSON 读写竞态。
        if not plot_rank_future.result():
            logger.error("❌ 执行阶段 3A·排名与映射链失败，无法继续")
            return False
        if not step7_character_styles(task_id, force):
            logger.error("❌ 执行阶段 4·角色语体失败，流程终止")
            return False
        if not step8_assign_combinations(task_id, provider, force):
            logger.error("❌ 执行阶段 5·人物状态组合失败，流程终止")
            return False

        # Step 12.4 只读取 scenes.data，12.3 完成后即可与其它产物并行。
        # Step 13 需要 Step 8 写入的 speaker_combination；Step 15 需要 12.3
        # 写入的真实 scene_id；Step 9/10 为了避免共享 JSON 读写竞态放在 Step 8 后。
        downstream = {
            "执行阶段 6A·人物提示词": pool.submit(step9_char_prompts, task_id, force),
            "执行阶段 6B·多角色站位": pool.submit(step10_char_position, task_id, force),
            "执行阶段 6C·场景提示词": pool.submit(step12_prompts, task_id),
            "执行阶段 6D·CG 文字信息": pool.submit(step13_cg, task_id, force),
            "执行阶段 6E·声音描述": pool.submit(step15_sound, task_id, force),
        }

        # 执行阶段 2C 已在执行阶段 1 后启动；等待所有独立产物完成。
        results = {name: future.result() for name, future in downstream.items()}
        results["执行阶段 2C·封面素材"] = cover_future.result()
        failed = [name for name, ok in results.items() if not ok]
        if failed:
            logger.error("❌ 以下并行产物步骤失败: %s", ", ".join(failed))
            return False

        # 最终换名和聚合必须在所有产物写完后执行，且二者不能并行。
        if not step16_name_anonymizer(task_id, force):
            return False
        if not step17_ultimate(task_id, force):
            return False
    return True


def run_pipeline(
    task_id: Optional[str] = None,
    provider: str = "qwen",
    model: Optional[str] = None,
    force: bool = False,
    stage: str = "all",
    scene_retries: int = 3,
    start_step: Optional[int] = None,
) -> Optional[str]:
    """
    运行信息提取管道。

    Args:
        task_id: 任务ID，为 None 时自动查找最新任务
        provider: LLM 提供商
        model: 模型名称
        force: 是否强制重新处理，即使数据已存在
        stage: 执行阶段
            - "all":   完整流程 (character -> plot -> scene -> sound -> aggregation)
            - "plot":  仅第 3~8 步 (beats + 排名 + 改名 + NPC + 语体表 + 状态组合)
            - "scene": 仅场景提示词提取 (Step 12 -> scenes 表)
            - "scene-force": 强制重新提取场景，重试直到 scene_prompts 表写入成功
            - "cg":    仅构建 cg 表并生成文字提示词 (Step 13)
        scene_retries: scene-force 阶段的最大重试次数

    Returns:
        成功返回 task_id，失败返回 None
    """
    pipeline_start = time.time()

    stage_label = {
        "all": "完整流程（依赖并行调度）",
        "plot": "剧情处理阶段",
        "scene": "场景处理阶段",
        "scene-force": "场景强制提取阶段（重试直到入库）",
        "cg": "CG 文字信息阶段",
    }.get(stage, stage)

    print("\n" + "#" * 60)
    logger.info("🧩 [Pipeline] 启动信息提取统一流水线")
    logger.info(f"🔧 [Mode] {'指定任务模式: ' + task_id if task_id else '自动最新任务模式'}")
    logger.info(f"🎯 [Stage] {stage_label}")
    print("#" * 60)

    if task_id is None:
        task_id = find_latest_task_from_db()
        if task_id is None:
            logger.error("未找到任何任务，流程终止")
            return None

    logger.info(f"🌟 [Context] 当前流水线 Task ID: {task_id}")

    _safe_usage_set_context(
        task_id, "info_extractor", force_new_run=True
    )

    # 先构建步骤列表：除了用于断点/阶段执行，也允许测试或集成方注入自定义
    # 步骤列表。只有默认的完整 17 步列表才走并行调度，自定义列表保持原有
    # 串行语义，避免调试钩子被新的调度器绕过。
    steps = _build_stage_steps(stage, provider, model, force, scene_retries, start_step)

    # 完整流程使用依赖并行编排。断点续跑和其它 stage 保留原有串行调度，
    # 这样 --start-step 的历史语义不变，也避免用户从任意中间步骤启动时
    # 误以为缺失的上游依赖会被自动补齐。
    default_full_step_names = [
        "角色数据提取与去重", "原名映射", "剧情基础数据", "台词统计",
        "排名与改名映射", "NPC 映射", "角色语体", "人物状态组合",
        "人物提示词", "多角色站位", "故事树元数据", "场景提取与归并",
        "CG 文字信息", "封面素材", "声音描述", "统一换名", "最终聚合",
    ]
    is_default_full_schedule = [name for name, _ in steps] == default_full_step_names
    if stage == "all" and start_step is None and is_default_full_schedule:
        logger.info(
            "🧵 [Schedule] 启用依赖并行：故事树元数据完成后，并行启动"
            "场景提取与归并、角色与剧情基础链，在场景对齐阶段汇合"
        )
        if not _run_pipeline_parallel(
            task_id=task_id,
            provider=provider,
            model=model,
            force=force,
        ):
            logger.error("❌ 并行流水线执行失败")
            return None

        duration = time.time() - pipeline_start
        logger.info("\n" + "#" * 60)
        logger.info("🎉 [Success] 信息提取流水线执行完毕！")
        logger.info(f"🎯 [Stage] {stage_label}")
        logger.info(f"⏱️  总耗时: {duration:.2f} 秒")
        logger.info(f"📂 产物路径: {OUTPUTS_PATH}/{task_id}/")
        logger.info("#" * 60)
        return task_id

    if stage == "all" and start_step is None and not is_default_full_schedule:
        logger.info("🧪 检测到自定义完整步骤列表，使用串行调度")
    if start_step is not None:
        logger.info(f"⏩ [Start] 从指定断点开始执行，共 {len(steps)} 个阶段")
    for name, fn in steps:
        if not fn(task_id):
            logger.error(f"❌ {name} 执行失败，流程终止")
            return None

    duration = time.time() - pipeline_start
    logger.info("\n" + "#" * 60)
    logger.info("🎉 [Success] 信息提取流水线执行完毕！")
    logger.info(f"🎯 [Stage] {stage_label}")
    logger.info(f"⏱️  总耗时: {duration:.2f} 秒")
    logger.info(f"📂 产物路径: {OUTPUTS_PATH}/{task_id}/")
    logger.info("#" * 60)

    return task_id


def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(
        description="信息提取器 - 统一流水线：故事树元数据 -> 场景/剧情基础链并行 -> 下游素材并行 -> 统一换名 -> 最终聚合",
    )
    parser.add_argument("--task-id", default=None, help="任务ID（如果不提供则自动查找最新任务）")
    parser.add_argument("--provider", choices=["openai", "claude", "qwen"], default="qwen",
                        help="LLM提供商 (默认: qwen)")
    parser.add_argument("--model", default=None, help="模型名称（默认根据提供商选择）")
    parser.add_argument("--stage", choices=["all", "plot", "scene", "scene-force", "cg"], default="all",
                        help="执行阶段: all=完整流程; "
                             "plot=仅剧情处理阶段(beats+排名+改名+NPC+语体表+状态组合); "
                             "scene=仅场景处理阶段; scene-force=强制提取场景并重试直到scene_prompts入库; "
                             "cg=仅CG文字信息 (默认: all)")
    parser.add_argument("--force", action="store_true", help="强制重新处理，即使数据已存在")
    parser.add_argument("--scene-retries", type=int, default=3,
                        help="场景强制提取(scene-force)的最大重试次数 (默认: 3)")
    parser.add_argument("--start-step", type=int, default=None,
                        help="从兼容断点 N 开始执行到结尾（仅 --stage all 时有效，配合 --force 覆盖重跑）")
    parser.add_argument("--verbose", action="store_true", help="显示详细日志")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        result_task_id = run_pipeline(
            task_id=args.task_id,
            provider=args.provider,
            model=args.model,
            force=args.force,
            stage=args.stage,
            scene_retries=args.scene_retries,
            start_step=args.start_step,
        )
        if result_task_id:
            logger.info("\n✅ 流水线执行成功!")
            sys.exit(0)
        logger.info("\n❌ 流水线执行失败")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("\n🛑 用户手动中止流程")
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ 流水线执行异常: {e}")
        import traceback
        logger.error(traceback.format_exc())
        logger.error(f"\n❌ 流水线执行异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
