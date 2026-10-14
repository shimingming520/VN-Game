#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CG 图片生成器

读 cg 表并支持两种路径：
- cg-reference-v1：Image 1 为唯一主场景，Image 2..N 为逐人物身份参考，
  任务级 scene Anchor 固定在最后；required 参考缺失时不调用 Provider。
- 历史数据：只允许复用已完成的 CG；缺少结构化合同时默认禁止再用人物拼接图调用 Provider。
- 输出 = images/cg/<cg_id>.png（与普通场景图分目录保存）。

按 API_MODEL 自动路由：
- gpt-image-*    → GPTImageEditProvider / GPTImageTextProvider（私有部署）
- qwen-image-*   → QwenImageEditProvider / QwenImageTextProvider（DashScope）
  - qwen-image-edit / qwen-image-edit-plus / qwen-image-edit-max 仅图生图
  - qwen-image-2.0[-pro] 同时支持文/图生图

新 Contract 始终使用身份安全的图生图且不降级文生图；旧路径保留原有回退。

用法：
    python novel-game-factory/services/scene_painter/core/cg_generator.py --task-id <task_id>
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import dotenv
import requests
from sqlalchemy import text
from tqdm import tqdm

# 项目根 + service dir 加进 sys.path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
service_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if service_dir not in sys.path:
    sys.path.insert(0, service_dir)

from common.config import OUTPUTS_PATH
from common.cg_data import load_cg_contract
from common.image_concurrency import (
    ThreadLocalResource,
    resolve_image_concurrency,
    run_dependency_layer,
)
from common.visual_checkpoint import (
    assert_retry_budget,
    canonical_digest,
    file_sha256,
    mark_failed as checkpoint_mark_failed,
    mark_running as checkpoint_mark_running,
    mark_success as checkpoint_mark_success,
    reusable_or_adopt_existing,
)
from common.platform_db import platform_engine
from common.visual_style import (
    ASSET_SCENE,
    AssetStylePlan,
    apply_asset_style,
    compose_anchor_guided_prompt,
    resolve_asset_style_plan,
)
from common.visual_debug import record_visual_generation
try:
    from services.scene_painter.core.artifact_contract import (
        atomic_image_output,
        validate_image_file,
    )
except ImportError:
    from artifact_contract import atomic_image_output, validate_image_file  # type: ignore

try:
    from services.scene_painter.core.cg_prompt_repair import (
        CGPromptRepairError,
        CGPromptRepairResult,
        is_moderation_blocked,
        repair_cg_prompt,
        replacement_audit,
    )
except ImportError:  # pragma: no cover - 直接脚本运行时的兼容路径
    from cg_prompt_repair import (  # type: ignore
        CGPromptRepairError,
        CGPromptRepairResult,
        is_moderation_blocked,
        repair_cg_prompt,
        replacement_audit,
    )

try:
    from services.scene_painter.image_providers.gpt_image import (
        GPTImageEditProvider,
        GPTImageTextProvider,
    )
    from services.scene_painter.image_providers.qwen_image import (
        QwenImageEditProvider,
        QwenImageTextProvider,
    )
    from services.scene_painter.image_providers.qwen_config import (
        is_qwen_model as _is_qwen_model,
    )
    from services.scene_painter.image_providers.wan_image import WanImageTextProvider
    from services.scene_painter.image_providers.wan_image_25 import WanLegacyImageTextProvider
    from services.scene_painter.image_providers.wan_config import is_legacy_wan as _is_legacy_wan
except ImportError:
    from scene_painter.image_providers.gpt_image import (  # type: ignore
        GPTImageEditProvider,
        GPTImageTextProvider,
    )
    from scene_painter.image_providers.qwen_image import (  # type: ignore
        QwenImageEditProvider,
        QwenImageTextProvider,
    )
    from scene_painter.image_providers.qwen_config import (  # type: ignore
        is_qwen_model as _is_qwen_model,
    )
    from scene_painter.image_providers.wan_image import WanImageTextProvider  # type: ignore
    from scene_painter.image_providers.wan_image_25 import WanLegacyImageTextProvider  # type: ignore
    from scene_painter.image_providers.wan_config import is_legacy_wan as _is_legacy_wan  # type: ignore

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

dotenv.load_dotenv()

CG_WIDTH = 1792
CG_HEIGHT = 1008


def _is_dashscope_model(model: str) -> bool:
    name = (model or "").lower()
    return name.startswith("wan") or name.startswith("wanx") or _is_qwen_model(model)


CG_RENDER_SUFFIX = (
    ", 16:9 aspect ratio, cinematic widescreen composition, single decisive moment, "
    "visually powerful dramatic staging, strong emotional tension, dynamic coherent composition"
)

CG_CLEAN_STYLE_SUFFIX = (
    ", high clarity, coherent lighting, clear well-defined complete shapes, "
    "stable anatomy and spatial relationships"
)

CG_REFERENCE_CONTRACT_SCHEMA = "cg-reference-v1"
CG_MAX_REFERENCE_IMAGES = int(os.getenv("CG_MAX_REFERENCE_IMAGES", "8"))
CG_LEGACY_REFERENCE_FALLBACK_ENV = "CG_LEGACY_REFERENCE_FALLBACK_ENABLED"


def legacy_reference_fallback_enabled() -> bool:
    """Explicit emergency-only switch for historical contact-sheet CG generation."""
    return os.getenv(CG_LEGACY_REFERENCE_FALLBACK_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class ProcessResult:
    cg_id: str
    success: bool
    image_path: Optional[str] = None
    error_message: Optional[str] = None
    used_reference: bool = False
    generation_time: float = 0.0
    retryable: bool = True


class CGReferenceContractBlocked(RuntimeError):
    """Required local reference files are missing; Provider retries cannot help."""


class CGModerationRepairFailed(RuntimeError):
    """CG 审核拒绝且最小提示词修复未能安全完成。"""


# ============================================================================
# 图片生成器（图生图 + 文生图）
#
# 按 model 名路由：
#   gpt-image-*  → GPT Image 私有部署（OpenAI 风格 multipart）
#   qwen-image-* → DashScope 千问图像（同步多模态）
# ============================================================================


class CGImageGenerator:
    """CG 专用 Provider 路由，支持确定顺序的单图或多图编辑。

    内部封装两套 provider，文/图生图按 model 名自动选择具体实现。文生图回退
    在 ``qwen-image-edit*`` 这种"只能图生图"的模型上不可用，调用方应保证
    至少有一张参考图。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 300,
        model: str = "gpt-image-2",
        size: str = "1792x1008",
        quality: str = "high",
        max_retries: int = 3,
        enable_qwen_edit_fallback: bool = False,
    ):
        self.size = size
        self.model = model
        self.api_max_retries = max_retries
        self._is_qwen = _is_qwen_model(model)
        self._is_wan = _is_dashscope_model(model) and not self._is_qwen
        self.max_reference_images = min(
            CG_MAX_REFERENCE_IMAGES,
            3 if self._is_qwen or self._is_wan else CG_MAX_REFERENCE_IMAGES,
        )
        self._edit_provider = None

        if self._is_qwen:
            # 与 scene_generator 完全一致：API_MODEL 决定文生图 provider，
            # QWEN_VARIANT_MODEL 决定图生图 provider，尺寸分别走各自环境变量。
            self._edit_provider = QwenImageEditProvider(
                api_key=api_key,
                base_url=base_url,
                model=os.getenv("QWEN_VARIANT_MODEL", "qwen-image-edit-plus"),
                size=os.getenv("QWEN_EDIT_SIZE", os.getenv("QWEN_IMAGE_SIZE", "1664*928")),
                timeout_seconds=timeout,
                max_retries=max_retries,
            )
            self._text_provider = QwenImageTextProvider(
                api_key=api_key,
                base_url=base_url,
                model=model,
                size=os.getenv("QWEN_IMAGE_SIZE", "1664*928"),
                timeout_seconds=timeout,
                max_retries=max_retries,
            )
        elif self._is_wan:
            wan_cls = WanLegacyImageTextProvider if _is_legacy_wan(model) else WanImageTextProvider
            self._text_provider = wan_cls(
                api_key=api_key,
                base_url=base_url,
                model=model,
                size=size,
                timeout_seconds=timeout,
                max_retries=max_retries,
            )
            if enable_qwen_edit_fallback:
                edit_api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
                if not edit_api_key:
                    raise ValueError(
                        "Wan 的自定义风格 CG 图生图需要 "
                        "GPT_IMAGE_API_KEY / OPENAI_API_KEY"
                    )
                self._edit_provider = QwenImageEditProvider(
                    api_key=edit_api_key,
                    base_url=os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"),
                    model=os.getenv("QWEN_VARIANT_MODEL", "qwen-image-edit-plus"),
                    size=os.getenv("QWEN_EDIT_SIZE", "1664*928"),
                    timeout_seconds=timeout,
                    max_retries=max_retries,
                )
        else:
            self._text_provider = GPTImageTextProvider(
                api_key=api_key,
                base_url=base_url,
                model=model,
                size=size,
                quality=quality,
                timeout_seconds=timeout,
                max_retries=max_retries,
            )
            self._edit_provider = GPTImageEditProvider(
                api_key=api_key,
                base_url=base_url,
                model=model,
                size=size,
                quality=quality,
                timeout_seconds=timeout,
                max_retries=max_retries,
            )

    def _save_first_item(self, items: List[Dict[str, Any]], output_path: Path) -> bool:
        if not items:
            return False
        item = items[0]
        provider = self._edit_provider or self._text_provider
        with atomic_image_output(output_path) as temporary_path:
            if hasattr(provider, "save_image_item"):
                if not provider.save_image_item(item, temporary_path):
                    raise RuntimeError("Provider 图片保存失败")
            elif item.get("url"):
                response = requests.get(item["url"], timeout=120)
                response.raise_for_status()
                temporary_path.write_bytes(response.content)
            elif item.get("b64_json"):
                temporary_path.write_bytes(base64.b64decode(item["b64_json"]))
            else:
                raise RuntimeError("图片响应缺少 url 和 b64_json")
        return True

    def supports_text_only(self) -> bool:
        """是否能在没有参考图时回退到文生图。``qwen-image-edit*`` 系列不支持。"""
        return self._text_provider is not None

    def generate_with_reference(self, prompt: str, reference_image: Path, output_path: Path) -> bool:
        if self._edit_provider is None:
            raise RuntimeError(f"模型 {self.model} 与 scene_generator 一样不支持图生图")
        items = self._edit_provider.edit(reference_image, prompt)
        return self._save_first_item(items, output_path)

    def generate_with_references(self, prompt: str, reference_images: List[Path], output_path: Path) -> bool:
        """按调用方给出的确定顺序上传多张内容/身份/风格参考图。"""
        if self._edit_provider is None:
            raise RuntimeError(f"模型 {self.model} 与 scene_generator 一样不支持图生图")
        items = self._edit_provider.edit_multi(reference_images, prompt)
        return self._save_first_item(items, output_path)

    def generate_text_only(self, prompt: str, output_path: Path) -> bool:
        if self._text_provider is None:
            raise RuntimeError(
                f"模型 {self.model} 仅支持图生图，无法在没有参考图时生成 CG"
            )
        items = self._text_provider.generate(prompt)
        return self._save_first_item(items, output_path)


# ============================================================================
# DB 读写
# ============================================================================

def fetch_cg_from_db(task_id: Optional[str]) -> tuple:
    """返回 (task_id, cg_data)。task_id 不传则取 cg 表最新一条。"""
    if task_id:
        return task_id, load_cg_contract(task_id)
    with platform_engine.connect() as conn:
        row = conn.execute(
            text("SELECT task_id, data FROM cg ORDER BY id DESC LIMIT 1")
        ).fetchone()
    if not row:
        return None, {}
    found_task_id = row[0]
    data = row[1] if isinstance(row[1], dict) else json.loads(row[1])
    return found_task_id, (data or {})


# ============================================================================
# 主处理器
# ============================================================================

class CGProcessor:
    """对照 SceneProcessor：单张 CG 处理 + 兜底重试。"""

    def __init__(
        self,
        image_generator: CGImageGenerator,
        output_dir: Path,
        char_dir: Path,
        scene_dir: Path,
        task_id: str,
        style_plan: AssetStylePlan,
        image_generator_factory: Optional[Callable[[], CGImageGenerator]] = None,
        prompt_repair_llm_client: Any = None,
    ):
        self._image_generators = ThreadLocalResource(
            image_generator_factory or (lambda: image_generator),
            initial=image_generator,
        )
        self.output_dir = output_dir
        self.char_dir = char_dir
        self.scene_dir = scene_dir
        self.task_id = task_id
        self.style_plan = style_plan
        self.task_dir = output_dir.parent.parent
        self.prompt_repair_llm_client = prompt_repair_llm_client
        self.results: List[ProcessResult] = []

    @property
    def image_generator(self) -> CGImageGenerator:
        """Return one Provider wrapper per worker to isolate mutable prompt state."""
        return self._image_generators.get()

    def process(self, cg_data: Dict[str, Any]) -> List[ProcessResult]:
        logging.info(f"开始处理 {len(cg_data)} 张 CG")
        items = list(cg_data.items())
        self.results = run_dependency_layer(
            items,
            lambda item: self._process_single(item[0], item[1]),
            concurrency=resolve_image_concurrency("CG_IMAGE_CONCURRENCY"),
            stage="cg",
            task_id=self.task_id,
            label=lambda item: str(item[0]),
            logger=logging.getLogger(__name__),
        )
        success = sum(1 for r in self.results if r.success)
        logging.info(f"处理完成，成功: {success}，失败: {len(self.results) - success}")
        return self.results

    @staticmethod
    def _existing_files(filenames: List[str], directory: Path) -> List[Path]:
        out: List[Path] = []
        for fn in filenames or []:
            p = directory / fn
            if validate_image_file(p):
                out.append(p)
        return out

    @staticmethod
    def _log_attachments(cg_id: str, label: str, declared: List[str], found: List[Path], directory: Path) -> None:
        """把某类附件的声明/命中/缺失情况打到终端，方便确认图生图是否调用到所需附件。"""
        if not declared:
            print(f"[{cg_id}] {label}: 未声明")
            return
        found_names = {p.name for p in found}
        missing = [fn for fn in declared if fn not in found_names]
        print(f"[{cg_id}] {label}: 声明 {len(declared)} 张，找到 {len(found)} 张 @ {directory}")
        for p in found:
            print(f"[{cg_id}]   ✓ {p.name}")
        for fn in missing:
            print(f"[{cg_id}]   ✗ 缺失 {fn}")
            logging.warning(f"[{cg_id}] {label}缺失: {fn}（期望位于 {directory}）")

    @staticmethod
    def _augment_prompt_with_scene_hint(prompt: str, scene_count: int) -> str:
        """在 cg_prompt 末尾补一句"场景图仅作氛围参考、不绑定构图"的硬指令。

        info_extractor 写出的 cg_prompt 已含画风/角色/构图/质量词。这里附加的句子
        提示模型："多张参考图里第一张锁人物，后面那 N 张只是环境/光照参考，
        不要复用其镜头、视角、聚焦主体"。仅在确实喂场景图时追加。
        """
        if scene_count <= 0:
            return prompt
        hint = (
            f", USE the {scene_count} attached scene reference image(s) ONLY for ambient style — "
            f"reuse their lighting, color palette, mist/lantern/material cues — "
            f"DO NOT copy their camera angle, framing, or focal subject; "
            f"the camera and composition are defined by this prompt"
        )
        return prompt.rstrip().rstrip(",") + hint

    def _compose_legacy_base_prompt(self, raw_prompt: str) -> str:
        prompt = apply_asset_style(self.style_plan, raw_prompt or "")
        if not prompt:
            raise ValueError("CG 没有 cg_prompt（请先跑 info_extractor --stage cg）")
        if "16:9 aspect ratio" not in prompt:
            prompt = prompt.rstrip().rstrip(",") + CG_RENDER_SUFFIX
        if "stable anatomy and spatial relationships" not in prompt:
            prompt = prompt.rstrip().rstrip(",") + CG_CLEAN_STYLE_SUFFIX
        return prompt

    @staticmethod
    def _prompt_repair_limit() -> int:
        try:
            return max(1, int(os.getenv("CG_PROMPT_REPAIR_MAX_ATTEMPTS", "1")))
        except (TypeError, ValueError):
            return 1

    def _prompt_repair_client(self) -> Any:
        """从当前图片 Provider 继承鉴权配置，避免 scene 镜像依赖其它服务。"""

        provider = self.image_generator
        for candidate in (
            getattr(provider, "_edit_provider", None),
            getattr(provider, "_text_provider", None),
        ):
            if candidate is None:
                continue
            provider_module = candidate.__class__.__module__
            if not (
                provider_module.endswith(".gpt_image")
                or provider_module.endswith(".qwen_image")
            ):
                continue
            api_key = str(getattr(candidate, "api_key", "") or "").strip()
            base_url = str(getattr(candidate, "base_url", "") or "").strip()
            if api_key:
                try:
                    from services.scene_painter.core.cg_prompt_repair import _OpenAIJsonClient
                except ImportError:  # pragma: no cover
                    from cg_prompt_repair import _OpenAIJsonClient  # type: ignore
                from openai import OpenAI

                if base_url and not base_url.rstrip("/").endswith("/v1"):
                    base_url = base_url.rstrip("/") + "/v1"
                kwargs: Dict[str, Any] = {
                    "api_key": api_key,
                    "max_retries": max(
                        0,
                        int(os.getenv("CG_PROMPT_REPAIR_LLM_MAX_RETRIES", "2")),
                    ),
                }
                if base_url:
                    kwargs["base_url"] = base_url
                model = os.getenv("CG_PROMPT_REPAIR_LLM_MODEL") or os.getenv(
                    "LLM_MODEL", "qwen3-max"
                )
                return _OpenAIJsonClient(OpenAI(**kwargs), model=model)
        return None

    @staticmethod
    def _prompt_contract_phrases(
        spec: Mapping[str, Any], prompt: str
    ) -> tuple[List[str], List[str]]:
        """分离严格不可变的身份/地点短语与需保持等义的剧情短语。"""

        immutable: List[str] = []
        semantic: List[str] = []
        immutable_keys = {
            "name",
            "entity_name",
            "entity_id",
            "scene_id",
            "location",
            "architecture",
        }
        semantic_keys = {
            "single_moment",
            "hard_constraints",
            "action",
            "placement",
            "expression",
            "body_language",
            "costume_state",
            "camera",
            "composition",
            "angle",
            "shot",
            "primary",
            "continuity",
            "mood",
        }

        def visit(value: Any, key: str = "") -> None:
            if isinstance(value, Mapping):
                for child_key, child_value in value.items():
                    visit(child_value, str(child_key))
                return
            if isinstance(value, (list, tuple)):
                for child in value:
                    visit(child, key)
                return
            if key not in immutable_keys | semantic_keys or not isinstance(value, str):
                return
            phrase = value.strip()
            if (
                2 <= len(phrase) <= 240
                and phrase in prompt
            ):
                (immutable if key in immutable_keys else semantic).append(phrase)

        visit(spec)
        def compact(values: List[str]) -> List[str]:
            unique = []
            for phrase in sorted(set(values), key=lambda item: (-len(item), item)):
                if not any(phrase in existing for existing in unique):
                    unique.append(phrase)
            return unique

        return compact(immutable), compact(semantic)

    def _run_with_prompt_repair(
        self,
        prompt: str,
        spec: Mapping[str, Any],
        operation: Callable[[str], Any],
        *,
        stage: str,
    ) -> tuple[Any, str, List[CGPromptRepairResult]]:
        """执行 Provider 调用；审核拒绝时只做有限的局部替换。"""

        current_prompt = prompt
        repairs: List[CGPromptRepairResult] = []
        immutable, protected = self._prompt_contract_phrases(spec, prompt)
        max_repairs = self._prompt_repair_limit()

        while True:
            try:
                return operation(current_prompt), current_prompt, repairs
            except Exception as exc:
                if not is_moderation_blocked(exc):
                    raise
                if len(repairs) >= max_repairs:
                    raise CGModerationRepairFailed(
                        f"{stage} 审核拒绝，已达到 CG 最小提示词修复上限 "
                        f"{max_repairs} 次；保留原始提示词，停止偏离原文的替代生成"
                    ) from exc
                try:
                    repair = repair_cg_prompt(
                        current_prompt,
                        rejection_reason=str(
                            getattr(exc, "reason", "") or str(exc)
                        ),
                        protected_phrases=protected,
                        immutable_phrases=immutable,
                        llm_client=(
                            self.prompt_repair_llm_client
                            or self._prompt_repair_client()
                        ),
                    )
                except CGPromptRepairError as repair_exc:
                    raise CGModerationRepairFailed(
                        f"{stage} 审核拒绝，最小提示词修复未通过校验: {repair_exc}"
                    ) from repair_exc
                except Exception as repair_exc:
                    raise CGModerationRepairFailed(
                        f"{stage} 审核拒绝，调用最小提示词修复服务失败: {repair_exc}"
                    ) from repair_exc
                repairs.append(repair)
                current_prompt = repair.repaired_prompt
                logging.warning(
                    "[%s] CG 审核拒绝，已应用最小提示词替换（第 %s/%s 次）: %s",
                    stage,
                    len(repairs),
                    max_repairs,
                    replacement_audit(repair),
                )

    @staticmethod
    def _repair_extra(
        original_prompt: str,
        final_prompt: str,
        repairs: List[CGPromptRepairResult],
    ) -> Dict[str, Any]:
        if not repairs:
            return {}
        return {
            "prompt_repair": {
                "mode": "minimal_substitution",
                "original_prompt": original_prompt,
                "final_prompt": final_prompt,
                "attempts": [
                    {
                        "rejection_reason": item.rejection_reason,
                        "repair_input_prompt": item.original_prompt,
                        "repair_output_prompt": item.repaired_prompt,
                        "replacements": replacement_audit(item),
                    }
                    for item in repairs
                ],
            }
        }

    def _safe_task_path(self, relative_path: str) -> Optional[Path]:
        value = str(relative_path or "").strip()
        if not value:
            return None
        path = Path(value)
        if path.is_absolute():
            return None
        resolved = (self.task_dir / path).resolve()
        try:
            resolved.relative_to(self.task_dir.resolve())
        except ValueError:
            return None
        return resolved

    def _resolve_structured_references(
        self, cg_id: str, spec: Mapping[str, Any]
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Resolve content references, enforce the limit, and append style last."""
        requirements = [
            dict(item)
            for item in spec.get("reference_requirements") or []
            if isinstance(item, Mapping)
            and str(item.get("role") or "") in {"scene", "character"}
        ]
        required_scenes = [
            item
            for item in requirements
            if item.get("role") == "scene" and item.get("required") is True
        ]
        if len(required_scenes) != 1:
            raise CGReferenceContractBlocked(
                f"CG {cg_id} 必须且只能声明 1 个 required 主场景，"
                f"实际为 {len(required_scenes)}"
            )
        role_priority = {"scene": 0, "character": 1}
        resolved: List[Dict[str, Any]] = []
        missing_required: List[Dict[str, Any]] = []
        omitted_optional: List[Dict[str, Any]] = []

        for order, requirement in enumerate(requirements):
            selected: Optional[Path] = None
            for candidate in requirement.get("file_candidates") or []:
                path = self._safe_task_path(str(candidate))
                if path is not None and validate_image_file(path):
                    selected = path
                    break
            if selected is None:
                diagnostic = {**requirement, "reason": "file_not_found"}
                if requirement.get("required"):
                    missing_required.append(diagnostic)
                else:
                    omitted_optional.append(diagnostic)
                continue
            resolved.append(
                {
                    **requirement,
                    "path": selected,
                    "_order": order,
                }
            )

        if missing_required:
            details = "; ".join(
                f"{item.get('entity_name') or item.get('entity_id')} -> "
                f"{item.get('file_candidates') or []}"
                for item in missing_required
            )
            raise CGReferenceContractBlocked(
                f"CG {cg_id} 缺少 required 参考图，已阻止 Provider 调用: {details}"
            )

        resolved.sort(
            key=lambda item: (
                role_priority.get(str(item.get("role") or ""), 1),
                0 if item.get("required") else 1,
                int(item.get("_order") or 0),
            )
        )
        required_content = [item for item in resolved if item.get("required")]
        optional_content = [item for item in resolved if not item.get("required")]

        style_reference: Optional[Dict[str, Any]] = None
        if self.style_plan.uses_anchor:
            anchor_path = self.style_plan.anchor_path
            if anchor_path is None or not validate_image_file(anchor_path):
                raise CGReferenceContractBlocked(
                    f"CG {cg_id} 的场景 Anchor 不可用，已阻止 Provider 调用"
                )
            style_reference = {
                "role": "style",
                "entity_id": "scene_anchor",
                "entity_name": "scene rendering style Anchor",
                "required": True,
                "path": anchor_path,
                "preserve": (
                    "rendering medium, linework, palette, shading, lighting, material treatment and texture only"
                ),
            }

        reserved = len(required_content) + (1 if style_reference else 0)
        reference_limit = self.image_generator.max_reference_images
        if reference_limit < 1 or reserved > reference_limit:
            raise CGReferenceContractBlocked(
                f"CG {cg_id} required 参考图数量 {reserved} 超过上限 "
                f"reference_limit={reference_limit}"
            )
        optional_slots = reference_limit - reserved
        omitted_optional.extend(
            {**item, "reason": "reference_limit"}
            for item in optional_content[optional_slots:]
        )
        selected_references = [
            *required_content,
            *optional_content[:optional_slots],
        ]
        if style_reference is not None:
            selected_references.append(style_reference)
        selected_references = [
            {**item, "image_index": index}
            for index, item in enumerate(selected_references, start=1)
        ]

        if not selected_references or selected_references[0].get("role") != "scene":
            raise CGReferenceContractBlocked(
                f"CG {cg_id} 的 Image 1 不是唯一主场景图，已阻止 Provider 调用"
            )
        return selected_references, omitted_optional

    @staticmethod
    def _structured_reference_instruction(reference: Mapping[str, Any]) -> str:
        index = reference.get("image_index")
        role = str(reference.get("role") or "")
        name = str(
            reference.get("entity_name") or reference.get("entity_id") or "reference"
        ).strip()
        preserve = str(reference.get("preserve") or "").strip()
        if role == "scene":
            return (
                f"Use Image {index} as the exact {name} location reference. "
                f"Preserve {preserve or 'location identity, architecture, spatial layout, materials, palette and lighting continuity'}. "
                "Do not copy its camera angle or focal composition; use the CG camera specified by the text."
            )
        if role == "style":
            return (
                f"Use Image {index} only as the final scene rendering-style Anchor. "
                f"Preserve {preserve}. Ignore its people, objects, location, architecture, pose and composition."
            )

        staging = "; ".join(
            value
            for value in (
                f"placement: {reference.get('placement')}" if reference.get("placement") else "",
                f"action: {reference.get('action')}" if reference.get("action") else "",
                f"expression: {reference.get('expression')}" if reference.get("expression") else "",
                f"body language: {reference.get('body_language')}" if reference.get("body_language") else "",
                f"costume state: {reference.get('costume_state')}" if reference.get("costume_state") else "",
            )
            if value
        )
        instruction = (
            f"Use Image {index} as the official {name} character identity reference. "
            f"Preserve {preserve or 'face, age, hairstyle, body proportions, base costume, palette and silhouette'}."
        )
        if staging:
            instruction += f" In this CG depict {name} with {staging}."
        return instruction

    def _compose_structured_prompt(
        self,
        spec: Mapping[str, Any],
        references: List[Dict[str, Any]],
        omitted_optional: List[Dict[str, Any]],
    ) -> str:
        prompt = apply_asset_style(self.style_plan, spec.get("cg_prompt") or "")
        if not prompt:
            raise ValueError("结构化 CG 缺少 cg_prompt")
        if "16:9 aspect ratio" not in prompt:
            prompt = prompt.rstrip().rstrip(",") + CG_RENDER_SUFFIX
        if "stable anatomy and spatial relationships" not in prompt:
            prompt = prompt.rstrip().rstrip(",") + CG_CLEAN_STYLE_SUFFIX

        mapping = "\n".join(
            self._structured_reference_instruction(reference)
            for reference in references
        )
        resolved_characters = [
            str(item.get("entity_name") or item.get("entity_id") or "").strip()
            for item in references
            if item.get("role") == "character"
        ]
        omitted_names = [
            str(item.get("entity_name") or item.get("entity_id") or "").strip()
            for item in omitted_optional
            if item.get("role") == "character"
        ]
        visual_spec = spec.get("cg_visual_spec") or {}
        offscreen_names = [
            str(item.get("name") or "").strip()
            for item in visual_spec.get("offscreen_characters") or []
            if isinstance(item, Mapping) and str(item.get("name") or "").strip()
        ]
        hard_rules = [
            "Reference-image mapping is strict and positional; never swap identities between images.",
            "Create one finished cinematic CG, not a collage, character sheet, contact sheet, split panel, or multiple-view layout.",
            "Show each referenced character at most once with stable anatomy and coherent spatial relationships.",
        ]
        if resolved_characters:
            hard_rules.append(
                "The only named characters allowed to appear are: "
                + ", ".join(resolved_characters)
                + ". Do not invent additional people."
            )
        if omitted_names:
            hard_rules.append(
                "Omit these optional characters because no usable identity reference was supplied: "
                + ", ".join(omitted_names)
                + ". This rule overrides any earlier prose mentioning them."
            )
        if offscreen_names:
            hard_rules.append(
                "Keep these characters offscreen: " + ", ".join(offscreen_names) + "."
            )
        return (
            f"{prompt}\n\nREFERENCE IMAGE CONTRACT:\n{mapping}\n\n"
            "HARD REFERENCE RULES:\n- " + "\n- ".join(hard_rules)
        )

    def _process_structured(self, cg_id: str, spec: Dict[str, Any]) -> ProcessResult:
        start = time.time()
        checkpoint_digest = ""
        dependency_digest = ""
        standard_task_root = Path(OUTPUTS_PATH).resolve() / self.task_id
        try:
            self.output_dir.resolve().relative_to(standard_task_root)
            checkpoint_enabled = True
        except ValueError:
            checkpoint_enabled = False
        try:
            references, omitted_optional = self._resolve_structured_references(cg_id, spec)
            raw_cg_prompt = str(spec.get("cg_prompt") or "").strip()
            prompt = self._compose_structured_prompt(spec, references, omitted_optional)
            original_provider_prompt = prompt
            safe_name = re.sub(r"[^\w\-]", "_", cg_id)
            output_path = self.output_dir / f"{safe_name}.png"
            reference_paths = [Path(item["path"]) for item in references]
            dependency_digest = canonical_digest(
                [
                    {
                        "role": item.get("role"),
                        "entity_id": item.get("entity_id"),
                        "image_index": item.get("image_index"),
                        "sha256": file_sha256(item["path"]),
                    }
                    for item in references
                ]
            )
            checkpoint_digest = canonical_digest(
                {
                    "cg_id": cg_id,
                    "prompt": prompt,
                    "reference_contract_schema": spec.get(
                        "reference_contract_schema"
                    ),
                    "visual_spec": spec.get("cg_visual_spec") or {},
                    "model": self.image_generator.model,
                    "style": {
                        "mode": self.style_plan.mode,
                        "style": self.style_plan.style,
                        "anchor_sha256": self.style_plan.anchor_sha256,
                    },
                }
            )
            if checkpoint_enabled:
                decision = reusable_or_adopt_existing(
                    self.task_id,
                    "scene_painter",
                    "generate_cg",
                    cg_id,
                    input_digest=checkpoint_digest,
                    generator_version="cg-image-v2-structured-resume",
                    dependency_digest=dependency_digest,
                    output_paths=[output_path],
                    validator=validate_image_file,
                )
                if decision.reusable:
                    logging.info("[%s] 复用有效结构化 CG (%s)", cg_id, decision.reason)
                    return ProcessResult(
                        cg_id=cg_id,
                        success=True,
                        image_path=str(output_path),
                        used_reference=True,
                        generation_time=time.time() - start,
                    )
                assert_retry_budget(
                    self.task_id,
                    "scene_painter",
                    "generate_cg",
                    cg_id,
                    input_digest=checkpoint_digest,
                    generator_version="cg-image-v2-structured-resume",
                    dependency_digest=dependency_digest,
                )
                checkpoint_mark_running(
                    self.task_id,
                    "scene_painter",
                    "generate_cg",
                    cg_id,
                    input_digest=checkpoint_digest,
                    generator_version="cg-image-v2-structured-resume",
                    dependency_digest=dependency_digest,
                )
            logging.info(
                "[%s] 新 CG Contract 参考图顺序: %s",
                cg_id,
                " | ".join(
                    f"Image {item['image_index']}={Path(item['path']).name}({item['role']})"
                    for item in references
                ),
            )

            provider_prompt = prompt

            def generate_structured(candidate_cg_prompt: str) -> bool:
                nonlocal provider_prompt
                candidate_spec = dict(spec)
                candidate_spec["cg_prompt"] = candidate_cg_prompt
                provider_prompt = self._compose_structured_prompt(
                    candidate_spec, references, omitted_optional
                )
                if len(reference_paths) == 1:
                    ok_result = self.image_generator.generate_with_reference(
                        provider_prompt, reference_paths[0], output_path
                    )
                else:
                    ok_result = self.image_generator.generate_with_references(
                        provider_prompt, reference_paths, output_path
                    )
                if not ok_result:
                    raise RuntimeError("结构化 CG 图生图保存失败")
                return True

            _, prompt, prompt_repairs = self._run_with_prompt_repair(
                raw_cg_prompt,
                spec,
                generate_structured,
                stage=f"cg:{cg_id}",
            )
            prompt = provider_prompt

            if prompt_repairs:
                logging.info(
                    "[%s] CG 采用最小提示词修复后生成；原始 CG prompt 保留，"
                    "替换记录=%s",
                    cg_id,
                    [replacement_audit(repair) for repair in prompt_repairs],
                )

            if _usage_tracker:
                _usage_tracker.add_image(1, self.image_generator.model)
            actual_references = [
                {
                    "path": item["path"],
                    "role": (
                        f"Image {item['image_index']} 主场景参考图"
                        if item["role"] == "scene"
                        else f"Image {item['image_index']} 场景风格 Anchor"
                        if item["role"] == "style"
                        else f"Image {item['image_index']} {item.get('entity_name')} 人物身份参考图"
                    ),
                }
                for item in references
            ]
            record_visual_generation(
                self.task_id,
                "cg",
                output_path,
                prompt=prompt,
                generation_mode="image_to_image",
                reference_images=actual_references,
                logic=[
                    "Image 1 固定为唯一主场景；不复制其镜头构图。",
                    "Image 2..N 为逐人物独立身份参考；不再使用人物拼接图。",
                    "场景 Anchor 存在时固定排在最后，只控制渲染风格。",
                    "required 参考图缺失时在 Provider 调用前阻断。",
                ],
                model=str(self.image_generator.model),
                provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
                extra={
                    "cg_id": cg_id,
                    "used_reference": True,
                    "reference_contract_schema": CG_REFERENCE_CONTRACT_SCHEMA,
                    "omitted_optional_references": [
                        {
                            "entity_name": item.get("entity_name"),
                            "reason": item.get("reason"),
                        }
                        for item in omitted_optional
                    ],
                    **self._repair_extra(
                        original_provider_prompt,
                        prompt,
                        prompt_repairs,
                    ),
                },
            )
            if checkpoint_enabled:
                checkpoint_mark_success(
                    self.task_id,
                    "scene_painter",
                    "generate_cg",
                    cg_id,
                    input_digest=checkpoint_digest,
                    generator_version="cg-image-v2-structured-resume",
                    dependency_digest=dependency_digest,
                    output_paths=[output_path],
                    validator=validate_image_file,
                    result_mode=(
                        "moderation_repaired"
                        if prompt_repairs
                        else "generated"
                    ),
                )
            return ProcessResult(
                cg_id=cg_id,
                success=True,
                image_path=str(output_path),
                used_reference=True,
                generation_time=time.time() - start,
            )
        except CGReferenceContractBlocked as exc:
            logging.error(str(exc))
            if checkpoint_enabled and checkpoint_digest:
                checkpoint_mark_failed(
                    self.task_id,
                    "scene_painter",
                    "generate_cg",
                    cg_id,
                    input_digest=checkpoint_digest,
                    generator_version="cg-image-v2-structured-resume",
                    dependency_digest=dependency_digest,
                    error=str(exc),
                )
            return ProcessResult(
                cg_id=cg_id,
                success=False,
                error_message=str(exc),
                generation_time=time.time() - start,
                retryable=False,
            )
        except CGModerationRepairFailed as exc:
            logging.error("结构化 CG %s 审核修复失败: %s", cg_id, exc)
            if checkpoint_enabled and checkpoint_digest:
                try:
                    checkpoint_mark_failed(
                        self.task_id,
                        "scene_painter",
                        "generate_cg",
                        cg_id,
                        input_digest=checkpoint_digest,
                        generator_version="cg-image-v2-structured-resume",
                        dependency_digest=dependency_digest,
                        error=str(exc),
                    )
                except Exception:
                    logging.exception("记录结构化 CG 审核修复失败 checkpoint 时发生异常")
            return ProcessResult(
                cg_id=cg_id,
                success=False,
                error_message=str(exc),
                generation_time=time.time() - start,
                retryable=False,
            )
        except Exception as exc:
            logging.error("处理结构化 CG 失败 %s: %s", cg_id, exc)
            if checkpoint_enabled and checkpoint_digest:
                try:
                    checkpoint_mark_failed(
                        self.task_id,
                        "scene_painter",
                        "generate_cg",
                        cg_id,
                        input_digest=checkpoint_digest,
                        generator_version="cg-image-v2-structured-resume",
                        dependency_digest=dependency_digest,
                        error=str(exc),
                    )
                except Exception:
                    logging.exception("记录结构化 CG 失败 checkpoint 时发生异常")
            return ProcessResult(
                cg_id=cg_id,
                success=False,
                error_message=str(exc),
                generation_time=time.time() - start,
                retryable=True,
            )

    def _process_single(self, cg_id: str, spec: Dict[str, Any]) -> ProcessResult:
        if spec.get("reference_contract_schema") == CG_REFERENCE_CONTRACT_SCHEMA:
            return self._process_structured(cg_id, spec)
        return self._process_legacy(cg_id, spec)

    def _process_legacy(self, cg_id: str, spec: Dict[str, Any]) -> ProcessResult:
        start = time.time()
        checkpoint_digest = ""
        dependency_digest = ""
        standard_task_root = Path(OUTPUTS_PATH).resolve() / self.task_id
        try:
            self.output_dir.resolve().relative_to(standard_task_root)
            checkpoint_enabled = True
        except ValueError:
            checkpoint_enabled = False
        try:
            raw_cg_prompt = str(spec.get("cg_prompt") or "").strip()
            prompt = self._compose_legacy_base_prompt(raw_cg_prompt)

            safe_name = re.sub(r"[^\w\-]", "_", cg_id)
            output_path = self.output_dir / f"{safe_name}.png"

            char_att = spec.get("char_img_att") or []
            scene_att = spec.get("scene_img_att") or []
            char_paths = self._existing_files(char_att, self.char_dir)
            scene_paths = self._existing_files(scene_att, self.scene_dir)

            # 打印附件调用明细：声明了哪些、磁盘上找到哪些、缺失哪些
            self._log_attachments(cg_id, "人物参考图", char_att, char_paths, self.char_dir)
            self._log_attachments(cg_id, "场景参考图", scene_att, scene_paths, self.scene_dir)

            # 参考图组装顺序：人物拼接图（锁人物）在前；场景图（锁氛围）在后
            # 单次最多取 1 张人物 + 2 张场景，控制 multipart 大小
            references: List[Path] = []
            if char_paths:
                references.append(char_paths[0])
            references.extend(scene_paths[:2])
            dependency_references = list(references)
            if self.style_plan.uses_anchor and self.style_plan.anchor_path is not None:
                dependency_references.append(self.style_plan.anchor_path)
            dependency_digest = canonical_digest(
                [
                    {"sha256": file_sha256(path)}
                    for path in dependency_references
                    if path is not None and validate_image_file(path)
                ]
            )
            checkpoint_digest = canonical_digest(
                {
                    "cg_id": cg_id,
                    "prompt": prompt,
                    "char_img_att": char_att,
                    "scene_img_att": scene_att,
                    "model": self.image_generator.model,
                    "style": {
                        "mode": self.style_plan.mode,
                        "style": self.style_plan.style,
                        "anchor_sha256": self.style_plan.anchor_sha256,
                    },
                }
            )
            if checkpoint_enabled:
                decision = reusable_or_adopt_existing(
                    self.task_id,
                    "scene_painter",
                    "generate_cg",
                    cg_id,
                    input_digest=checkpoint_digest,
                    generator_version="cg-image-v2-legacy-resume",
                    dependency_digest=dependency_digest,
                    output_paths=[output_path],
                    validator=validate_image_file,
                )
                if decision.reusable:
                    logging.info("[%s] 复用有效 legacy CG (%s)", cg_id, decision.reason)
                    return ProcessResult(
                        cg_id=cg_id,
                        success=True,
                        image_path=str(output_path),
                        used_reference=bool(references),
                        generation_time=time.time() - start,
                    )
                assert_retry_budget(
                    self.task_id,
                    "scene_painter",
                    "generate_cg",
                    cg_id,
                    input_digest=checkpoint_digest,
                    generator_version="cg-image-v2-legacy-resume",
                    dependency_digest=dependency_digest,
                )
                checkpoint_mark_running(
                    self.task_id,
                    "scene_painter",
                    "generate_cg",
                    cg_id,
                    input_digest=checkpoint_digest,
                    generator_version="cg-image-v2-legacy-resume",
                    dependency_digest=dependency_digest,
                )

            has_legacy_character_contract = bool(
                spec.get("char_img_att") or spec.get("characters")
            )
            if (
                has_legacy_character_contract
                and not legacy_reference_fallback_enabled()
            ):
                raise CGReferenceContractBlocked(
                    f"CG {cg_id} 仍是历史附件合同，已禁止使用人物拼接图继续生图；"
                    "请重跑 Info Extractor CG 子阶段生成 cg-reference-v1。"
                )

            if references:
                ref_names = " | ".join(p.name for p in references)
                print(f"[{cg_id}] 图生图附件（共 {len(references)} 张）: {ref_names}")
                logging.info(f"[{cg_id}] 图生图将使用 {len(references)} 张参考图: {ref_names}")
            else:
                print(f"[{cg_id}] 无可用参考图，走文生图")

            used_reference = False
            final_provider_prompt = prompt
            actual_reference_images: List[Dict[str, Any]] = []
            generation_mode = "text_to_image"
            generation_logic = ["CG 使用上游 CG 提示词直接文生图。"]
            prompt_repairs: List[CGPromptRepairResult] = []
            repair_original_prompt = prompt
            if references:
                try:
                    repair_original_prompt = self._augment_prompt_with_scene_hint(
                        prompt, len(scene_paths[:2])
                    )

                    def generate_legacy_reference(candidate_raw_prompt: str) -> bool:
                        nonlocal final_provider_prompt
                        candidate_prompt = self._augment_prompt_with_scene_hint(
                            self._compose_legacy_base_prompt(candidate_raw_prompt),
                            len(scene_paths[:2]),
                        )
                        final_provider_prompt = candidate_prompt
                        if len(references) == 1:
                            ok_result = self.image_generator.generate_with_reference(
                                candidate_prompt, references[0], output_path
                            )
                        else:
                            ok_result = self.image_generator.generate_with_references(
                                candidate_prompt, references, output_path
                            )
                        if not ok_result:
                            raise RuntimeError("图生图保存失败")
                        return True

                    _, _, prompt_repairs = self._run_with_prompt_repair(
                        raw_cg_prompt,
                        spec,
                        generate_legacy_reference,
                        stage=f"cg:{cg_id}:legacy-reference",
                    )
                    used_reference = True
                    generation_mode = "image_to_image"
                    actual_reference_images = []
                    if char_paths:
                        actual_reference_images.append(
                            {"path": char_paths[0], "role": "人物拼接参考图"}
                        )
                    actual_reference_images.extend(
                        {"path": path, "role": "场景氛围参考图"}
                        for path in scene_paths[:2]
                    )
                    generation_logic = [
                        "CG 优先使用人物拼接图锁定角色身份与服饰。",
                        "最多附加两张场景图约束环境光照、色彩与材质，不复制其镜头和构图。",
                    ]
                    logging.info(
                        f"[{cg_id}] 图生图完成（参考图 {len(references)} 张：人物 {1 if char_paths else 0} + 场景 {len(scene_paths[:2])}）"
                    )
                except CGModerationRepairFailed:
                    raise
                except Exception as e:
                    if self.style_plan.uses_anchor:
                        logging.warning(
                            f"[{cg_id}] 业务参考图生图失败，"
                            f"使用已确认场景 Anchor 重试: {e}"
                        )
                        anchor_prompt = compose_anchor_guided_prompt(self.style_plan, prompt)
                        repair_original_prompt = anchor_prompt
                        def generate_legacy_anchor(candidate_raw_prompt: str) -> bool:
                            nonlocal final_provider_prompt
                            candidate_prompt = compose_anchor_guided_prompt(
                                self.style_plan,
                                self._compose_legacy_base_prompt(candidate_raw_prompt),
                            )
                            final_provider_prompt = candidate_prompt
                            ok_result = self.image_generator.generate_with_reference(
                                candidate_prompt,
                                self.style_plan.anchor_path,
                                output_path,
                            )
                            if not ok_result:
                                raise RuntimeError("Anchor 图生图保存失败")
                            return True

                        _, _, prompt_repairs = self._run_with_prompt_repair(
                            raw_cg_prompt,
                            spec,
                            generate_legacy_anchor,
                            stage=f"cg:{cg_id}:anchor-fallback",
                        )
                        used_reference = True
                        generation_mode = "image_to_image"
                        actual_reference_images = [
                            {"path": self.style_plan.anchor_path, "role": "场景风格 Anchor"}
                        ]
                        generation_logic = [
                            "业务人物/场景参考图生图失败，改用已确认场景 Anchor 重试。",
                            "Anchor 仅用于保持视觉风格，CG 内容与构图由文字提示词决定。",
                        ]
                    else:
                        # qwen-image-edit* 系列没有文生图能力；让调用者直接看到图生图错误
                        if not self.image_generator.supports_text_only():
                            raise
                        logging.warning(f"[{cg_id}] 图生图失败，退化为文生图: {e}")
                        repair_original_prompt = prompt
                        def generate_legacy_text(candidate_raw_prompt: str) -> bool:
                            nonlocal final_provider_prompt
                            candidate_prompt = self._compose_legacy_base_prompt(candidate_raw_prompt)
                            final_provider_prompt = candidate_prompt
                            ok_result = self.image_generator.generate_text_only(
                                candidate_prompt, output_path
                            )
                            if not ok_result:
                                raise RuntimeError("文生图保存失败")
                            return True

                        _, _, prompt_repairs = self._run_with_prompt_repair(
                            raw_cg_prompt,
                            spec,
                            generate_legacy_text,
                            stage=f"cg:{cg_id}:text-fallback",
                        )
                        generation_mode = "text_to_image"
                        actual_reference_images = []
                        generation_logic = ["业务参考图生图失败，当前模型退化为 CG 文生图。"]
            else:
                if self.style_plan.uses_anchor:
                    anchor_prompt = compose_anchor_guided_prompt(self.style_plan, prompt)
                    repair_original_prompt = anchor_prompt
                    def generate_legacy_anchor_only(candidate_raw_prompt: str) -> bool:
                        nonlocal final_provider_prompt
                        candidate_prompt = compose_anchor_guided_prompt(
                            self.style_plan,
                            self._compose_legacy_base_prompt(candidate_raw_prompt),
                        )
                        final_provider_prompt = candidate_prompt
                        ok_result = self.image_generator.generate_with_reference(
                            candidate_prompt,
                            self.style_plan.anchor_path,
                            output_path,
                        )
                        if not ok_result:
                            raise RuntimeError("Anchor 图生图保存失败")
                        return True

                    _, _, prompt_repairs = self._run_with_prompt_repair(
                        raw_cg_prompt,
                        spec,
                        generate_legacy_anchor_only,
                        stage=f"cg:{cg_id}:anchor",
                    )
                    used_reference = True
                    generation_mode = "image_to_image"
                    actual_reference_images = [
                        {"path": self.style_plan.anchor_path, "role": "场景风格 Anchor"}
                    ]
                    generation_logic = [
                        "CG 没有可用业务参考图，使用已确认场景 Anchor 做图生图。",
                        "Anchor 只约束视觉风格，角色、事件和构图由 CG 提示词决定。",
                    ]
                    logging.info(
                        f"[{cg_id}] 无业务参考图，使用已确认场景 Anchor"
                    )
                else:
                    if (spec.get("char_img_att") or []) or (spec.get("scene_img_att") or []):
                        logging.warning(f"[{cg_id}] 全部参考图缺失，退化为文生图")
                    if not self.image_generator.supports_text_only():
                        raise RuntimeError(
                            f"模型 {self.image_generator.model} 仅支持图生图，"
                            f"但 CG {cg_id} 没有可用的参考图（char_img_att / scene_img_att 全部缺失）"
                        )
                    repair_original_prompt = prompt
                    def generate_legacy_text_only(candidate_raw_prompt: str) -> bool:
                        nonlocal final_provider_prompt
                        candidate_prompt = self._compose_legacy_base_prompt(candidate_raw_prompt)
                        final_provider_prompt = candidate_prompt
                        ok_result = self.image_generator.generate_text_only(
                            candidate_prompt, output_path
                        )
                        if not ok_result:
                            raise RuntimeError("文生图保存失败")
                        return True

                    _, _, prompt_repairs = self._run_with_prompt_repair(
                        raw_cg_prompt,
                        spec,
                        generate_legacy_text_only,
                        stage=f"cg:{cg_id}:text",
                    )
                    generation_mode = "text_to_image"
                    actual_reference_images = []
                    generation_logic = ["CG 没有可用参考图，使用 CG 提示词直接文生图。"]

            if _usage_tracker:
                _usage_tracker.add_image(1, self.image_generator.model)

            if prompt_repairs:
                logging.info(
                    "[%s] CG 采用最小提示词修复后生成；原始 provider prompt 保留，"
                    "替换记录=%s",
                    cg_id,
                    [
                        replacement_audit(repair)
                        for repair in prompt_repairs
                    ],
                )

            record_visual_generation(
                self.task_id,
                "cg",
                output_path,
                prompt=final_provider_prompt,
                generation_mode=generation_mode,
                reference_images=actual_reference_images,
                logic=generation_logic,
                model=str(self.image_generator.model),
                provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
                extra={
                    "cg_id": cg_id,
                    "used_reference": used_reference,
                    **self._repair_extra(
                        repair_original_prompt,
                        final_provider_prompt,
                        prompt_repairs,
                    ),
                },
            )
            if checkpoint_enabled:
                checkpoint_mark_success(
                    self.task_id,
                    "scene_painter",
                    "generate_cg",
                    cg_id,
                    input_digest=checkpoint_digest,
                    generator_version="cg-image-v2-legacy-resume",
                    dependency_digest=dependency_digest,
                    output_paths=[output_path],
                    validator=validate_image_file,
                    result_mode=(
                        "moderation_repaired"
                        if prompt_repairs
                        else "generated"
                    ),
                )

            return ProcessResult(
                cg_id=cg_id,
                success=True,
                image_path=str(output_path),
                used_reference=used_reference,
                generation_time=time.time() - start,
            )
        except CGReferenceContractBlocked as e:
            logging.error(str(e))
            if checkpoint_enabled and checkpoint_digest:
                try:
                    checkpoint_mark_failed(
                        self.task_id,
                        "scene_painter",
                        "generate_cg",
                        cg_id,
                        input_digest=checkpoint_digest,
                        generator_version="cg-image-v2-legacy-resume",
                        dependency_digest=dependency_digest,
                        error=str(e),
                    )
                except Exception:
                    logging.exception("记录 legacy CG 非重试失败 checkpoint 时发生异常")
            return ProcessResult(
                cg_id=cg_id,
                success=False,
                error_message=str(e),
                generation_time=time.time() - start,
                retryable=False,
            )
        except CGModerationRepairFailed as e:
            logging.error(f"CG {cg_id} 审核修复失败: {e}")
            if checkpoint_enabled and checkpoint_digest:
                try:
                    checkpoint_mark_failed(
                        self.task_id,
                        "scene_painter",
                        "generate_cg",
                        cg_id,
                        input_digest=checkpoint_digest,
                        generator_version="cg-image-v2-legacy-resume",
                        dependency_digest=dependency_digest,
                        error=str(e),
                    )
                except Exception:
                    logging.exception("记录 legacy CG 审核修复失败 checkpoint 时发生异常")
            return ProcessResult(
                cg_id=cg_id,
                success=False,
                error_message=str(e),
                generation_time=time.time() - start,
                retryable=False,
            )
        except Exception as e:
            logging.error(f"处理 CG 失败 {cg_id}: {e}")
            if checkpoint_enabled and checkpoint_digest:
                try:
                    checkpoint_mark_failed(
                        self.task_id,
                        "scene_painter",
                        "generate_cg",
                        cg_id,
                        input_digest=checkpoint_digest,
                        generator_version="cg-image-v2-legacy-resume",
                        dependency_digest=dependency_digest,
                        error=str(e),
                    )
                except Exception:
                    logging.exception("记录 legacy CG 失败 checkpoint 时发生异常")
            return ProcessResult(
                cg_id=cg_id,
                success=False,
                error_message=str(e),
                generation_time=time.time() - start,
            )


# ============================================================================
# 主入口
# ============================================================================

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    parser = argparse.ArgumentParser(description="CG 图片生成器")
    parser.add_argument("--task-id", type=str, default=None, help="指定task_id")
    args = parser.parse_args()

    task_id, cg_data = fetch_cg_from_db(args.task_id)
    if not cg_data:
        logging.warning("cg 表中没有任何数据，跳过 CG 生成（请先跑 info_extractor --stage cg）")
        return 0
    if not task_id:
        task_id = args.task_id

    style_plan = resolve_asset_style_plan(task_id, ASSET_SCENE)
    logging.info(
        "CG 美术风格路由: mode=%s style=%s anchor=%s",
        style_plan.mode,
        style_plan.style,
        style_plan.anchor_path or "(none)",
    )

    # 仅处理已经有 cg_prompt 的条目（info_extractor/cg/cg_prompt.py 已写入）
    runnable = {k: v for k, v in cg_data.items() if isinstance(v, dict) and (v.get("cg_prompt") or "").strip()}
    if not runnable:
        logging.warning("cg 表里所有条目都缺 cg_prompt，先跑 info_extractor --stage cg")
        return 0

    base_url = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com")
    api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
    timeout = int(os.getenv("API_TIMEOUT", "300"))
    # 与 scene_generator 共用同一套 provider/model/quality 配置。
    model = os.getenv("API_MODEL", "gpt-image-2")
    quality = os.getenv("GPT_IMAGE_QUALITY", "low")
    max_retries = int(os.getenv("API_MAX_RETRIES", "3"))

    # 与 scene_generator 一致：Qwen/GPT 走 OpenAI-compatible 中转，
    # Wan 文生图走 DashScope。
    if _is_qwen_model(model):
        base_url = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com")
        api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            logging.error("Qwen 需要 GPT_IMAGE_API_KEY / OPENAI_API_KEY")
            return 1
    elif _is_dashscope_model(model):
        base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1")
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            logging.error("DASHSCOPE_API_KEY 未设置（wan 模型需要）")
            return 1
    else:
        if not api_key:
            logging.error("API Key 未设置，请配置 GPT_IMAGE_API_KEY 或 OPENAI_API_KEY")
            return 1

    # CG 成品与普通场景图分开存放；场景参考图仍从 images/scenes 读取。
    output_dir = Path(OUTPUTS_PATH) / task_id / "images" / "cg"
    output_dir.mkdir(parents=True, exist_ok=True)
    char_dir = Path(OUTPUTS_PATH) / task_id / "images" / "characters"
    scene_dir = Path(OUTPUTS_PATH) / task_id / "images" / "scenes"

    if _usage_tracker and task_id:
        _usage_tracker.set_context(task_id, "scene_painter")

    has_structured_cg = any(
        spec.get("reference_contract_schema") == CG_REFERENCE_CONTRACT_SCHEMA
        for spec in runnable.values()
    )
    def build_image_generator() -> CGImageGenerator:
        return CGImageGenerator(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            model=model,
            size=(
                f"{int(os.getenv('SCENE_WIDTH', str(CG_WIDTH)))}x"
                f"{int(os.getenv('SCENE_HEIGHT', str(CG_HEIGHT)))}"
            ),
            quality=quality,
            max_retries=max_retries,
            enable_qwen_edit_fallback=style_plan.uses_anchor or has_structured_cg,
        )

    image_generator = build_image_generator()

    processor = CGProcessor(
        image_generator,
        output_dir,
        char_dir,
        scene_dir,
        task_id,
        style_plan,
        image_generator_factory=build_image_generator,
    )
    results = processor.process(runnable)

    # 兜底：多轮重试，节奏对齐 scene_generator
    # 与场景图一致，避免 Provider 内层重试 × 外层 25 次形成调用风暴。
    MAX_TOTAL_RETRIES = int(os.getenv("ASSET_MAX_TOTAL_RETRIES", "5"))
    PER_ROUND_RETRIES = int(os.getenv("ASSET_RETRIES_PER_ROUND", "2"))
    retry_counts: Dict[str, int] = {}

    round_num = 0
    while True:
        failed = [r for r in results if not r.success]
        if not failed:
            break
        to_retry = [
            r
            for r in failed
            if r.retryable and retry_counts.get(r.cg_id, 0) < MAX_TOTAL_RETRIES
        ]
        if not to_retry:
            logging.warning(
                "所有失败 CG 均不可重试或已达到 %s 次重试上限，停止重试",
                MAX_TOTAL_RETRIES,
            )
            break

        round_num += 1
        logging.info(f"===== CG 兜底重试第 {round_num} 轮：{len(to_retry)} 个，每张最多 {PER_ROUND_RETRIES} 次 =====")

        def retry_cg(r):
            spec = runnable.get(r.cg_id)
            if not spec:
                return r.cg_id, r
            best: ProcessResult = r
            for _ in range(PER_ROUND_RETRIES):
                retry_counts[r.cg_id] = retry_counts.get(r.cg_id, 0) + 1
                res = processor._process_single(r.cg_id, spec)
                if res.success:
                    best = res
                    break
                best = res
                if retry_counts[r.cg_id] >= MAX_TOTAL_RETRIES:
                    logging.warning(f"  {r.cg_id} 已达累计上限 {MAX_TOTAL_RETRIES} 次，停止")
                    break
            return r.cg_id, best

        retry_pairs = run_dependency_layer(
            to_retry,
            retry_cg,
            concurrency=resolve_image_concurrency("CG_RETRY_IMAGE_CONCURRENCY"),
            stage=f"cg_retry_{round_num}",
            task_id=task_id,
            label=lambda result: str(result.cg_id),
            logger=logging.getLogger(__name__),
        )
        retry_map = dict(retry_pairs)

        results = [retry_map.get(r.cg_id, r) for r in results]

    total = len(results)
    success = sum(1 for r in results if r.success)
    print(f"CG 处理完成：总共 {total} 张，成功 {success} 张，失败 {total - success} 张")

    if _usage_tracker:
        _usage_tracker.flush()

    # A few CGs may be unavailable while ordinary scenes remain usable.  Let
    # scene_painter publish a partial report instead of failing the whole DAG.
    return 2 if success != total else 0


if __name__ == "__main__":
    sys.exit(main())
