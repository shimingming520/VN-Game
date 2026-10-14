#!/usr/bin/env python3
"""
角色重绘器 - 支持多状态变体的单角色重绘

【层级】工具层，只管画图（GPT/qwen 调用 + 透明合同校验，落到本地 OUTPUTS_PATH）。
        命令行调试用；也作为 tasks.py 里 redraw_character celery 任务的实现。
        不做重试/版本化/OSS 上传/DB 记账。
        提示词只从 character_prompts 表读（由 info_extractor 侧的 char_prompt_builder 事先落库），
        本模块不再自造提示词——信息提取与生图已分离。

文件命名格式: {SpeakerID}_{p}-{b}-{c}_{emotion}_{shot}.png（shot 景别默认 LS）
  例如: TangSan_1-2-3_calm_LS.png / TangSan_1-2-3_happy_LS.png

# 场景1a: 只重绘基础组合（is_active=True 中数字最小）的 calm 图（默认行为）
python novel-game-factory/services/char_designer/core/redraw_character.py --task-id <tid> --char TangSan

# 场景1b: 重绘基础组合之外的所有变体 calm + 所有情绪图（基础 calm 保持不变）
python redraw_character.py --task-id <tid> --char TangSan --mode all_except_base_calm

# 场景1c: 从任务级角色 Anchor 重新生成基础图，再生成全部变体和情绪（全链路图生图；已有图时加 --force）
python redraw_character.py --task-id <tid> --char TangSan --mode all_from_anchor

# 使用 --force 忽略已有本地图片，强制重新生成并覆盖
python redraw_character.py --task-id <tid> --char TangSan --mode all_from_anchor --force

# 默认复用已有有效图片，只补生成缺失图片；--force 时只清理当前模式会
# 重新生成的 LS 及其派生文件，再按 LS → 扩展画布 → MS/CS 的顺序执行。

# 所有模式都会把当前组合、情绪、景别和输出路径实时打印到 stderr，便于在终端观察进度。
# 所有 LS 生图结束后，会按 character_prompts.height 追加顶部透明画布，再裁切 MS/CS。

# 场景2a: 只重绘指定变体的 calm 图（--char 可省略，从 combination 自动解析）
python redraw_character.py --task-id <tid> --combination TangSan-1-2-3

# 场景2b: 重绘指定变体的所有情绪图（以该变体 calm 为基础，calm 本身不重新生成）
python redraw_character.py --task-id <tid> --combination TangSan-1-2-3 --mode variant_emotions

# 场景3a: 只重绘指定变体的某几个情绪图
python redraw_character.py --task-id <tid> --combination TangSan-1-2-3 --mode emotions_only --emotions happy,sad

# 场景3b: 重绘指定变体的所有情绪图（等同于 variant_emotions，但走 emotions_only 路径）
python redraw_character.py --task-id <tid> --combination TangSan-1-2-3 --mode emotions_only

# 使用 GPT Image provider 重绘
python redraw_character.py --task-id <tid> --char TangSan --image-provider gpt

# 指定景别：LS 为 AI 出图源，MS/CS 由 LS 裁切派生（默认 LS,MS,CS 全出）
python redraw_character.py --task-id <tid> --char TangSan --shots LS,MS,CS
python redraw_character.py --task-id <tid> --char TangSan --shots LS   # 只出远景，不裁切
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

current_dir = Path(__file__).resolve().parent
char_designer_path = current_dir.parent
workspace_root = char_designer_path.parent
project_root_path = workspace_root.parent
for path in (str(workspace_root), str(char_designer_path), str(current_dir), str(project_root_path)):
    if path not in sys.path:
        sys.path.insert(0, path)


def add_common_config_search_paths() -> None:
    # 本仓库自带 common/config.py 时，直接用它并返回，绝不去扫描其它仓库副本。
    # 否则 glob 命中的外部仓库 config.py 会被 insert(0) 抢占 sys.path，
    # 而部分旧副本的 DATABASE_URL 未对密码里的 '@' 做 %40 编码，导致连库失败。
    if (project_root_path / "common" / "config.py").exists():
        candidate_str = str(project_root_path)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)
        return

    candidates = [
        workspace_root,
        workspace_root.parent,
        workspace_root.parent / "novel-game-factory",
        workspace_root.parent / "sj_novel" / "novel-game-factory",
    ]
    search_roots = [workspace_root.parent, Path.home() / "Documents" / "code", Path.home() / "Documents" / "gitlab"]
    for search_root in search_roots:
        if search_root.exists():
            candidates.extend(path.parent.parent for path in search_root.glob("*/novel-game-factory/common/config.py"))
            candidates.extend(path.parent.parent for path in search_root.glob("*/*/novel-game-factory/common/config.py"))

    for candidate in candidates:
        if (candidate / "common" / "config.py").exists():
            candidate_str = str(candidate)
            if candidate_str not in sys.path:
                sys.path.insert(0, candidate_str)


add_common_config_search_paths()

os.environ.setdefault("DB_HOST", "172.16.50.17")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_NAME", "extreme")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "change_me")

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=project_root_path / "novel-game-factory" / ".env")
    load_dotenv(dotenv_path=workspace_root / ".env")
except ImportError:
    pass

from common.config import DATABASE_URL, OUTPUTS_PATH, PLATFORM_DATABASE_URL
from common.name_mapping_utils import (
    get_change_name_section,
    get_original_conversion,
)
from common.visual_style import (
    ASSET_CHARACTER,
    AssetStylePlan,
    compose_anchor_guided_prompt,
    ensure_asset_style,
    resolve_asset_style_plan,
)

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _progress(message: str) -> None:
    """向终端输出不会被 logging 配置吞掉的实时进度。"""
    # 写 stderr，避免破坏 main() 最后输出的机器可解析 JSON。
    print(f"[redraw] {message}", file=sys.stderr, flush=True)

from sqlalchemy import create_engine, text

from services.char_designer.core.generate_calm import (
    _combination_to_filename,
    _parse_combination_str,
    chinese_to_camel_case,
    load_prompt_metadata_from_db,
    parse_characters_by_active_combinations,
    parse_characters_first_combination,
    parse_characters_from_db_json,
)
from services.char_designer.core.emotion_prompt_builder import (
    EMOTIONS,
    has_personality_content,
    modify_prompt_for_emotion,
    normalize_personality,
    select_personality_variant,
)
from services.char_designer.core.generate_shots import (
    SHOT_CHOICES,
    generate_shots_for_task,
)
from services.char_designer.core.expand_canvas import expand_for_task
from services.char_designer.core.generate_variant_calm import IDENTITY_CONSISTENCY_PREFIX
from char_designer.image_providers.gpt_image import GPTImageEditProvider, GPTImageTextProvider
from char_designer.image_providers.qwen_image import QwenImageEditProvider, QwenTextImageProvider
from char_designer.image_providers.wan_image_25 import Wan25TextImageProvider
from services.char_designer.core.artifact_contract import validate_image_file
from services.char_designer.utils.native_transparency import (
    CharacterOutputPolicy,
    resolve_character_output_policy,
    validator_for_policy,
)


@dataclass
class ImageProviderOptions:
    image_provider: str = "gpt"
    api_key: Optional[str] = None
    base_url: str = field(default_factory=lambda: os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"))
    model: Optional[str] = None
    size: Optional[str] = None
    quality: str = "high"
    response_format: str = ""
    output_format: str = ""
    background: str = ""
    moderation: str = ""
    timeout_seconds: int = 300
    native_transparency: bool = False
    require_transparency: bool = False
    output_policy: Optional[CharacterOutputPolicy] = None


@dataclass
class RedrawResult:
    speaker_id: str
    success: bool
    calm_generated: bool = False
    emotion_generated: bool = False
    error_message: str = ""
    calm_image_path: Optional[str] = None
    emotion_count: int = 0
    shot_count: int = 0


@dataclass
class CharacterContext:
    key: str
    name: str
    data: Dict
    speaker_id: str
    active_combinations: List[Dict]
    base_combination: str
    output_dir: Path
    prompt_records: Dict[str, Dict]
    style_plan: Optional[AssetStylePlan] = None


def get_character_by_speaker_id(task_id: str, speaker_id: str) -> Optional[Dict]:
    try:
        engine = create_engine(PLATFORM_DATABASE_URL)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM characters WHERE task_id = :tid"),
                {"tid": task_id}
            ).fetchone()
            if not row:
                return None

            char_data = row[0]
            for char_key, char_value in char_data.items():
                char_record = char_value[0] if isinstance(char_value, list) else char_value
                if not isinstance(char_record, dict):
                    continue
                char_name = char_record.get("name", char_key)
                if chinese_to_camel_case(char_name) == speaker_id:
                    return {"key": char_key, "name": char_name, "data": char_record}
            return None
    except Exception as e:
        logger.error(f"查询角色数据失败: {e}")
        return None


def get_character_by_name(task_id: str, name: str) -> Optional[Dict]:
    try:
        engine = create_engine(PLATFORM_DATABASE_URL)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM characters WHERE task_id = :tid"),
                {"tid": task_id}
            ).fetchone()
            if not row:
                return None

            char_data = row[0]
            camel_name = chinese_to_camel_case(name)
            for char_key, char_value in char_data.items():
                char_record = char_value[0] if isinstance(char_value, list) else char_value
                if not isinstance(char_record, dict):
                    continue
                char_name = char_record.get("name", char_key)
                if char_name == name or chinese_to_camel_case(char_name) == camel_name:
                    return {"key": char_key, "name": char_name, "data": char_record}
            return None
    except Exception as e:
        logger.error(f"查询角色数据失败: {e}")
        return None


def _load_name_mappings(task_id: str) -> Dict:
    try:
        engine = create_engine(PLATFORM_DATABASE_URL)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM name_mappings WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id},
            ).fetchone()
        value = row[0] if row else None
        if isinstance(value, (str, bytes, bytearray)):
            value = json.loads(value)
        return dict(value) if isinstance(value, dict) else {}
    except Exception as e:
        logger.warning(f"加载 name_mappings 失败: {e}")
        return {}


def _resolve_speaker_id_from_any_name(task_id: str, name: str) -> Optional[str]:
    """把输入名（改名前 or 改名后 or 本地化译名）解析成 characters 表锚定的
    改名前 speaker_id。characters/character_prompts 的身份锚点始终是改名前 ID，
    所以改名后的名字要先经 new-name 折回改名前名字，再取其 conversion ID。"""
    name = str(name or "").strip()
    if not name:
        return None
    data = _load_name_mappings(task_id)
    if not data:
        return None

    # 改名前名字 -> 改名前规范 ID（characters 表锚点）
    original_conversion = get_original_conversion(data)

    # 直接命中改名前名字
    if name in original_conversion:
        return str(original_conversion[name])

    # 改名后名字 -> 改名前名字（new-name 是 original_name -> changed_name）
    change = get_change_name_section(data)
    new_names = change.get("new-name")
    new_names = dict(new_names) if isinstance(new_names, dict) else {}
    for original_name, changed_name in new_names.items():
        if str(changed_name).strip() == name and original_name in original_conversion:
            return str(original_conversion[original_name])

    return None


def resolve_character(task_id: str, char_identifier: str) -> Optional[Dict]:
    char_info = get_character_by_speaker_id(task_id, char_identifier)
    if char_info:
        return char_info
    char_info = get_character_by_name(task_id, char_identifier)
    if char_info:
        return char_info
    # 兜底：输入的可能是改名后/本地化名字，characters 表里存的是改名前原名，
    # 直接按 name 比对会漏。经 name_mappings 折回改名前 speaker_id 再匹配。
    resolved_speaker_id = _resolve_speaker_id_from_any_name(task_id, char_identifier)
    if resolved_speaker_id:
        return get_character_by_speaker_id(task_id, resolved_speaker_id)
    return None


def _get_active_combinations(char_data: Dict) -> List[Dict]:
    if isinstance(char_data, list):
        char_data = char_data[0] if char_data else {}
    if not isinstance(char_data, dict):
        return []

    combinations = char_data.get("valid_status_combinations", [])
    active = [
        combo for combo in combinations
        if isinstance(combo, dict) and combo.get("is_active") and combo.get("combination")
    ]

    def _sort_key(combo: Dict) -> Tuple[int, int, int]:
        try:
            _, p_id, b_id, c_id = _parse_combination_str(combo["combination"])
            return p_id, b_id, c_id
        except Exception:
            return 999, 999, 999

    return sorted(active, key=_sort_key)


def _normalize_combination(combination: str, char_name: str, speaker_id: str) -> str:
    value = Path(combination.strip()).stem
    value = re.sub(r'_(calm|happy|sad|angry|fearful|disgusted|surprised)$', '', value)
    value = re.sub(r'^([A-Za-z一-鿿]+)_(\d+-\d+-\d+)$', r'\1-\2', value)

    short_match = re.fullmatch(r'(\d+)-(\d+)-(\d+)', value)
    if short_match:
        return f"{char_name}-{short_match.group(1)}-{short_match.group(2)}-{short_match.group(3)}"

    parts = value.rsplit("-", 3)
    if len(parts) != 4:
        return value

    name_part, p_id, b_id, c_id = parts
    if name_part == char_name:
        return value
    if name_part == speaker_id or chinese_to_camel_case(name_part) == speaker_id:
        return f"{char_name}-{p_id}-{b_id}-{c_id}"
    return value


def _find_calm_image_for_combination(context: "CharacterContext", combination: str) -> Optional[Path]:
    path = context.output_dir / _calm_filename(context, combination)
    return path if path.exists() and validate_image_file(path) else None


def _safe_replace(temp_path: Path, final_path: Path) -> bool:
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp_path, final_path)
        return True
    except Exception as e:
        logger.error(f"替换文件失败 {final_path}: {e}")
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass
        return False


def _cleanup_temp_file(temp_path: Path) -> None:
    try:
        if temp_path.exists():
            temp_path.unlink()
    except Exception:
        pass


def _get_prompt_record(context: CharacterContext, combination: Optional[str] = None) -> Optional[Dict]:
    if combination and context.prompt_records.get(combination):
        return context.prompt_records[combination]
    return context.prompt_records.get(context.speaker_id)


def _calm_filename(context: CharacterContext, combination: str) -> str:
    """解析 calm 图文件名，与主流程共用一套 speaker_id 合同：
    优先沿用磁盘上已存在的 calm 文件名（prefer_existing），
    再取 character_prompts 里对齐过的权威 speaker_id（prompt_record），
    最后才回退中文名转拼音。避免 redraw 自造出与批量流程不一致的文件名。"""
    return _combination_to_filename(
        combination,
        "calm",
        prompt_record=_get_prompt_record(context, combination),
        output_dir=context.output_dir,
        prefer_existing=True,
    )


def _build_context(task_id: str, char_identifier: str) -> Optional[CharacterContext]:
    char_info = resolve_character(task_id, char_identifier)
    if not char_info:
        return None

    active_combinations = _get_active_combinations(char_info["data"])
    if not active_combinations:
        return None

    _, _, prompt_records = load_prompt_metadata_from_db(task_id)

    try:
        style_plan = resolve_asset_style_plan(task_id, ASSET_CHARACTER)
    except Exception as exc:
        # 与主流程一致：风格锚点解析失败时退回纯文生图，不阻断重绘。
        logger.warning(f"人物风格锚点解析失败，回退纯文生图: {exc}")
        style_plan = None

    return CharacterContext(
        key=char_info["key"],
        name=char_info["name"],
        data=char_info["data"],
        speaker_id=chinese_to_camel_case(char_info["name"]),
        active_combinations=active_combinations,
        base_combination=active_combinations[0]["combination"],
        output_dir=Path(OUTPUTS_PATH) / task_id / "images" / "characters",
        prompt_records=prompt_records,
        style_plan=style_plan,
    )


def _build_character_for_combination(context: CharacterContext, combination: str) -> Optional[object]:
    char_data = {context.key: context.data}
    characters = parse_characters_by_active_combinations(char_data)
    character = next((c for c in characters if c.combination == combination), None)
    if not character and combination == context.base_combination:
        base_characters = parse_characters_first_combination(char_data)
        character = base_characters[0] if base_characters else None
    if not character:
        fallback_characters = parse_characters_from_db_json(char_data)
        character = fallback_characters[0] if fallback_characters else None
    if not character:
        return None

    character.speaker_id = context.speaker_id
    character.combination = combination
    return character


def _get_effective_prompt(
    task_id: str,
    context: CharacterContext,
    combination: str,
) -> Optional[str]:
    # 生图侧只读 character_prompts 表里已算好的提示词，不再自造。
    # 造词属于 info_extractor 侧，读不到就报错（由上游保证提示词已落库）。
    record = _get_prompt_record(context, combination)
    if not record:
        return None
    return ensure_asset_style(
        task_id,
        ASSET_CHARACTER,
        record.get("positive_prompt") or "",
    )


def _personality_for_combination(context: CharacterContext, combination: str) -> Dict:
    record = _get_prompt_record(context, combination)
    personality = normalize_personality(record.get("personality") if record else None)
    if has_personality_content(personality):
        return personality

    personality = select_personality_variant(context.data, combination)
    if has_personality_content(personality):
        logger.warning(
            f"{combination} 的 character_prompts 缺少 personality，"
            "本次从 characters 做只读 legacy 回退；建议重跑 Step 8 --force"
        )
    return personality


def _emotions_for_combination(context: CharacterContext, combination: str) -> List[str]:
    """Return the combination contract's non-calm emotions.

    ``redraw_character`` historically used the global ``EMOTIONS`` list for
    manual redraws.  The Anchor full-run mode follows the persisted
    ``character_prompts`` contract when it is available, so a combination only
    receives the emotions declared for that combination.
    """
    record = _get_prompt_record(context, combination)
    raw_emotions = record.get("emotions") if isinstance(record, dict) else None
    if not isinstance(raw_emotions, list):
        return list(EMOTIONS)

    selected: List[str] = []
    for raw_emotion in raw_emotions:
        emotion = str(raw_emotion or "").strip().lower()
        if emotion == "calm" or emotion not in EMOTIONS or emotion in selected:
            continue
        selected.append(emotion)
    return selected


def _create_text_provider(options: ImageProviderOptions):
    if options.image_provider == "qwen":
        return QwenTextImageProvider(
            api_key=options.api_key,
            model=options.model,
            size=options.size,
            max_retries=int(os.environ.get("QWEN_MAX_RETRIES", "3")),
            retry_delay=int(os.environ.get("QWEN_RETRY_DELAY", "5")),
        )
    if options.image_provider == "wan":
        return Wan25TextImageProvider(
            api_key=options.api_key,
            model=options.model or "wan2.5-t2i-preview",
            size=options.size,
            max_retries=int(os.environ.get("WAN_MAX_RETRIES", "3")),
            retry_delay=int(os.environ.get("WAN_RETRY_DELAY", "5")),
            request_timeout=options.timeout_seconds,
        )

    gpt_api_key = options.api_key or os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
    return GPTImageTextProvider(
        api_key=gpt_api_key,
        base_url=options.base_url,
        model=options.model or "gpt-image-2",
        size=options.size or "1024x1536",
        quality=options.quality,
        n=1,
        response_format=options.response_format,
        output_format=options.output_format,
        background=options.background,
        moderation=options.moderation,
        timeout_seconds=options.timeout_seconds,
        transparent_character_output=options.native_transparency,
    )


def _create_edit_provider(options: ImageProviderOptions):
    if options.image_provider in {"qwen", "wan"}:
        return QwenImageEditProvider(
            api_key=options.api_key,
            model=(None if options.image_provider == "wan" else options.model)
            or os.environ.get("QWEN_VARIANT_MODEL", "qwen-image-edit-plus"),
            size=options.size or "1140*1472",
            max_retries=int(os.environ.get("QWEN_MAX_RETRIES", "3")),
            retry_delay=int(os.environ.get("QWEN_RETRY_DELAY", "5")),
        )

    gpt_api_key = options.api_key or os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
    return GPTImageEditProvider(
        api_key=gpt_api_key,
        base_url=options.base_url,
        model=options.model or "gpt-image-2",
        size=options.size or "1024x1536",
        quality=options.quality,
        n=1,
        response_format=options.response_format,
        output_format=options.output_format,
        background=options.background,
        moderation=options.moderation,
        timeout_seconds=options.timeout_seconds,
        transparent_character_output=options.native_transparency,
    )


def _save_text_image(provider, image_provider: str, prompt: str, output_path: Path) -> bool:
    if image_provider in {"qwen", "wan"}:
        image_url = provider.generate(prompt)
        return bool(image_url and provider.download(image_url, output_path))

    data_items = provider.generate(prompt)
    return bool(data_items and provider.save_image_item(data_items[0], output_path))


def _save_edit_image(provider, image_provider: str, base_image: Path, prompt: str, output_path: Path) -> bool:
    if image_provider in {"qwen", "wan"}:
        image_url = provider.generate(base_image, prompt)
        return bool(image_url and provider.download(image_url, output_path))

    data_items = provider.edit(base_image, prompt)
    return bool(data_items and provider.save_image_item(data_items[0], output_path))



def _normalize_shots(shots: Optional[List[str]]) -> List[str]:
    """去重规整景别：始终包含 LS（AI 出图源），保持 LS/MS/CS 顺序，忽略非法值。"""
    requested = {s.strip().upper() for s in (shots or []) if s and s.strip()}
    requested.add("LS")
    return [s for s in SHOT_CHOICES if s in requested]


def _resume_source_path(path: Path) -> Path:
    """返回 expand_canvas 原流水线使用的未扩展 LS 私有备份路径。"""
    # public: <task>/images/characters/<file>；private: <task>/.resume_sources/...
    task_root = path.parents[2]
    return task_root / ".resume_sources" / "char_designer" / "ls" / path.name


def _is_expanded_canvas_file(path: Path) -> bool:
    """判断图片是否已经被 expand_canvas 加过顶部透明画布。"""
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.info.get("expanded_canvas_v") == "2"
    except Exception:
        return False


def _unexpanded_generation_source(path: Path) -> Optional[Path]:
    """返回可安全用于图生图的未扩展 LS。

    force 重绘会先写入新的未扩展 LS。此时即使旧的 ``.resume_sources`` 仍在，
    也必须使用当前文件，不能让旧恢复源覆盖本次重绘结果。若 public LS 已经
    扩展且恢复源缺失，则返回 ``None``，禁止把顶部透明画布传给图生图。
    """
    if validate_image_file(path) and not _is_expanded_canvas_file(path):
        return path
    source_path = _resume_source_path(path)
    if validate_image_file(source_path):
        return source_path
    return None


def _emotion_ls_path(calm_path: Path, emotion: str) -> Path:
    """由 calm LS 文件名推导同一组合的情绪 LS 文件名。"""
    stem = calm_path.stem
    if stem.endswith("_LS"):
        stem = stem[:-len("_LS")]
    if stem.endswith("_calm"):
        stem = stem[:-len("_calm")]
    return calm_path.with_name(f"{stem}_{emotion}_LS.png")


def _calm_path_candidates(context: "CharacterContext", combination: str) -> List[Path]:
    """Return existing-preferred and canonical calm paths for one combination.

    A previous run may have used a legacy speaker ID while the current prompt
    record uses the authoritative ID.  Force cleanup must remove both names so
    the old file cannot survive beside the newly generated one.
    """
    prompt_record = _get_prompt_record(context, combination)
    names = {
        _calm_filename(context, combination),
        _combination_to_filename(
            combination,
            "calm",
            prompt_record=prompt_record,
            output_dir=context.output_dir,
            prefer_existing=False,
        ),
    }
    return [context.output_dir / name for name in sorted(names)]


def _force_remove_asset_files(ls_path: Path, *, remove_ls: bool) -> None:
    """删除一个 LS 资产及其派生文件/恢复源。

    这里只接受 redraw 已经解析出的明确文件路径，不做目录级删除。派生 MS/CS
    始终删除，因为 LS 一旦变化，旧景别就不再与新 LS 匹配。
    """
    ls_path = Path(ls_path)
    paths = [
        ls_path.parent / f"{ls_path.stem[:-len('_LS')] if ls_path.stem.endswith('_LS') else ls_path.stem}_MS.png",
        ls_path.parent / f"{ls_path.stem[:-len('_LS')] if ls_path.stem.endswith('_LS') else ls_path.stem}_CS.png",
        ls_path.parent / f".{ls_path.name}.tmp.png",
    ]
    if remove_ls:
        paths.extend((ls_path, _resume_source_path(ls_path)))

    for path in dict.fromkeys(paths):
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
                _progress(f"force 清理旧产物: {path}")
        except Exception as exc:
            raise RuntimeError(f"force 清理文件失败 {path}: {exc}") from exc


def _force_cleanup_for_mode(
    context: "CharacterContext",
    redraw_mode: str,
    target_combination: str,
    emotions: Optional[List[str]] = None,
) -> None:
    """按 redraw 模式精确清理 force 重绘会覆盖的本地资产。

    基础 calm 在情绪-only/variant-emotions/all-except-base-calm 模式中是输入，
    因此保留它本身及其恢复源/派生景别；其余本次要重新生成的 calm/情绪 LS
    则连同对应恢复源和 MS/CS 一起删除。
    """
    if redraw_mode == "all_from_anchor":
        specs = [
            # A calm replacement invalidates every old emotion file for that
            # combination, including emotions removed from the current prompt
            # contract; otherwise stale assets would survive a force run.
            (combo_info["combination"], True, list(EMOTIONS))
            for combo_info in context.active_combinations
        ]
    elif redraw_mode == "all_except_base_calm":
        specs = [(context.base_combination, False, list(EMOTIONS))]
        specs.extend(
            (combo_info["combination"], True, list(EMOTIONS))
            for combo_info in context.active_combinations
            if combo_info["combination"] != context.base_combination
        )
    elif redraw_mode in {"emotions_only", "variant_emotions"}:
        target_emotions = list(emotions or EMOTIONS)
        specs = [(target_combination, False, target_emotions)]
    else:
        # calm_only 的契约只重生成 calm 及其派生景别；情绪图不是本次
        # 模式的输出，因此不能因为 force 而把它们误删。需要整套 calm+
        # 情绪重建时使用 all_from_anchor/all_except_base_calm。
        specs = [(target_combination, True, [])]

    for combination, remove_calm, target_emotions in specs:
        for calm_path in _calm_path_candidates(context, combination):
            if remove_calm:
                _force_remove_asset_files(calm_path, remove_ls=True)

            for emotion in target_emotions:
                emotion_path = _emotion_ls_path(calm_path, str(emotion).strip().lower())
                _force_remove_asset_files(emotion_path, remove_ls=True)


def _expand_and_apply_shots(
    task_id: str,
    ls_paths: List[Path],
    shots: List[str],
    options: ImageProviderOptions,
) -> Tuple[int, List[str]]:
    """按原始人物流水线调用正式的扩展和景别步骤。

    所有图生图必须在本函数之前完成。这里不复制 ``expand_canvas.py`` 或
    ``generate_shots.py`` 的实现，而是直接调用它们的任务级公开函数；
    ``image_paths`` 让单角色 redraw 只处理本次生成的 LS 集合。force 的删除
    已经使目标输出缺失、恢复源或 LS 输入发生变化，因此下游 checkpoint 会
    自动失效，不再向这两个确定性后处理步骤传递 force。
    """
    unique_paths: List[Path] = []
    seen = set()
    for path in ls_paths:
        resolved = Path(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_paths.append(resolved)

    if not unique_paths:
        return 0, []

    errors: List[str] = []
    valid_paths = [path for path in unique_paths if validate_image_file(path)]
    invalid_paths = [path for path in unique_paths if path not in valid_paths]
    for path in invalid_paths:
        errors.append(f"无效 LS 图片，无法后处理: {path.name}")
        _progress(f"后处理跳过，无效 LS: {path}")
    if not valid_paths:
        return 0, errors

    _progress(f"开始按身高扩展顶部透明画布: LS 数量={len(valid_paths)}")
    try:
        expanded_count = expand_for_task(
            task_id,
            image_paths=valid_paths,
            progress=_progress,
        )
        _progress(f"顶部身高画布阶段结束: 扩展={expanded_count}")
    except Exception as exc:
        errors.append(f"顶部身高画布扩展失败: {exc}")
        _progress(f"顶部身高画布阶段失败: {exc}")
        # 景别必须以扩展后的 LS 为输入；画布阶段失败时不能继续用
        # 未扩展的图片裁切出 MS/CS。
        return 0, errors

    requested_derived = [shot for shot in shots if shot != "LS"]
    if not requested_derived:
        return 0, errors

    _progress(
        f"开始生成派生景别: LS 数量={len(valid_paths)} "
        f"shots={','.join(requested_derived)}"
    )
    try:
        shot_result = generate_shots_for_task(
            task_id,
            image_provider=options.image_provider,
            image_paths=valid_paths,
            shots=requested_derived,
            progress=_progress,
        )
        shot_count = int(shot_result.get("ms", 0)) + int(shot_result.get("cs", 0))
        _progress(
            f"派生景别阶段结束: MS={shot_result.get('ms', 0)} "
            f"CS={shot_result.get('cs', 0)} 失败={shot_result.get('failed', 0)}"
        )
        if shot_result.get("failed"):
            errors.append(f"景别裁切失败 {shot_result['failed']} 张")
        return shot_count, errors
    except Exception as exc:
        errors.append(f"景别裁切失败: {exc}")
        _progress(f"景别裁切阶段失败: {exc}")
        return 0, errors


def _generate_base_calm(
    context: CharacterContext,
    combination: str,
    prompt: str,
    options: ImageProviderOptions,
    *,
    force: bool = False,
) -> Tuple[bool, Optional[str]]:
    if not _build_character_for_combination(context, combination):
        logger.error(f"解析角色变体失败: {combination}")
        return False, None

    filename = _calm_filename(context, combination)
    final_path = context.output_dir / filename
    temp_path = context.output_dir / f".{filename}.tmp.png"
    _cleanup_temp_file(temp_path)

    if final_path.exists() and validate_image_file(final_path) and not force:
        _progress(f"基础 calm 已存在，跳过: {final_path}")
        return True, str(final_path)

    _progress(
        f"开始生成基础 calm: combination={combination} "
        f"方式={'强制覆盖' if force else '缺失时生成'} -> {final_path}"
    )

    # 与主流程 generate_calm 对齐：任务配置了风格锚点时，基础 calm 走
    # anchor 图生图（锚点只控媒介/线条/色彩/光影/材质，内容以提示词为准）；
    # 否则退回纯文生图。变体图/情绪图仍以 calm 为基础做图生图，不受此分支影响。
    style_plan = context.style_plan
    uses_anchor = bool(style_plan is not None and style_plan.uses_anchor)
    if uses_anchor:
        provider = _create_edit_provider(options)
        provider_prompt = compose_anchor_guided_prompt(style_plan, prompt)
        _progress(f"基础 calm 使用 Anchor 图生图: {style_plan.anchor_path}")
        saved = _save_edit_image(
            provider,
            options.image_provider,
            Path(style_plan.anchor_path),
            provider_prompt,
            temp_path,
        )
    else:
        provider = _create_text_provider(options)
        _progress("基础 calm 未配置 Anchor，使用文生图")
        saved = _save_text_image(provider, options.image_provider, prompt, temp_path)

    if not saved or not temp_path.exists():
        _cleanup_temp_file(temp_path)
        _progress(f"基础 calm 生成失败: {final_path}")
        return False, None

    validator = (
        validator_for_policy(options.output_policy)
        if options.output_policy is not None
        else validate_image_file
    )
    if not validator(temp_path):
        _cleanup_temp_file(temp_path)
        _progress(f"基础 calm 校验失败: {final_path}")
        return False, None
    if not _safe_replace(temp_path, final_path):
        _progress(f"基础 calm 覆盖失败: {final_path}")
        return False, None

    if _usage_tracker:
        _usage_tracker.add_image(1, provider.model)
    _progress(f"基础 calm 完成: {final_path}")
    return True, str(final_path)


def _generate_variant_calm(
    context: CharacterContext,
    combination: str,
    prompt: str,
    options: ImageProviderOptions,
    *,
    force: bool = False,
) -> Tuple[bool, Optional[str]]:
    filename = _calm_filename(context, combination)
    final_path = context.output_dir / filename
    temp_path = context.output_dir / f".{filename}.tmp.png"
    _cleanup_temp_file(temp_path)

    if final_path.exists() and validate_image_file(final_path) and not force:
        _progress(f"变体 calm 已存在，跳过: combination={combination} -> {final_path}")
        return True, str(final_path)

    base_calm = _find_calm_image_for_combination(context, context.base_combination)
    if not base_calm:
        logger.error(f"未找到基础 calm 图: {context.base_combination}")
        _progress(f"变体 calm 无法生成，缺少基础 calm: {context.base_combination}")
        return False, None
    generation_source = _unexpanded_generation_source(base_calm)
    if generation_source is None:
        _progress(
            f"变体 calm 无法生成，基础 calm 已扩展但缺少未扩展恢复源: {base_calm}"
        )
        return False, None

    _progress(
        f"开始生成变体 calm: combination={combination} "
        f"方式={'强制覆盖' if force else '缺失时生成'} -> {final_path}"
    )

    provider = _create_edit_provider(options)
    _progress(f"变体 calm 使用未扩展基础图图生图: {generation_source}")
    if not _save_edit_image(
        provider,
        options.image_provider,
        generation_source,
        IDENTITY_CONSISTENCY_PREFIX + prompt,
        temp_path,
    ) or not temp_path.exists():
        _cleanup_temp_file(temp_path)
        _progress(f"变体 calm 生成失败: combination={combination}")
        return False, None
    validator = (
        validator_for_policy(options.output_policy)
        if options.output_policy is not None
        else validate_image_file
    )
    if not validator(temp_path):
        _cleanup_temp_file(temp_path)
        _progress(f"变体 calm 校验失败: {final_path}")
        return False, None
    if not _safe_replace(temp_path, final_path):
        _progress(f"变体 calm 覆盖失败: {final_path}")
        return False, None

    if _usage_tracker:
        _usage_tracker.add_image(1, provider.model)
    _progress(f"变体 calm 完成: combination={combination} -> {final_path}")
    return True, str(final_path)


def _ensure_variant_base_available(
    task_id: str,
    context: CharacterContext,
    result: RedrawResult,
    options: ImageProviderOptions,
    *,
    force: bool = False,
) -> bool:
    if _find_calm_image_for_combination(context, context.base_combination):
        _progress(f"基础 calm 已就绪，作为变体图生图基图: {context.base_combination}")
        return True

    prompt = _get_effective_prompt(task_id, context, context.base_combination)
    if not prompt:
        logger.error(f"无法获取基础组合提示词: {context.base_combination}")
        return False

    success, image_path = _generate_base_calm(
        context, context.base_combination, prompt, options, force=force,
    )
    if success and image_path:
        result.calm_generated = True
        result.calm_image_path = image_path
    return success


def _generate_emotions_for_combination(
    task_id: str,
    context: CharacterContext,
    combination: str,
    calm_image: Path,
    target_emotions: List[str],
    options: ImageProviderOptions,
    *,
    force: bool = False,
) -> Tuple[int, List[Path], List[str]]:
    # 延迟初始化 provider：当所有目标情绪图都已存在且未使用 --force 时，
    # 可以纯本地恢复，不应因为缺少 API Key 而阻断跳过流程。
    provider = None
    success_count = 0
    generated_paths: List[Path] = []
    errors: List[str] = []
    calm_stem = calm_image.stem
    if calm_stem.endswith("_LS"):
        calm_stem = calm_stem[:-3]
    calm_stem = calm_stem[:-5] if calm_stem.endswith("_calm") else calm_stem
    personality = _personality_for_combination(context, combination)
    generation_source = _unexpanded_generation_source(calm_image)
    if generation_source is None:
        error = f"缺少未扩展 calm 恢复源，无法生成情绪图: {calm_image.name}"
        _progress(error)
        return 0, [], [error]
    if generation_source != calm_image:
        _progress(f"情绪图使用未扩展 calm 源图: {generation_source}")

    for emotion_index, emotion in enumerate(target_emotions, start=1):
        final_path = context.output_dir / f"{calm_stem}_{emotion}_LS.png"
        temp_path = context.output_dir / f".{final_path.name}.tmp.png"
        _cleanup_temp_file(temp_path)

        if final_path.exists() and validate_image_file(final_path) and not force:
            _progress(
                f"情绪图已存在，跳过: combination={combination} "
                f"emotion={emotion} ({emotion_index}/{len(target_emotions)}) -> {final_path}"
            )
            # Existing valid LS is a successful available emotion image.  Its
            # height expansion and derived shots are handled after all
            # image-edit calls, matching the original pipeline order.
            generated_paths.append(final_path)
            success_count += 1
            continue

        _progress(
            f"开始生成情绪图: combination={combination} emotion={emotion} "
            f"({emotion_index}/{len(target_emotions)}) "
            f"方式={'强制覆盖' if force else '缺失时生成'} -> {final_path}"
        )
        if provider is None:
            provider = _create_edit_provider(options)
        prompt = ensure_asset_style(
            task_id,
            ASSET_CHARACTER,
            modify_prompt_for_emotion("", emotion, personality=personality),
        )
        logger.info(
            f"[{context.speaker_id}] combination={combination} emotion={emotion} "
            f"personality_id={personality.get('personality_id')} prompt={prompt}"
        )
        if not _save_edit_image(
            provider,
            options.image_provider,
            generation_source,
            prompt,
            temp_path,
        ) or not temp_path.exists():
            _cleanup_temp_file(temp_path)
            errors.append(f"情绪图生成失败: {final_path.name}")
            _progress(f"情绪图生成失败: {final_path}")
            continue
        validator = (
            validator_for_policy(options.output_policy)
            if options.output_policy is not None
            else validate_image_file
        )
        if not validator(temp_path):
            _cleanup_temp_file(temp_path)
            errors.append(f"情绪图透明合同校验失败: {final_path.name}")
            _progress(f"情绪图校验失败: {final_path}")
            continue
        if not _safe_replace(temp_path, final_path):
            errors.append(f"情绪图替换失败: {final_path.name}")
            _progress(f"情绪图覆盖失败: {final_path}")
            continue
        if _usage_tracker:
            _usage_tracker.add_image(1, provider.model)
        success_count += 1
        generated_paths.append(final_path)
        _progress(f"情绪图完成: combination={combination} emotion={emotion} -> {final_path}")

    return success_count, generated_paths, errors


def redraw_single_character(
    task_id: str,
    char: Optional[str] = None,
    speaker_id: Optional[str] = None,
    name: Optional[str] = None,
    combination: Optional[str] = None,
    redraw_mode: str = "calm_only",
    emotions: Optional[List[str]] = None,
    shots: Optional[List[str]] = None,
    api_key: Optional[str] = None,
    image_provider: str = "gpt",
    base_url: str = "",
    model: Optional[str] = None,
    size: Optional[str] = None,
    quality: str = "high",
    response_format: str = "",
    output_format: str = "",
    background: str = "",
    moderation: str = "",
    timeout_seconds: int = 300,
    force: bool = False,
) -> RedrawResult:
    start_time = time.time()
    from services.char_designer.core.generate_calm import _init_conversion_cache
    _init_conversion_cache(task_id)
    resolved_char = char or speaker_id or name
    if not resolved_char and combination:
        resolved_char = combination.split("-")[0]

    result = RedrawResult(speaker_id=resolved_char or "unknown", success=False)
    _progress(
        f"开始人物重绘: task_id={task_id} char={resolved_char or '-'} "
        f"mode={redraw_mode} force={force}"
    )
    if not resolved_char:
        result.error_message = "必须提供 char 参数或 combination 参数"
        _progress(f"参数错误: {result.error_message}")
        return result

    context = _build_context(task_id, resolved_char)
    if not context:
        result.error_message = f"未找到角色或有效组合: {resolved_char}"
        _progress(f"角色解析失败: {result.error_message}")
        return result

    context.output_dir.mkdir(parents=True, exist_ok=True)
    result.speaker_id = context.speaker_id
    _progress(
        f"已解析角色: speaker_id={context.speaker_id} "
        f"active_combinations={len(context.active_combinations)} "
        f"output_dir={context.output_dir}"
    )

    if combination:
        target_combination = _normalize_combination(combination, context.name, context.speaker_id)
    else:
        target_combination = context.base_combination

    options = ImageProviderOptions(
        image_provider=image_provider,
        api_key=api_key,
        base_url=base_url or os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"),
        model=model,
        size=size,
        quality=quality,
        response_format=response_format,
        output_format=output_format,
        background=background,
        moderation=moderation,
        timeout_seconds=timeout_seconds,
    )
    try:
        output_policy = resolve_character_output_policy(
            task_id,
            image_provider=options.image_provider,
            model=options.model or {
                "gpt": "gpt-image-2",
                "qwen": "qwen-image-plus",
                "wan": "wan2.5-t2i-preview",
            }[options.image_provider],
            requested_background=options.background,
            requested_output_format=options.output_format,
        )
    except Exception as exc:
        result.error_message = str(exc)
        _progress(f"输出合同解析失败: {result.error_message}")
        return result
    options.background = output_policy.background
    options.output_format = output_policy.output_format
    options.native_transparency = output_policy.native_transparency
    options.require_transparency = output_policy.require_transparency
    options.output_policy = output_policy
    _progress(
        f"图片配置: provider={options.image_provider} "
        f"model={options.model or {'gpt': 'gpt-image-2', 'qwen': 'qwen-image-plus', 'wan': 'wan2.5-t2i-preview'}[options.image_provider]} "
        f"output_format={options.output_format or '(default)'} "
        f"force={force}"
    )
    # 默认与主流程一致：每张立绘产出 LS/MS/CS 三个景别。
    target_shots = _normalize_shots(shots if shots is not None else list(SHOT_CHOICES))
    _progress(f"目标景别: {','.join(target_shots)}")
    errors: List[str] = []
    pending_ls_paths: List[Path] = []

    try:
        if redraw_mode == "all_from_anchor":
            _progress("进入 all_from_anchor：从 Anchor 开始处理基础图、全部变体和情绪")
            # This mode intentionally starts over from the task-level
            # character Anchor.  It never falls back to text-to-image: the
            # first character image must also be an image edit.
            if context.style_plan is None or not context.style_plan.anchor_path:
                result.error_message = (
                    "未找到任务级角色 Anchor，无法执行 all_from_anchor；"
                    "请先生成并发布 style_anchors/character_anchor.png"
                )
                _progress(f"流程终止: {result.error_message}")
                return result

            base_combination = context.base_combination
            base_prompt = _get_effective_prompt(task_id, context, base_combination)
            if not base_prompt:
                result.error_message = f"无法获取基础组合提示词: {base_combination}"
                _progress(f"流程终止: {result.error_message}")
                return result
            if not validate_image_file(Path(context.style_plan.anchor_path)):
                result.error_message = f"角色 Anchor 不是有效图片: {context.style_plan.anchor_path}"
                _progress(f"流程终止: {result.error_message}")
                return result
            if force:
                for combo_info in context.active_combinations:
                    combination = combo_info["combination"]
                    if not _get_effective_prompt(task_id, context, combination):
                        result.error_message = f"无法获取提示词: {combination}"
                        _progress(f"流程终止: {result.error_message}")
                        return result
                _force_cleanup_for_mode(context, redraw_mode, base_combination)

            base_success, base_image_path = _generate_base_calm(
                context,
                base_combination,
                base_prompt,
                options,
                force=force,
            )
            if not base_success or not base_image_path:
                result.error_message = f"基础组合 {base_combination} 的 Anchor 图生图失败"
                _progress(f"流程终止: {result.error_message}")
                return result

            result.calm_generated = True
            result.calm_image_path = base_image_path
            base_calm_path = Path(base_image_path)
            pending_ls_paths.append(base_calm_path)

            total_emotions = 0
            for combination_index, combo_info in enumerate(context.active_combinations, start=1):
                combination = combo_info["combination"]
                _progress(
                    f"处理变体 {combination} "
                    f"({combination_index}/{len(context.active_combinations)})"
                )
                calm_path = base_calm_path if combination == base_combination else None

                if calm_path is None:
                    prompt = _get_effective_prompt(task_id, context, combination)
                    if not prompt:
                        errors.append(f"无法获取提示词: {combination}")
                        _progress(f"变体跳过，缺少提示词: {combination}")
                        continue
                    success, image_path = _generate_variant_calm(
                        context,
                        combination,
                        prompt,
                        options,
                        force=force,
                    )
                    if not success or not image_path:
                        errors.append(f"变体 calm 生成失败: {combination}")
                        _progress(f"变体跳过，calm 生成失败: {combination}")
                        continue
                    calm_path = Path(image_path)
                    result.calm_image_path = image_path
                    pending_ls_paths.append(calm_path)

                target_emotions = _emotions_for_combination(context, combination)
                emotion_count, emotion_paths, emotion_errors = (
                    _generate_emotions_for_combination(
                        task_id,
                        context,
                        combination,
                        calm_path,
                        target_emotions,
                        options,
                        force=force,
                    )
                )
                total_emotions += emotion_count
                pending_ls_paths.extend(emotion_paths)
                errors.extend(emotion_errors)
                _progress(
                    f"变体处理完成: combination={combination} "
                    f"情绪成功={emotion_count}/{len(target_emotions)}"
                )

            result.emotion_generated = total_emotions > 0
            result.emotion_count = total_emotions
            final_shots, final_errors = _expand_and_apply_shots(
                task_id,
                pending_ls_paths,
                target_shots,
                options,
            )
            result.shot_count += final_shots
            errors.extend(final_errors)
            if errors:
                result.error_message = "; ".join(errors)
                _progress(f"all_from_anchor 完成但有失败: {result.error_message}")
                return result
            result.success = True

        elif redraw_mode == "emotions_only":
            _progress(f"进入 emotions_only: combination={target_combination}")
            target_emotions = [emotion for emotion in (emotions or EMOTIONS) if emotion in EMOTIONS]
            if not target_emotions:
                result.error_message = "没有有效的情绪类型"
                _progress(f"流程终止: {result.error_message}")
                return result

            calm_image = _find_calm_image_for_combination(context, target_combination)
            if not calm_image:
                result.error_message = f"未找到 {target_combination} 的 calm 图，无法生成情绪图"
                _progress(f"流程终止: {result.error_message}")
                return result

            if force:
                _force_cleanup_for_mode(
                    context,
                    redraw_mode,
                    target_combination,
                    emotions=target_emotions,
                )
            count, emotion_paths, errors = _generate_emotions_for_combination(
                task_id, context, target_combination, calm_image, target_emotions, options,
                force=force,
            )
            result.emotion_generated = count > 0
            result.emotion_count = count
            pending_ls_paths.extend(emotion_paths)
            final_shots, final_errors = _expand_and_apply_shots(
                task_id,
                pending_ls_paths,
                target_shots,
                options,
            )
            result.shot_count += final_shots
            errors.extend(final_errors)
            result.success = count == len(target_emotions) and not errors
            if not result.success:
                result.error_message = "; ".join(errors) or "情绪图生成失败"
                _progress(f"emotions_only 失败: {result.error_message}")
                return result

        elif redraw_mode == "variant_emotions":
            _progress(f"进入 variant_emotions: combination={target_combination}")
            calm_image = _find_calm_image_for_combination(context, target_combination)
            if not calm_image:
                result.error_message = f"未找到 {target_combination} 的 calm 图，无法生成情绪图"
                _progress(f"流程终止: {result.error_message}")
                return result

            if force:
                _force_cleanup_for_mode(
                    context,
                    redraw_mode,
                    target_combination,
                    emotions=EMOTIONS,
                )
            count, emotion_paths, errors = _generate_emotions_for_combination(
                task_id, context, target_combination, calm_image, EMOTIONS, options,
                force=force,
            )
            result.emotion_generated = count > 0
            result.emotion_count = count
            pending_ls_paths.extend(emotion_paths)
            final_shots, final_errors = _expand_and_apply_shots(
                task_id,
                pending_ls_paths,
                target_shots,
                options,
            )
            result.shot_count += final_shots
            errors.extend(final_errors)
            result.success = count == len(EMOTIONS) and not errors
            if not result.success:
                result.error_message = "; ".join(errors) or "情绪图生成失败"
                _progress(f"variant_emotions 失败: {result.error_message}")
                return result

        elif redraw_mode == "all_except_base_calm":
            _progress("进入 all_except_base_calm：保留基础 calm，处理其余变体")
            base_calm = _find_calm_image_for_combination(context, context.base_combination)
            if not base_calm:
                result.error_message = f"未找到基础 calm 图，无法保持基础 calm 不变: {context.base_combination}"
                _progress(f"流程终止: {result.error_message}")
                return result

            if force:
                for combo_info in context.active_combinations:
                    combo = combo_info["combination"]
                    if combo == context.base_combination:
                        continue
                    if not _get_effective_prompt(task_id, context, combo):
                        result.error_message = f"无法获取 {combo} 的提示词"
                        _progress(f"流程终止: {result.error_message}")
                        return result
                _force_cleanup_for_mode(context, redraw_mode, context.base_combination)
            base_count, base_paths, base_errors = _generate_emotions_for_combination(
                task_id, context, context.base_combination, base_calm, EMOTIONS, options,
                force=force,
            )
            total_emotions = base_count
            pending_ls_paths.extend(base_paths)
            errors.extend(base_errors)

            for combo_info in context.active_combinations:
                combo = combo_info["combination"]
                if combo == context.base_combination:
                    continue

                prompt = _get_effective_prompt(task_id, context, combo)
                if not prompt:
                    errors.append(f"无法获取提示词: {combo}")
                    _progress(f"变体跳过，缺少提示词: {combo}")
                    continue

                success, image_path = _generate_variant_calm(context, combo, prompt, options, force=force)
                if not success or not image_path:
                    errors.append(f"变体 calm 生成失败: {combo}")
                    _progress(f"变体跳过，calm 生成失败: {combo}")
                    continue

                result.calm_generated = True
                result.calm_image_path = image_path
                calm_path = Path(image_path)
                pending_ls_paths.append(calm_path)
                emotion_count, emotion_paths, emotion_errors = _generate_emotions_for_combination(
                    task_id, context, combo, calm_path, EMOTIONS, options,
                    force=force,
                )
                total_emotions += emotion_count
                pending_ls_paths.extend(emotion_paths)
                errors.extend(emotion_errors)
                _progress(
                    f"变体处理完成: combination={combo} "
                    f"情绪成功={emotion_count}/{len(EMOTIONS)}"
                )

            result.emotion_generated = total_emotions > 0
            result.emotion_count = total_emotions
            final_shots, final_errors = _expand_and_apply_shots(
                task_id,
                pending_ls_paths,
                target_shots,
                options,
            )
            result.shot_count += final_shots
            errors.extend(final_errors)
            if errors:
                result.error_message = "; ".join(errors)
                _progress(f"all_except_base_calm 完成但有失败: {result.error_message}")
                return result
            if not result.calm_generated and not result.emotion_generated:
                result.error_message = "没有生成任何变体 calm 或情绪图"
                _progress(f"流程终止: {result.error_message}")
                return result
            result.success = True

        else:
            _progress(f"进入单组合模式: combination={target_combination}")
            prompt = _get_effective_prompt(task_id, context, target_combination)
            if not prompt:
                result.error_message = f"无法获取 {target_combination} 的提示词"
                _progress(f"流程终止: {result.error_message}")
                return result

            generated_base_path: Optional[Path] = None
            if target_combination == context.base_combination:
                if force:
                    _force_cleanup_for_mode(context, redraw_mode, target_combination)
                success, image_path = _generate_base_calm(
                    context, target_combination, prompt, options, force=force
                )
            else:
                if not _ensure_variant_base_available(
                    task_id, context, result, options, force=force
                ):
                    result.error_message = f"未找到基础 calm 图且自动补生成失败: {context.base_combination}"
                    _progress(f"流程终止: {result.error_message}")
                    return result
                # 变体图生图可能刚刚自动补生成了基础 calm；它同样是新
                # 的 LS，不能漏过后续的画布扩展和 MS/CS 裁切。
                if result.calm_generated and result.calm_image_path:
                    generated_base_path = Path(result.calm_image_path)
                if force:
                    _force_cleanup_for_mode(context, redraw_mode, target_combination)
                success, image_path = _generate_variant_calm(
                    context, target_combination, prompt, options, force=force
                )

            if not success or not image_path:
                result.error_message = "立绘生成失败"
                _progress(f"流程终止: {result.error_message}")
                return result

            result.calm_generated = True
            result.calm_image_path = image_path
            if generated_base_path is not None:
                pending_ls_paths.append(generated_base_path)
            pending_ls_paths.append(Path(image_path))
            final_shots, final_errors = _expand_and_apply_shots(
                task_id,
                pending_ls_paths,
                target_shots,
                options,
            )
            result.shot_count += final_shots
            if final_errors:
                result.error_message = "; ".join(final_errors)
                _progress(f"后处理失败: {result.error_message}")
                return result
            result.success = True

        if redraw_mode in {"emotions_only", "variant_emotions"}:
            result.success = True

    except Exception as e:
        result.error_message = f"重绘异常: {e}"
        _progress(f"流程异常: {result.error_message}")
        logger.exception("角色重绘异常")
        return result

    duration = time.time() - start_time
    _progress(
        f"重绘结束: success={result.success} calm={result.calm_generated} "
        f"emotions={result.emotion_count} shots={result.shot_count} "
        f"耗时={duration:.1f}s"
    )
    logger.info(
        "重绘完成: speaker_id=%s mode=%s shots=%s duration=%.2fs calm=%s emotions=%s emotion_count=%s shot_count=%s",
        result.speaker_id,
        redraw_mode,
        ",".join(target_shots),
        duration,
        result.calm_generated,
        result.emotion_generated,
        result.emotion_count,
        result.shot_count,
    )
    return result


def main():
    parser = argparse.ArgumentParser(description="角色重绘器 - 支持多状态变体的单角色重绘")
    parser.add_argument("--task-id", type=str, required=True, help="任务ID")
    parser.add_argument("--char", type=str, default=None, help="角色标识符（speaker_id 或 name）")
    parser.add_argument("--combination", type=str, default=None, help="目标变体组合，如 TangSan-1-2-3")
    parser.add_argument(
        "--mode",
        type=str,
        default="calm_only",
        choices=[
            "calm_only",
            "all_except_base_calm",
            "all_from_anchor",
            "variant_emotions",
            "emotions_only",
        ],
        help="重绘模式",
    )
    parser.add_argument("--emotions", type=str, default=None, help="逗号分隔的情绪列表")
    parser.add_argument("--shots", type=str, default=None,
                        help="逗号分隔的景别，可选 LS/MS/CS，默认 LS,MS,CS；LS 为 AI 出图源，MS/CS 由 LS 裁切派生")
    parser.add_argument("--api-key", type=str, help="当前 image provider 使用的 API Key")
    parser.add_argument("--image-provider", choices=["qwen", "wan", "gpt"], default="gpt", help="图片模型提供方，默认 gpt")
    parser.add_argument("--base-url",
                        default=os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"),
                        help="GPT Image 兼容接口 Base URL（默认读环境变量 GPT_IMAGE_BASE_URL）")
    parser.add_argument("--model", default=None, help="图片模型名；qwen 默认 qwen-image-plus/qwen-image-edit-plus，gpt 默认 gpt-image-2")
    parser.add_argument("--size", default=None, help="图片尺寸；qwen 格式如 1140*1472，gpt 格式如 1024x1536")
    parser.add_argument("--quality", default="high", help="GPT Image 质量：low / medium / high / auto")
    parser.add_argument("--response-format", default="", help="GPT Image 返回格式：url / b64_json，留空则不传")
    parser.add_argument("--output-format", default="", help="GPT Image 输出格式：png / jpeg / webp，留空则不传")
    parser.add_argument("--background", default="", help="GPT Image 背景；正式人物透明合同会强制使用 transparent")
    parser.add_argument("--moderation", default="", help="GPT Image 审核：auto / low，留空则不传")
    parser.add_argument("--timeout", type=int, default=300, help="GPT Image 请求超时时间（秒）")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "先删除当前模式冲突的本地 LS/MS/CS 和未扩展恢复源，再重新生图、"
            "扩展画布并裁切景别；默认复用已有图片，仅补生成缺失文件"
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出模式")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    emotions_list = None
    if args.emotions:
        emotions_list = [item.strip() for item in args.emotions.split(",") if item.strip()]

    shots_list = None
    if args.shots:
        shots_list = [item.strip() for item in args.shots.split(",") if item.strip()]

    result = redraw_single_character(
        task_id=args.task_id,
        char=args.char,
        combination=args.combination,
        redraw_mode=args.mode,
        emotions=emotions_list,
        shots=shots_list,
        api_key=args.api_key,
        image_provider=args.image_provider,
        base_url=args.base_url,
        model=args.model,
        size=args.size,
        quality=args.quality,
        response_format=args.response_format,
        output_format=args.output_format,
        background=args.background,
        moderation=args.moderation,
        timeout_seconds=args.timeout,
        force=args.force,
    )

    if result.success:
        print(json.dumps({
            "success": True,
            "speaker_id": result.speaker_id,
            "calm_generated": result.calm_generated,
            "emotion_generated": result.emotion_generated,
            "emotion_count": result.emotion_count,
            "shot_count": result.shot_count,
            "calm_image_path": result.calm_image_path,
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    print(json.dumps({
        "success": False,
        "speaker_id": result.speaker_id,
        "error_message": result.error_message,
        "calm_generated": result.calm_generated,
        "emotion_generated": result.emotion_generated,
        "emotion_count": result.emotion_count,
        "shot_count": result.shot_count,
        "calm_image_path": result.calm_image_path,
    }, ensure_ascii=False, indent=2))
    sys.exit(1)


if __name__ == "__main__":
    main()
