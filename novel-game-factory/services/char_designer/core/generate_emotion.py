#!/usr/bin/env python3
"""
情绪图片生成器 - 基于已有的 calm.png 图片生成多情绪变体
使用 qwen-image-edit-plus 模型进行图生图
"""

import os
import sys
import json
import time
import logging
import argparse
import base64
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import dashscope
from dashscope import MultiModalConversation
import requests

# 添加项目根目录到路径（core -> char_designer -> services -> novel-game-factory）
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
            candidates.extend(
                path.parent.parent for path in search_root.glob("*/*/novel-game-factory/common/config.py"))

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

# 尝试加载环境变量（从项目根目录）
try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=project_root_path / "novel-game-factory" / ".env")
    load_dotenv(dotenv_path=workspace_root / ".env")
except ImportError:
    pass  # 如果没有 dotenv，继续

# 导入配置
from common.config import OUTPUTS_PATH
from common.image_concurrency import (
    ThreadLocalResource,
    resolve_image_concurrency,
    run_dependency_layer,
)
from common.platform_db import platform_engine
from common.visual_checkpoint import (
    assert_retry_budget,
    canonical_digest,
    file_sha256,
    mark_failed as checkpoint_mark_failed,
    mark_running as checkpoint_mark_running,
    mark_success as checkpoint_mark_success,
    reusable_or_adopt_existing,
)
from common.visual_style import (
    ASSET_CHARACTER,
    AssetStylePlan,
    apply_asset_style,
    resolve_asset_style_plan,
)
from common.visual_debug import record_visual_generation
from services.char_designer.core.artifact_contract import (
    CharacterImageArtifactError,
    atomic_image_output,
    character_resume_source_path,
    publish_character_resume_source,
    restore_character_working_copy,
    validate_image_file,
)
from services.char_designer.utils.native_transparency import (
    inspect_native_transparency,
    resolve_character_output_policy,
    validator_for_policy,
)

try:
    from char_designer.image_providers.gpt_image import GPTImageEditProvider
    from char_designer.image_providers.qwen_image import QwenImageEditProvider
    from char_designer.utils.prompt_repair import execute_with_prompt_repair
except ImportError:
    from image_providers.gpt_image import GPTImageEditProvider  # type: ignore
    from image_providers.qwen_image import QwenImageEditProvider  # type: ignore
    from utils.prompt_repair import execute_with_prompt_repair  # type: ignore
from services.char_designer.utils.character_identity import speaker_combination_key
from services.char_designer.core.generate_calm import (
    _init_conversion_cache,
    chinese_to_camel_case,
    resolve_authoritative_speaker_id,
)
from services.char_designer.core.emotion_prompt_builder import (
    EMOTIONS,
    EMOTION_PROMPT_CONTRACT_VERSION,
    has_personality_content,
    modify_prompt_for_emotion,
    normalize_personality,
    select_personality_variant,
)

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

# 配置日志
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
EMOTION_GENERATOR_VERSION = "character-emotion-v3-native-rgba"


def encode_image_to_base64(image_path: str) -> str:
    """
    将图像文件编码为Base64字符串，格式为 data:{mime_type};base64,{base64_data}

    Args:
        image_path: 图像文件路径

    Returns:
        str: Base64编码的字符串
    """
    mime_type, _ = mimetypes.guess_type(image_path)
    if not mime_type or not mime_type.startswith("image/"):
        raise ValueError(f"不支持或无法识别的图像格式: {image_path}")

    try:
        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
        return f"data:{mime_type};base64,{encoded_string}"
    except IOError as e:
        raise IOError(f"读取文件时出错: {image_path}, 错误: {str(e)}")


def get_latest_timestamp_dir(base_dir: str) -> Optional[Path]:
    """
    获取基础目录中的最新时间戳目录

    Args:
        base_dir: 基础目录路径

    Returns:
        Optional[Path]: 最新时间戳目录路径，如果没有则返回None
    """
    base_path = Path(base_dir)
    if not base_path.exists():
        return None

    timestamp_dirs = [d for d in base_path.iterdir() if d.is_dir() and d.name[0].isdigit()]
    if not timestamp_dirs:
        return None

    latest_dir = max(timestamp_dirs, key=lambda d: d.name)
    return latest_dir


def normalize_combination_key(combination: str) -> str:
    """
    将 combination 字符串归一化为与文件名一致的 key（驼峰名 + p-b-c）。

    这样无论 DB 里存的是中文名（'萧无尘-1-1-1'）还是英文名（'Harlow-1-1-1'），
    都会被归一化成与 _combination_to_filename 中相同的形式，从而能和
    filename_to_combination 还原出的文件名 key 正确匹配。

    '萧无尘-1-1-1' -> 'XiaoWuChen-1-1-1'
    'Harlow-1-1-1' -> 'Harlow-1-1-1'
    无法解析时原样返回。
    """
    parts = combination.rsplit("-", 3)
    if len(parts) != 4:
        return combination
    name, p, b, c = parts
    try:
        safe_name = chinese_to_camel_case(name)
    except Exception:
        return combination
    return f"{safe_name}-{p}-{b}-{c}"


def _combination_match_keys(combination: str, speaker_id: str = "") -> List[str]:
    keys: List[str] = []
    if combination:
        for value in (
            combination,
            normalize_combination_key(combination),
            speaker_combination_key(speaker_id, combination),
        ):
            if value and value not in keys:
                keys.append(value)
    return keys


def _load_legacy_personality_map(task_id: str) -> Dict[str, Dict[str, Any]]:
    """Read personalities from ``characters`` for pre-contract prompt records."""
    try:
        from sqlalchemy import text as sa_text
        with platform_engine.connect() as conn:
            row = conn.execute(
                sa_text("SELECT data FROM characters WHERE task_id = :tid"),
                {"tid": task_id}
            ).fetchone()
        if not row:
            return {}

        characters = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        result: Dict[str, Dict[str, Any]] = {}
        for char_key, raw_record in (characters or {}).items():
            record = raw_record[0] if isinstance(raw_record, list) and raw_record else raw_record
            if not isinstance(record, dict):
                continue
            char_name = str(record.get("name") or char_key)
            speaker_id = resolve_authoritative_speaker_id(
                char_name,
                chinese_to_camel_case(char_name),
            )
            combinations = record.get("valid_status_combinations") or []
            for combo_info in combinations:
                if not isinstance(combo_info, dict):
                    continue
                combination = str(combo_info.get("combination") or "").strip()
                if not combination:
                    continue
                personality = select_personality_variant(record, combination)
                if not has_personality_content(personality):
                    continue
                for key in _combination_match_keys(combination, speaker_id):
                    result[key] = personality
        return result
    except Exception as e:
        logger.warning(f"读取 legacy personality 回退数据失败: {e}")
        return {}


def load_combination_prompt_records(task_id: str) -> Dict[str, Dict[str, Any]]:
    """Load exact combination records, hydrating legacy personality in memory."""
    try:
        from sqlalchemy import text as sa_text
        with platform_engine.connect() as conn:
            row = conn.execute(
                sa_text("SELECT data FROM character_prompts WHERE task_id = :tid"),
                {"tid": task_id}
            ).fetchone()
        if not row:
            return {}

        items = row[0] if isinstance(row[0], list) else json.loads(row[0])
        result: Dict[str, Dict[str, Any]] = {}
        unique_records: List[Dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            record = dict(item)
            combination = str(record.get("combination") or "").strip()
            if not combination:
                continue
            raw_name = combination.rsplit("-", 3)[0]
            resolved_id = resolve_authoritative_speaker_id(
                raw_name,
                record.get("speaker_id") or "",
            )
            if resolved_id:
                record["speaker_id"] = resolved_id
            personality = normalize_personality(record.get("personality"))
            if has_personality_content(personality):
                record["personality"] = personality
            unique_records.append(record)
            for key in _combination_match_keys(combination, str(record.get("speaker_id") or "")):
                result[key] = record

        if any(not has_personality_content(record.get("personality")) for record in unique_records):
            legacy_map = _load_legacy_personality_map(task_id)
            hydrated = 0
            for record in unique_records:
                if has_personality_content(record.get("personality")):
                    continue
                combination = str(record.get("combination") or "")
                for key in _combination_match_keys(combination, str(record.get("speaker_id") or "")):
                    personality = legacy_map.get(key)
                    if personality:
                        record["personality"] = personality
                        hydrated += 1
                        break
            if hydrated:
                logger.warning(
                    f"character_prompts 尚未升级 personality Contract，"
                    f"已从 characters 只读回退 {hydrated} 条；建议重跑 Step 8 --force"
                )
        return result
    except Exception as e:
        logger.warning(f"读取 combination prompt records 失败: {e}")
        return {}


def load_combination_emotions(task_id: str) -> Dict[str, List[str]]:
    """Backward-compatible emotion lookup derived from full prompt records."""
    result: Dict[str, List[str]] = {}
    for key, record in load_combination_prompt_records(task_id).items():
        emotions = record.get("emotions", [])
        if isinstance(emotions, list) and emotions:
            result[key] = emotions
    return result


def filename_to_combination(filename_stem: str) -> str:
    """
    从 calm 文件名 stem 还原 combination 字符串。
    '萧无尘_1-1-1_calm' -> '萧无尘-1-1-1'
    'AnWei_1-1-1_calm'  -> 'AnWei-1-1-1'
    旧格式 'Harlow_1_calm' -> '' (需要外部通过 DB 补全)
    """
    for shot in ("CS", "MS", "LS"):
        if filename_stem.endswith(f"_{shot}"):
            filename_stem = filename_stem[:-(len(shot) + 1)]
            break
    if filename_stem.endswith("_calm"):
        stem = filename_stem[:-5]
    else:
        stem = filename_stem
    idx = stem.rfind("_")
    if idx == -1:
        return ""
    name = stem[:idx]
    ids = stem[idx + 1:]  # 新格式 "1-1-1"，旧格式 "1"
    # 旧格式：ids 是纯数字（无连字符），无法还原完整 combination
    if "-" not in ids:
        return ""
    return f"{name}-{ids}"


def load_name_to_combination_map(task_id: str) -> Dict[str, str]:
    """
    从 character_prompts 表构建 {character_name: combination} 映射。
    当一个角色有多个 combination 时，取数字最小的那个（与 generate_calm 一致）。
    key 同时包含原始名和归一化驼峰名（chinese_to_camel_case），
    以便用文件名里的英文驼峰角色名也能命中。
    """
    try:
        from sqlalchemy import text as sa_text
        with platform_engine.connect() as conn:
            row = conn.execute(
                sa_text("SELECT data FROM character_prompts WHERE task_id = :tid"),
                {"tid": task_id}
            ).fetchone()
        if not row:
            return {}
        result: Dict[str, str] = {}

        def _better(combo_a: str, combo_b: str) -> str:
            """返回 (p,b,c) 数字更小的 combination。"""
            pa = combo_a.rsplit("-", 3)
            pb = combo_b.rsplit("-", 3)
            try:
                ka = (int(pa[1]), int(pa[2]), int(pa[3]))
                kb = (int(pb[1]), int(pb[2]), int(pb[3]))
            except (ValueError, IndexError):
                return combo_a
            return combo_a if ka <= kb else combo_b

        for raw_item in row[0]:
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            combo = item.get("combination", "")
            if not combo:
                continue
            # 解析 name 和 (p, b, c)
            parts = combo.rsplit("-", 3)
            if len(parts) != 4:
                continue
            name = parts[0]
            try:
                int(parts[1]);
                int(parts[2]);
                int(parts[3])
            except ValueError:
                continue
            # 同时用原始名和归一化驼峰名作为 key
            resolved_id = resolve_authoritative_speaker_id(
                name,
                item.get("speaker_id") or "",
            )
            keys = {name, resolved_id or str(item.get("speaker_id") or "").strip()}
            keys.discard("")
            try:
                keys.add(chinese_to_camel_case(name))
            except Exception:
                pass
            for k in keys:
                if k not in result:
                    result[k] = combo
                else:
                    result[k] = _better(result[k], combo)
        return result
    except Exception as e:
        logger.warning(f"读取 name->combination 映射失败: {e}")
        return {}


def find_calm_images(base_dir: str) -> List[Tuple[Path, str]]:
    """
    在基础目录中查找所有 calm.png 文件。
    返回 (calm_image_path, character_name)，character_name 仅用于日志。
    """
    calm_images = []
    base_path = Path(base_dir)
    if not base_path.exists():
        logger.error(f"基础目录不存在: {base_dir}")
        return calm_images

    calm_files = list(base_path.glob("*_calm_LS.png"))
    # 兼容尚未迁移的旧文件。
    calm_files.extend(base_path.glob("*_calm.png"))
    for calm_file in calm_files:
        stem = calm_file.stem  # e.g. "萧无尘_1-1-1_calm"
        # 取第一个 _ 之前的部分作为 character_name（仅用于日志）
        idx = stem.find("_")
        character_name = stem[:idx] if idx != -1 else stem.replace("_calm", "")
        calm_images.append((calm_file, character_name))
        logger.debug(f"找到 calm 图片: {calm_file}")

    logger.info(f"在目录 {base_dir} 中共找到 {len(calm_images)} 个 calm 图片文件")
    return calm_images


def cleanup_emotion_files(output_dir: Path):
    """
    清理输出目录中的情绪图片文件（保留 calm.png）

    Args:
        output_dir: 输出目录路径
    """
    emotion_suffixes = ['happy', 'sad', 'surprised', 'fearful', 'angry', 'disgusted']

    if not output_dir.exists():
        logger.info(f"输出目录不存在，无需清理: {output_dir}")
        return

    deleted_count = 0
    for file_path in output_dir.glob("*.png"):
        if file_path.is_file():
            filename = file_path.name
            # 检查是否是情绪文件（以情绪后缀结尾，但不是calm）
            for emotion in emotion_suffixes:
                if filename.endswith(f"_{emotion}_LS.png") or filename.endswith(f"_{emotion}.png"):
                    try:
                        file_path.unlink()  # 删除文件
                        logger.info(f"已删除情绪文件: {filename}")
                        deleted_count += 1
                        break  # 找到匹配，跳出内层循环
                    except Exception as e:
                        logger.error(f"删除文件失败 {filename}: {e}")

    logger.info(f"清理完成，共删除 {deleted_count} 个情绪文件")


class EmotionImageGenerator:
    """情绪图片生成器"""

    def __init__(
            self,
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
            style_plan: Optional[AssetStylePlan] = None,
    ):
        self.image_provider = image_provider
        self.model = model or ("qwen-image-edit-plus" if image_provider == "qwen" else "gpt-image-2")
        self.style_plan = style_plan
        self.output_policy = resolve_character_output_policy(
            style_plan.task_id if style_plan is not None else "standalone",
            image_provider=self.image_provider,
            model=self.model,
            requested_background=background,
            requested_output_format=output_format,
        )
        self.asset_validator = validator_for_policy(self.output_policy)
        self.config = self._load_config(api_key)

        if image_provider == "qwen":
            self.provider = QwenImageEditProvider(
                api_key=api_key,
                model=self.model,
                size=size or "1140*1472",
                n=self.config['n'],
                watermark=self.config['watermark'],
                prompt_extend=self.config['prompt_extend'],
                negative_prompt=self.config['negative_prompt'],
                max_retries=self.config['max_retries'],
                retry_delay=self.config['retry_delay'],
            )
        else:
            gpt_api_key = api_key or os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
            self.provider = GPTImageEditProvider(
                api_key=gpt_api_key,
                base_url=base_url or os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"),
                model=self.model,
                size=size or "1024x1536",
                quality=quality,
                n=self.config['n'],
                response_format=response_format,
                output_format=self.output_policy.output_format,
                background=self.output_policy.background,
                moderation=moderation,
                timeout_seconds=timeout_seconds,
                max_retries=self.config['max_retries'],
                retry_delay=self.config['retry_delay'],
                transparent_character_output=self.output_policy.native_transparency,
            )

        logger.info(
            "情绪图片生成器初始化完成，provider=%s model=%s output_contract=%s",
            self.image_provider,
            self.model,
            self.output_policy.digest_payload(),
        )

    def _load_config(self, api_key: Optional[str] = None) -> Dict[str, Any]:
        """加载配置"""
        # 从参数或环境变量获取API密钥
        qwen_api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if self.image_provider == "qwen" and not qwen_api_key:
            logger.error("API Key 未设置，请设置 DASHSCOPE_API_KEY 环境变量或通过参数传递")
            raise ValueError("API Key 未设置")

        # 默认配置
        config = {
            # Qwen API 配置
            "qwen_api_key": qwen_api_key,
            "model": "qwen-image-edit-plus",  # 使用图生图模型
            "base_http_api_url": "https://dashscope.aliyuncs.com/api/v1",

            # 输出配置（由外部参数指定）
            "output_dir": "",

            # 图像生成配置
            "n": 1,  # 每次生成1张图片
            "watermark": False,
            "prompt_extend": True,
            "negative_prompt": "Keep everything else unchanged",

            # 处理配置
            "max_retries": 3,
            "retry_delay": 5,
            "timeout_seconds": 120,

            # 批量处理配置
            "batch_size": 1,
            "delay_between_batches": 10
        }

        return config

    def generate_emotion_image(self, input_image_path: str, prompt: str,
                               output_image_path: str, character_name: str = "") -> bool:
        """
        生成情绪图片

        Args:
            input_image_path: 输入图片路径（calm.png）
            prompt: 提示词
            output_image_path: 输出图片路径
            character_name: 角色名称（用于日志显示）

        Returns:
            bool: 是否成功
        """
        try:
            input_path = Path(input_image_path)
            output_path = Path(output_image_path)
            final_prompt = prompt
            with atomic_image_output(
                output_path,
                artifact_validator=self.asset_validator,
            ) as temporary_path:
                if self.image_provider == "qwen":
                    image_url = self.provider.generate(input_path, prompt)
                    if image_url and _usage_tracker:
                        _usage_tracker.add_image(
                            1, self.model, provider=self.image_provider
                        )
                    success = bool(
                        image_url and self.provider.download(image_url, temporary_path)
                    )
                else:
                    data_items, final_prompt = execute_with_prompt_repair(
                        lambda repaired_prompt: self.provider.edit(input_path, repaired_prompt),
                        prompt,
                        generation_mode="image_to_image",
                        stage=f"generate_emotion:{character_name}",
                    )
                    if data_items and _usage_tracker:
                        _usage_tracker.add_image(
                            len(data_items), self.model, provider=self.image_provider
                        )
                    success = bool(
                        data_items
                        and self.provider.save_image_item(data_items[0], temporary_path)
                    )
                    final_prompt = str(
                        getattr(self.provider, "last_prompt", final_prompt)
                    )

                if not success:
                    raise CharacterImageArtifactError(
                        f"Provider 未生成有效情绪图片: {output_path}"
                    )

            logger.info(f"[{character_name}] 情绪图片保存成功: {output_image_path}")
            transparency_metrics = (
                inspect_native_transparency(output_path)
                if self.output_policy.require_transparency
                else {}
            )
            if self.style_plan is not None:
                record_visual_generation(
                    self.style_plan.task_id,
                    "characters",
                    output_path,
                    prompt=final_prompt,
                    generation_mode="image_to_image",
                    reference_images=[
                        {"path": input_path, "role": "同组合 calm 基础立绘"}
                    ],
                    logic=[
                        "情绪立绘以同一组合的 calm 立绘做图生图。",
                        "动态情绪提示词只调整表情与微动作，参考图保持人物身份、服饰和构图连续性。",
                    ],
                    model=self.model,
                    provider=self.image_provider,
                    extra={
                        "character": character_name,
                        "character_output_policy": self.output_policy.digest_payload(),
                        "transparency_metrics": transparency_metrics,
                    },
                )
            return True
        except Exception as e:
            logger.error(f"[{character_name}] 生成情绪图片异常: {e}")
        return False

    def _download_image(self, image_url: str, output_path: str, character_name: str = "") -> bool:
        """
        下载图片

        Args:
            image_url: 图片URL
            output_path: 输出路径
            character_name: 角色名称（用于日志显示）

        Returns:
            bool: 是否成功
        """
        try:
            response = requests.get(image_url, timeout=30, stream=True)
            if response.status_code == 200:
                with open(output_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                logger.info(f"[{character_name}] 图片下载成功: {output_path}")
                return True
            else:
                logger.error(f"[{character_name}] 下载图片失败 (HTTP {response.status_code})")
                return False
        except Exception as e:
            logger.error(f"[{character_name}] 下载图片异常: {e}")
            return False

    def process_character(self, calm_image_path: Path, character_name: str,
                          output_dir: Path, emotions: List[str] = None,
                          output_stem: str = None,
                          prompt_record: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        处理单个角色，生成所有情绪图片

        Args:
            calm_image_path: calm图片路径
            character_name: 角色名（英文驼峰格式）
            output_dir: 输出目录（应与calm图片在同一时间戳目录）

        Returns:
            Dict[str, Any]: 处理结果
        """
        results = {
            "character": character_name,
            "calm_image": str(calm_image_path),
            "emotions": {},
            "success": 0,
            "failed": 0,
            "emotion_prompt_contract_version": EMOTION_PROMPT_CONTRACT_VERSION,
        }
        personality = normalize_personality(
            prompt_record.get("personality") if isinstance(prompt_record, dict) else None
        )
        results["personality_id"] = personality.get("personality_id")
        if not has_personality_content(personality):
            logger.warning(
                f"[{character_name}] 未找到组合级 personality，"
                "本次表情将使用自然克制的 legacy 回退提示词"
            )

        # 确保输出目录存在
        output_dir.mkdir(parents=True, exist_ok=True)

        # 确定情绪列表
        if emotions is None:
            emotions = EMOTIONS

        # 为每种情绪生成图片。该兼容入口保持串行；主批处理会把每个
        # emotion 展开成独立同层任务，以充分利用 Provider 并发。
        for emotion in emotions:
            emotion_result = self.process_emotion(
                calm_image_path,
                character_name,
                output_dir,
                emotion,
                output_stem=output_stem,
                personality=personality,
            )
            results["emotions"][emotion] = emotion_result
            if emotion_result["status"] in {"success", "skipped"}:
                results["success"] += 1
            else:
                results["failed"] += 1

        return results

    def process_emotion(
        self,
        calm_image_path: Path,
        character_name: str,
        output_dir: Path,
        emotion: str,
        *,
        output_stem: Optional[str] = None,
        personality: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate exactly one emotion variant from an already published calm."""
        personality = normalize_personality(personality)
        logger.info(f"为角色 {character_name} 生成 {emotion} 情绪图片")
        if output_stem:
            output_filename = f"{output_stem}_{emotion}_LS.png"
        else:
            calm_stem = calm_image_path.stem
            if calm_stem.endswith("_LS"):
                calm_stem = calm_stem[:-3]
            base_stem = calm_stem[:-5] if calm_stem.endswith("_calm") else calm_stem
            output_filename = f"{base_stem}_{emotion}_LS.png"
        output_image_path = output_dir / output_filename
        emotion_prompt = modify_prompt_for_emotion(
            "",
            emotion,
            personality=personality,
        )
        modified_prompt = (
            apply_asset_style(self.style_plan, emotion_prompt)
            if self.style_plan is not None
            else emotion_prompt
        )
        if not validate_image_file(calm_image_path):
            return {
                "status": "failed",
                "path": str(output_image_path),
                "prompt": modified_prompt,
                "error": f"calm 基础立绘不存在、为空或损坏: {calm_image_path}",
            }
        task_id = self.style_plan.task_id if self.style_plan is not None else ""
        asset_validator = self.asset_validator
        asset_id = f"{output_stem or character_name}:{emotion}"
        dependency_digest = canonical_digest(
            {
                "calm_path": calm_image_path.name,
                "calm_sha256": file_sha256(calm_image_path),
            }
        )
        checkpoint_digest = canonical_digest(
            {
                "emotion": emotion,
                "prompt": modified_prompt,
                "personality": personality,
                "prompt_contract_version": EMOTION_PROMPT_CONTRACT_VERSION,
                "provider": self.image_provider,
                "model": self.model,
                "output_contract": self.output_policy.digest_payload(),
                "style": {
                    "mode": self.style_plan.mode if self.style_plan else "",
                    "style": self.style_plan.style if self.style_plan else "",
                    "anchor_sha256": (
                        self.style_plan.anchor_sha256 if self.style_plan else ""
                    ),
                },
            }
        )
        if task_id:
            decision = reusable_or_adopt_existing(
                task_id,
                "char_designer",
                "generate_emotion",
                asset_id,
                input_digest=checkpoint_digest,
                generator_version=EMOTION_GENERATOR_VERSION,
                dependency_digest=dependency_digest,
                output_paths=[
                    character_resume_source_path(
                        task_id, output_image_path, fetch_remote=True
                    )
                ],
                validator=asset_validator,
            )
            if decision.reusable:
                restore_character_working_copy(task_id, output_image_path)
                logger.info(
                    "复用有效情绪图片: %s (%s)",
                    output_image_path,
                    decision.reason,
                )
                return {
                    "status": "skipped",
                    "path": str(output_image_path),
                    "prompt": modified_prompt,
                }
            assert_retry_budget(
                task_id,
                "char_designer",
                "generate_emotion",
                asset_id,
                input_digest=checkpoint_digest,
                generator_version=EMOTION_GENERATOR_VERSION,
                dependency_digest=dependency_digest,
            )
            checkpoint_mark_running(
                task_id,
                "char_designer",
                "generate_emotion",
                asset_id,
                input_digest=checkpoint_digest,
                generator_version=EMOTION_GENERATOR_VERSION,
                dependency_digest=dependency_digest,
            )
        logger.info(
            f"[{character_name}] emotion={emotion} "
            f"personality_id={personality.get('personality_id')} prompt={modified_prompt}"
        )
        success = self.generate_emotion_image(
            str(calm_image_path),
            modified_prompt,
            str(output_image_path),
            character_name,
        )
        if success:
            if task_id:
                resume_source = publish_character_resume_source(
                    task_id, output_image_path
                )
                checkpoint_mark_success(
                    task_id,
                    "char_designer",
                    "generate_emotion",
                    asset_id,
                    input_digest=checkpoint_digest,
                    generator_version=EMOTION_GENERATOR_VERSION,
                    dependency_digest=dependency_digest,
                    output_paths=[resume_source],
                    validator=asset_validator,
                )
            logger.info(f"[{character_name}] ✓ 情绪 {emotion} 图片生成成功")
            return {
                "status": "success",
                "path": str(output_image_path),
                "prompt": modified_prompt,
            }
        logger.error(f"[{character_name}] ✗ 情绪 {emotion} 图片生成失败")
        if task_id:
            checkpoint_mark_failed(
                task_id,
                "char_designer",
                "generate_emotion",
                asset_id,
                input_digest=checkpoint_digest,
                generator_version=EMOTION_GENERATOR_VERSION,
                dependency_digest=dependency_digest,
                error="Provider 未生成有效情绪图片",
            )
        return {
            "status": "failed",
            "path": None,
            "prompt": modified_prompt,
        }


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="情绪图片生成器 - 基于已有的 calm.png 图片生成多情绪变体",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 使用任务ID生成情绪图片
  python generate_emotion.py --task-id my_task_001

  # 指定API密钥
  python generate_emotion.py --task-id my_task_002 --api-key sk-xxx

  # 使用指定输入目录时仍需 task-id，以读取 story_planner 的 global_style
  python generate_emotion.py --task-id my_task_001 --input-dir outputs/my_task_001/images/characters

  # 测试模式（只生成提示词，不调用API）
  python generate_emotion.py --task-id test_task --test

环境变量:
  - DASHSCOPE_API_KEY: Qwen API Key（必需）
        """
    )

    parser.add_argument("--task-id", type=str, required=False,
                        help="任务ID，用于构建输入输出路径: char_designer/outputs/{task_id}/images/characters（如果未提供 --input-dir 则必需）")
    parser.add_argument("--input-dir", type=str,
                        help="输入目录（包含 calm.png 文件的目录），如果提供则优先使用")
    parser.add_argument("--output-dir", type=str,
                        help="输出目录（已弃用，使用 --task-id 替代）")
    parser.add_argument("--api-key", type=str,
                        help="当前 image provider 使用的 API Key")
    parser.add_argument("--image-provider", choices=["qwen", "gpt"], default="gpt",
                        help="图片模型提供方，默认 gpt")
    parser.add_argument("--base-url",
                        default=os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"),
                        help="GPT Image 兼容接口 Base URL")
    parser.add_argument("--model", default=None,
                        help="图片模型名；qwen 默认 qwen-image-edit-plus，gpt 默认 gpt-image-2")
    parser.add_argument("--size", default=None,
                        help="图片尺寸；qwen 格式如 1140*1472，gpt 格式如 1024x1536")
    parser.add_argument("--quality", default="high",
                        help="GPT Image 质量：low / medium / high / auto")
    parser.add_argument("--response-format", default="",
                        help="GPT Image 返回格式：url / b64_json，留空则不传")
    parser.add_argument("--output-format", default="",
                        help="GPT Image 输出格式：png / jpeg / webp，留空则不传")
    parser.add_argument("--background", default="",
                        help="GPT Image 背景；正式人物透明合同会强制使用 transparent")
    parser.add_argument("--moderation", default="",
                        help="GPT Image 审核：auto / low，留空则不传")
    parser.add_argument("--timeout", type=int, default=300,
                        help="GPT Image 请求超时时间（秒）")
    parser.add_argument("--test", action="store_true",
                        help="测试模式：只生成提示词，不调用API")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="详细输出模式")
    parser.add_argument("--character", type=str,
                        help="只处理指定角色名的图片（英文驼峰格式或中文名）")
    parser.add_argument("--emotions", type=str,
                        help="只处理指定情绪，多个情绪用逗号分隔，例如 'happy,sad'")
    parser.add_argument("--limit", type=int, default=0,
                        help="限制处理的最大角色数（0表示无限制）")

    args = parser.parse_args()

    # 警告已弃用的参数（仅保留 --output-dir）
    if args.output_dir:
        logger.warning("--output-dir 参数已弃用，使用 --input-dir 或 --task-id 替代")

    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # 情绪生图也必须通过 task_id 读取任务级风格合同。
    if not args.task_id:
        logger.error("必须提供 --task-id；无法仅通过目录解析任务级风格合同")
        sys.exit(1)

    # 确定输入输出目录
    if args.input_dir:
        # 使用指定的输入目录
        base_dir = Path(args.input_dir)
        logger.info(f"使用指定的输入目录: {base_dir}")
    else:
        # 使用 task-id 构建目录（与 generate_clam.py 一致）
        base_dir = Path(OUTPUTS_PATH) / args.task_id / "images" / "characters"
        logger.info(f"使用任务ID构建目录: {base_dir}")

    # 确保目录存在
    base_dir.mkdir(parents=True, exist_ok=True)
    output_dir = base_dir
    logger.info(f"输入输出目录: {output_dir}")

    if _usage_tracker and args.task_id:
        _usage_tracker.set_context(
            args.task_id, "char_designer", force_new_run=True
        )

    # 从 character_prompts 表加载每个 combination 的完整 Contract（有 task_id 时）
    combo_prompt_records_map: Dict[str, Dict[str, Any]] = {}
    combo_emotions_map: Dict[str, List[str]] = {}
    name_to_combination_map: Dict[str, str] = {}
    _init_conversion_cache(args.task_id)
    combo_prompt_records_map = load_combination_prompt_records(args.task_id)
    style_plan = resolve_asset_style_plan(args.task_id, ASSET_CHARACTER)
    logger.info(
        "人物情绪图风格路由: mode=%s style=%s anchor=%s",
        style_plan.mode,
        style_plan.style,
        style_plan.anchor_path or "(none)",
    )
    for key, record in combo_prompt_records_map.items():
        emotions = record.get("emotions", [])
        if isinstance(emotions, list) and emotions:
            combo_emotions_map[key] = emotions
    name_to_combination_map = load_name_to_combination_map(args.task_id)
    if combo_prompt_records_map:
        logger.info(
            f"从数据库加载了 {len(combo_prompt_records_map)} 个 combination Contract 匹配键（含别名）"
        )
    else:
        logger.info("未找到 combination Contract，将对所有 calm 图使用 legacy 回退")

    # 不在生成前删除上一份有效情绪图；同名新图经完整性校验后原子替换。
    logger.info("保留已有情绪图片，按文件原子更新")

    # 查找所有 calm 图片
    calm_images = find_calm_images(str(base_dir))
    if not calm_images:
        logger.warning(
            f"未找到任何 calm 图片，可能是基础立绘资产全部失败，"
            f"将作为资产级 partial 返回: {base_dir}"
        )
        sys.exit(2)

    # 初始化生成器（测试模式除外）
    generator = None
    generators = None
    if not args.test:
        def build_emotion_generator():
            return EmotionImageGenerator(
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
                style_plan=style_plan,
            )

        try:
            generator = build_emotion_generator()
            generators = ThreadLocalResource(
                build_emotion_generator,
                initial=generator,
            )
        except ValueError as e:
            logger.error(f"初始化生成器失败: {e}")
            sys.exit(1)

    # 过滤 calm 图片
    if args.character:
        calm_images = [item for item in calm_images if args.character in item[1]]
        logger.info(f"过滤后剩余 {len(calm_images)} 个角色")

    if args.limit > 0:
        calm_images = calm_images[:args.limit]
        logger.info(f"限制处理前 {args.limit} 个角色")

    # --emotions 参数作为强制覆盖（优先级最高）
    force_emotions = None
    if args.emotions:
        emotion_list = [e.strip() for e in args.emotions.split(',')]
        force_emotions = [e for e in emotion_list if e in EMOTIONS]
        if not force_emotions:
            logger.warning("--emotions 参数无有效情绪，忽略")
            force_emotions = None
        else:
            logger.info(f"强制指定情绪: {force_emotions}")

    # 先只解析每张 calm 的情绪任务；解析不调用 Provider。整批解析完成后，
    # 同层任务才并发执行，确保所有情绪图都只依赖已经发布的 calm 基础图。
    all_results = []
    emotion_jobs = []
    for calm_image_path, character_name in calm_images:
        # 从文件名还原 combination。
        # 注意：文件名里的角色名已是最终形态（generate_calm 写入时已做过
        # chinese_to_camel_case），因此这里得到的 combination（如 'XiaoWuChen-1-1-1'）
        # 应原样与 combo_emotions_map 的 key 比对，绝不可再次归一化，
        # 否则会把已驼峰化的英文名（XiaoWuChen）破坏成 Xiaowuchen 导致再次失配。
        combination = filename_to_combination(calm_image_path.stem)

        # 兜底：若还原失败（旧格式 Harlow_1_calm），或该 combination 不在情绪配置 map 中，
        # 尝试用 DB 的 name->combination 映射重新解析。
        # 这一步把“非空但匹配不上”的情况也纳入重解析，避免误落入全量情绪分支。
        # Contract records are the primary lookup source.  ``emotions`` may
        # legitimately be empty (for example when --emotions forces a one-off
        # test), so using only combo_emotions_map here can lose personality.
        combination_lookup_map = combo_prompt_records_map or combo_emotions_map
        need_db_resolve = (not combination) or (
            combination_lookup_map and combination not in combination_lookup_map
        )
        if need_db_resolve and character_name in name_to_combination_map:
            db_combo = name_to_combination_map[character_name]
            db_norm = normalize_combination_key(db_combo)
            # 仅当 DB 解析结果确实能命中 Contract 时才采用，否则保留原值。
            if not combination_lookup_map or db_norm in combination_lookup_map:
                combination = db_norm
                logger.info(f"{character_name} 通过 DB 补全 combination: {combination}")

        # 直接从 calm 文件名推导 output_stem，与 generate_calm.py/_variant_calm.py 保持一致
        calm_stem = calm_image_path.stem
        if calm_stem.endswith("_LS"):
            calm_stem = calm_stem[:-3]
        if calm_stem.endswith("_calm"):
            output_stem = calm_stem[:-5]
        else:
            output_stem = calm_stem

        if force_emotions:
            target_emotions = force_emotions
        elif combination and combination in combo_emotions_map:
            target_emotions = [e for e in combo_emotions_map[combination] if e != "calm"]
            logger.info(f"{character_name} ({combination}) 情绪: {target_emotions}")
        elif combo_emotions_map:
            # 有情绪配置但该角色未命中：说明配置里确实没有该 combination，
            # 不应擅自生成全量情绪。跳过该角色以避免生成多余的 angry 等情绪。
            logger.warning(
                f"{character_name} (combination={combination}) 未在情绪配置中找到，"
                f"跳过以避免生成多余情绪。如需强制生成请使用 --emotions 参数。"
            )
            continue
        else:
            # 完全没有情绪配置（DB 不可用或未提供 task_id），回退全量情绪
            target_emotions = [e for e in EMOTIONS if e != "calm"]
            logger.info(f"{character_name} 无情绪配置，使用全量情绪: {target_emotions}")

        if not target_emotions:
            logger.info(f"跳过 {character_name}：无需生成情绪图（仅有 calm）")
            continue

        logger.info(f"处理角色: {character_name} combination={combination}")
        output_dir.mkdir(parents=True, exist_ok=True)
        prompt_record = combo_prompt_records_map.get(combination)
        personality = normalize_personality(
            prompt_record.get("personality") if isinstance(prompt_record, dict) else None
        )

        if args.test:
            for emotion in target_emotions:
                modified_prompt = apply_asset_style(
                    style_plan,
                    modify_prompt_for_emotion(
                        "",
                        emotion,
                        personality=personality,
                    ),
                )
                logger.info(f"  [{character_name}] {emotion}: {modified_prompt}")
            continue

        if not has_personality_content(personality):
            logger.warning(
                f"[{character_name}] 未找到组合级 personality，"
                "本次表情将使用自然克制的 legacy 回退提示词"
            )
        for emotion in target_emotions:
            emotion_jobs.append(
                (
                    calm_image_path,
                    character_name,
                    emotion,
                    output_stem,
                    personality,
                )
            )

    if not args.test:
        def process_emotion_job(job):
            calm_image_path, character_name, emotion, output_stem, personality = job
            thread_generator = generators.get()
            result = thread_generator.process_emotion(
                calm_image_path,
                character_name,
                output_dir,
                emotion,
                output_stem=output_stem,
                personality=personality,
            )
            return character_name, emotion, result

        flat_results = run_dependency_layer(
            emotion_jobs,
            process_emotion_job,
            concurrency=resolve_image_concurrency("CHAR_EMOTION_IMAGE_CONCURRENCY"),
            stage="character_emotion",
            task_id=args.task_id,
            label=lambda job: f"{job[1]}:{job[2]}",
            logger=logger,
        )
        grouped_results: Dict[str, Dict[str, Any]] = {}
        for character_name, emotion, result in flat_results:
            character_result = grouped_results.setdefault(
                character_name,
                {
                    "character": character_name,
                    "emotions": {},
                    "success": 0,
                    "failed": 0,
                    "emotion_prompt_contract_version": EMOTION_PROMPT_CONTRACT_VERSION,
                },
            )
            character_result["emotions"][emotion] = result
            if result["status"] in {"success", "skipped"}:
                character_result["success"] += 1
            else:
                character_result["failed"] += 1
        all_results = list(grouped_results.values())

    # 输出总结
    print("\n" + "=" * 60)
    print("处理完成!")
    print("=" * 60)

    if args.test:
        print("测试模式：只生成提示词，未调用API")
        print(f"共处理 {len(calm_images)} 个角色")
    else:
        total_success = sum(r["success"] for r in all_results)
        total_failed = sum(r["failed"] for r in all_results)
        total_emotions = total_success + total_failed

        print(f"总角色数: {len(all_results)}")
        print(f"总情绪图片数: {total_emotions}")
        print(f"成功: {total_success}")
        print(f"失败: {total_failed}")
        if total_emotions > 0:
            print(f"成功率: {total_success / total_emotions:.2%}")

        # 输出每个角色的结果
        print("\n各角色结果:")
        for results in all_results:
            success_count = results["success"]
            failed_count = results["failed"]
            print(f"  {results['character']}: {success_count}成功, {failed_count}失败")

    print("=" * 60)

    if _usage_tracker:
        _usage_tracker.flush()

    if args.test:
        return 0
    # The batch may contain a few provider failures; let the parent collect a
    # partial result and continue independent assets.
    return 2 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
