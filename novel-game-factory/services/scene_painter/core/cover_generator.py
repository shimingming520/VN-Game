#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
封面图片生成器 - 从数据库读取提示词，调用 wan2.5 文生图

负责：
- 从 backgrounds 表读取由 cover_extractor.py 写入的提示词
    data['generated_prompts']    : 各章节封面提示词
    data['novel_cover_metadata'] : 游戏总封面提示词
- 调用 DashScope wan2.5 API 生成图片
- 保存图片到本地
"""

import os
import json
import requests
import time
import sys
import psycopg2
from dataclasses import dataclass
from dotenv import load_dotenv
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, unquote

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import DATABASE_URL, OUTPUTS_PATH, PLATFORM_DATABASE_URL
from common.image_concurrency import (
    ThreadLocalResource,
    resolve_image_concurrency,
    run_dependency_layer,
)
from common.visual_checkpoint import (
    assert_retry_budget,
    canonical_digest,
    mark_failed as checkpoint_mark_failed,
    mark_running as checkpoint_mark_running,
    mark_success as checkpoint_mark_success,
    reusable_or_adopt_existing,
)
from common.visual_style import (
    ASSET_SCENE,
    AssetStylePlan,
    apply_asset_style,
    compose_anchor_guided_prompt,
    resolve_asset_style_plan,
)
try:
    from services.scene_painter.core.artifact_contract import (
        atomic_image_output,
        validate_image_file,
    )
except ImportError:
    from artifact_contract import atomic_image_output, validate_image_file  # type: ignore

# 复用 scene_generator 的 ImageGenerator + WanImageTextProvider，按 API_MODEL 自动路由 gpt / wan
try:
    from services.scene_painter.core.scene_generator import (
        ImageGenerator as _SceneImageGenerator,
        SCENE_OUTER_REQUEST_ATTEMPTS,
        _is_qwen_model,
        _is_wan_model,
    )
except ImportError:
    from scene_generator import (  # type: ignore
        ImageGenerator as _SceneImageGenerator,
        SCENE_OUTER_REQUEST_ATTEMPTS,
        _is_qwen_model,
        _is_wan_model,
    )

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

# 解析平台库 URL 为 psycopg2 配置(跨模块共用表已迁 agent_platform)
parsed = urlparse(PLATFORM_DATABASE_URL)
DATABASE_CONFIG = {
    'host': parsed.hostname,
    'port': parsed.port,
    'user': parsed.username,
    'password': unquote(parsed.password),
    'dbname': parsed.path[1:]
}

CONFIG = {
    "IMAGE_SIZE": (1792, 1008),
    # 默认 gpt-image-2 私有部署；当 API_MODEL 设为 wan*/qwen-image* 时自动切 DashScope
    "MODEL_NAME": os.getenv("API_MODEL", "gpt-image-2"),
    "GPT_BASE_URL": os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"),
    "WAN_BASE_URL": os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"),
    "QUALITY": os.getenv("GPT_IMAGE_QUALITY", "high"),
    "API_TIMEOUT": int(os.getenv("API_TIMEOUT", "300")),
    "API_MAX_RETRIES": int(os.getenv("API_MAX_RETRIES", "3")),
    # === 旧版：DashScope wan2.5（保留备查） ===
    # "MODEL_NAME": "wan2.5-t2i-preview",
    # "API_ENDPOINT": "https://dashscope.aliyuncs.com/api/v1",
    # "API_TIMEOUT": 120,
}


# 16:9 比例后缀：与 scene_generator 一致。后端网关忽略 API 的 size 参数，
# 仅根据 prompt 里的比例措辞决定输出宽高比，不加容易生成竖版封面。
COVER_ASPECT_RATIO_SUFFIX = ", 16:9 aspect ratio, cinematic widescreen composition, horizontal landscape orientation"


def _ensure_aspect_ratio(prompt: str) -> str:
    """给封面 prompt 追加 16:9 比例措辞（已含则不重复追加）。"""
    if not prompt or "16:9 aspect ratio" in prompt:
        return prompt
    return prompt.rstrip().rstrip(",") + COVER_ASPECT_RATIO_SUFFIX


def build_cover_checkpoint_digest(
    asset_id: str,
    prompt: str,
    negative_prompt: str,
    style_plan: AssetStylePlan,
    *,
    model: str,
    image_size,
    quality: str,
) -> str:
    return canonical_digest(
        {
            "node_id": asset_id,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "model": model,
            "image_size": image_size,
            "quality": quality,
            "style": {
                "mode": style_plan.mode,
                "style": style_plan.style,
                "anchor_sha256": style_plan.anchor_sha256,
            },
        }
    )


def get_output_paths(task_id=None):
    """返回基于task_id的输出路径"""
    if not task_id:
        raise ValueError("task_id is required for output paths")
    output_dir = os.path.join(OUTPUTS_PATH, task_id, "images", "backgrounds")
    temp_dir = os.path.join(OUTPUTS_PATH, task_id, "images", "backgrounds", "temp")
    positions_file = os.path.join(OUTPUTS_PATH, task_id, "images", "backgrounds", "node_positions.json")
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)
    return output_dir, temp_dir, positions_file


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class ImageData:
    """图片数据"""
    data: bytes
    format: str = 'png'
    width: int = 1024
    height: int = 1024

    def save_to_file(self, file_path: str) -> None:
        with atomic_image_output(file_path) as temporary_path:
            temporary_path.write_bytes(self.data)


# ============================================================================
# 图片生成器：直接复用 scene_generator.ImageGenerator（按 model 名路由 gpt / wan）
# 旧本地实现仅支持 gpt-image-2，已删除；如需查阅请翻 git 历史
# ============================================================================

ImageGenerator = _SceneImageGenerator


def _wrap_generate_image(generator: ImageGenerator, prompt: str, negative_prompt: str,
                         width: int, height: int) -> "ImageData":
    """把 scene_generator 的 ImageData 转成 cover 这边的 ImageData（字段相同，类型不同）"""
    img = generator.retry_on_failure(
        prompt=prompt,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        max_retries=SCENE_OUTER_REQUEST_ATTEMPTS,
    )
    return ImageData(data=img.data, format=img.format, width=img.width, height=img.height)


def _generate_style_image(
    generator: ImageGenerator,
    style_plan: AssetStylePlan,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
) -> "ImageData":
    """Generate a cover through text-to-image or the confirmed scene Anchor."""
    if style_plan.uses_anchor:
        img = generator.edit_image_with_retry(
            base_image_path=style_plan.anchor_path,
            prompt=compose_anchor_guided_prompt(style_plan, prompt),
            width=width,
            height=height,
            max_retries=SCENE_OUTER_REQUEST_ATTEMPTS,
        )
    else:
        img = generator.retry_on_failure(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            max_retries=SCENE_OUTER_REQUEST_ATTEMPTS,
        )
    return ImageData(data=img.data, format=img.format, width=img.width, height=img.height)


# === 旧版 wan2.5 ImageGenerator（DashScope 异步任务式 API）保留备查 ===
# class ImageGenerator:
#     """图片生成器 - 调用 DashScope wan2.5 API"""
#
#     def __init__(self, api_endpoint: str, api_key: str, timeout: int = 60, model: str = "wan2.5-t2i-preview"):
#         self.api_endpoint = api_endpoint
#         self.api_key = api_key
#         self.timeout = timeout
#         self.model = model
#
#     def generate_image(self, prompt: str, negative_prompt: str = "", width: int = 1690, height: int = 960) -> ImageData:
#         """提交异步任务并轮询结果"""
#         payload = {
#             "model": self.model,
#             "input": {"prompt": prompt},
#             "parameters": {"size": f"{width}*{height}", "n": 1}
#         }
#         if negative_prompt:
#             payload["input"]["negative_prompt"] = negative_prompt
#
#         headers = {
#             "Authorization": f"Bearer {self.api_key}",
#             "Content-Type": "application/json",
#             "X-DashScope-Async": "enable"
#         }
#
#         print(f"调用API生成图片: {prompt[:50]}...")
#
#         response = requests.post(
#             f"{self.api_endpoint}/services/aigc/text2image/image-synthesis",
#             json=payload,
#             headers=headers,
#             timeout=self.timeout
#         )
#
#         if response.status_code == 403:
#             raise Exception("API密钥无权限访问wan2.5-t2i-preview模型，请确认账户权限")
#         elif response.status_code == 401:
#             raise Exception("API密钥无效，请检查DASHSCOPE_API_KEY设置")
#
#         response.raise_for_status()
#         result = response.json()
#
#         if 'output' not in result or 'task_id' not in result['output']:
#             raise Exception(f"API返回格式错误: {result}")
#
#         task_id = result['output']['task_id']
#         print(f"任务已提交，task_id: {task_id}")
#
#         # 轮询任务状态
#         for i in range(30):
#             time.sleep(10)
#             task_response = requests.get(
#                 f"{self.api_endpoint}/tasks/{task_id}",
#                 headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
#                 timeout=self.timeout
#             )
#             task_response.raise_for_status()
#             task_result = task_response.json()
#             task_status = task_result['output'].get('task_status', 'UNKNOWN')
#
#             if task_status == 'SUCCEEDED':
#                 image_url = task_result['output']['results'][0]['url']
#                 print(f"下载图片: {image_url}")
#                 img_response = requests.get(image_url, timeout=self.timeout)
#                 img_response.raise_for_status()
#                 return ImageData(data=img_response.content, format='png', width=width, height=height)
#             elif task_status == 'FAILED':
#                 raise Exception(f"任务失败: {task_result['output'].get('message', '未知错误')}")
#             elif task_status in ['PENDING', 'RUNNING']:
#                 print(f"任务处理中... ({i+1}/30)")
#                 continue
#             else:
#                 raise Exception(f"未知任务状态: {task_status}")
#
#         raise Exception("任务超时: 轮询30次后仍未完成")
#
#     def retry_on_failure(self, prompt: str, negative_prompt: str = "", width: int = 1690, height: int = 960, max_retries: int = 3) -> ImageData:
#         """带重试机制的图片生成"""
#         last_exception = None
#         for attempt in range(max_retries):
#             try:
#                 return self.generate_image(prompt=prompt, negative_prompt=negative_prompt, width=width, height=height)
#             except requests.RequestException as e:
#                 last_exception = e
#                 wait_time = 2 ** attempt
#                 print(f"第 {attempt + 1} 次尝试失败，{wait_time}秒后重试...")
#                 time.sleep(wait_time)
#         raise last_exception


# ============================================================================
# 封面图片生成器
# ============================================================================

class CoverGeneratorFromDB:
    """
    从 backgrounds 表读取已保存的提示词，生成封面图片。
    依赖 cover_extractor.py 先完成视觉元素提取和提示词构建。
    """

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or CONFIG
        load_dotenv()
        # 按 model 名自动选 api_key / base_url，全部来自 .env
        model = self.config["MODEL_NAME"]
        if _is_qwen_model(model):
            self.api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
            base_url = self.config.get("GPT_BASE_URL") or os.getenv(
                "GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"
            )
            if not self.api_key:
                raise ValueError("Qwen 需要 GPT_IMAGE_API_KEY / OPENAI_API_KEY")
        elif _is_wan_model(model):
            self.api_key = os.getenv("DASHSCOPE_API_KEY")
            base_url = self.config.get("WAN_BASE_URL") or os.getenv(
                "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"
            )
            if not self.api_key:
                raise ValueError("DASHSCOPE_API_KEY 未设置（wan 模型需要）")
        else:
            self.api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
            base_url = self.config.get("GPT_BASE_URL") or os.getenv(
                "GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"
            )
            if not self.api_key:
                raise ValueError("API Key 未设置，请配置 GPT_IMAGE_API_KEY 或 OPENAI_API_KEY")

        self.base_url = base_url
        self.model = model
        initial_generator = self._build_image_generator()
        self._image_generators = ThreadLocalResource(
            self._build_image_generator,
            initial=initial_generator,
        )
        self.output_dir = None
        self.temp_dir = None

    def _build_image_generator(self) -> ImageGenerator:
        size_str = f"{self.config['IMAGE_SIZE'][0]}x{self.config['IMAGE_SIZE'][1]}"
        return ImageGenerator(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.config["API_TIMEOUT"],
            model=self.model,
            size=size_str,
            quality=self.config.get("QUALITY", "high"),
            max_retries=self.config["API_MAX_RETRIES"],
        )

    @property
    def image_generator(self) -> ImageGenerator:
        """Return one mutable Provider wrapper per concurrent worker."""
        return self._image_generators.get()

    def generate_images_from_db_prompts(self, taskid=None):
        """
        从 backgrounds 表读取已保存的提示词，生成所有封面图片。
        返回 (success_count, total_count)
        """
        try:
            conn = psycopg2.connect(**DATABASE_CONFIG)
            cursor = conn.cursor()

            if taskid:
                selected_taskid = taskid
            else:
                cursor.execute("SELECT task_id FROM backgrounds ORDER BY create_time DESC LIMIT 1")
                result = cursor.fetchone()
                if not result:
                    print("错误: backgrounds 表中没有数据，请先运行 cover_extractor.py")
                    cursor.close()
                    conn.close()
                    return 0, 0
                selected_taskid = result[0]

            print(f"使用task_id: {selected_taskid}")
            cursor.execute("SELECT data FROM backgrounds WHERE task_id = %s LIMIT 1", (selected_taskid,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()

            if not row:
                print(f"错误: 未找到 task_id={selected_taskid} 的 backgrounds 数据")
                return 0, 0

            bg_data = row[0]
            chapter_prompts = bg_data.get('generated_prompts', {})
            novel_cover_metadata = bg_data.get('novel_cover_metadata', {})

            if not chapter_prompts:
                print("警告: 数据库中没有章节封面提示词，请先运行 build_all_prompts_to_db()")
                return 0, 0

        except Exception as e:
            print(f"读取提示词失败: {e}")
            import traceback
            traceback.print_exc()
            return 0, 0

        # 生成章节封面图片
        style_plan = resolve_asset_style_plan(selected_taskid, ASSET_SCENE)
        print(
            f"场景美术风格路由: mode={style_plan.mode} "
            f"style={style_plan.style} anchor={style_plan.anchor_path or '(none)'}"
        )
        success_count = 0
        chapter_total_count = len(chapter_prompts)
        novel_prompt_raw = str(novel_cover_metadata.get('prompt', '') or '').strip()
        total_count = chapter_total_count + (1 if novel_prompt_raw else 0)
        print(f"开始从数据库提示词生成 {chapter_total_count} 个章节封面...")

        def generate_chapter(item):
            node_id, prompt_data = item
            prompt = _ensure_aspect_ratio(apply_asset_style(
                style_plan,
                prompt_data.get('prompt', ''),
            ))
            negative_prompt = prompt_data.get('negative_prompt', '')
            final_path = os.path.join(self.output_dir, f"{node_id}.png")
            input_digest = build_cover_checkpoint_digest(
                str(node_id),
                prompt,
                negative_prompt,
                style_plan,
                model=self.config["MODEL_NAME"],
                image_size=self.config["IMAGE_SIZE"],
                quality=self.config.get("QUALITY", "high"),
            )
            try:
                decision = reusable_or_adopt_existing(
                    selected_taskid,
                    "scene_painter",
                    "generate_cover",
                    str(node_id),
                    input_digest=input_digest,
                    generator_version="cover-image-v2-resume",
                    output_paths=[final_path],
                    validator=validate_image_file,
                )
                if decision.reusable:
                    print(f"  ↷ 复用有效封面: {final_path} ({decision.reason})")
                    return node_id, True
                assert_retry_budget(
                    selected_taskid,
                    "scene_painter",
                    "generate_cover",
                    str(node_id),
                    input_digest=input_digest,
                    generator_version="cover-image-v2-resume",
                )
                checkpoint_mark_running(
                    selected_taskid,
                    "scene_painter",
                    "generate_cover",
                    str(node_id),
                    input_digest=input_digest,
                    generator_version="cover-image-v2-resume",
                )
                image_data = _generate_style_image(
                    self.image_generator,
                    style_plan,
                    prompt,
                    negative_prompt,
                    self.config["IMAGE_SIZE"][0],
                    self.config["IMAGE_SIZE"][1],
                )
                if _usage_tracker:
                    _usage_tracker.add_image(
                        1,
                        self.config["MODEL_NAME"],
                        provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
                    )
                with atomic_image_output(final_path) as temporary_path:
                    temporary_path.write_bytes(image_data.data)
                checkpoint_mark_success(
                    selected_taskid,
                    "scene_painter",
                    "generate_cover",
                    str(node_id),
                    input_digest=input_digest,
                    generator_version="cover-image-v2-resume",
                    output_paths=[final_path],
                    validator=validate_image_file,
                )
                print(f"  ✓ 成功生成封面: {final_path}")
                return node_id, True
            except Exception as e:
                try:
                    checkpoint_mark_failed(
                        selected_taskid,
                        "scene_painter",
                        "generate_cover",
                        str(node_id),
                        input_digest=input_digest,
                        generator_version="cover-image-v2-resume",
                        error=str(e),
                    )
                except Exception:
                    print(f"  ✗ 节点 {node_id} checkpoint 写入失败")
                print(f"  ✗ 节点 {node_id} 封面生成失败: {e}")
                return node_id, False

        chapter_items = list(chapter_prompts.items())
        chapter_results = run_dependency_layer(
            chapter_items,
            generate_chapter,
            concurrency=resolve_image_concurrency("COVER_IMAGE_CONCURRENCY"),
            stage="cover_chapter",
            task_id=selected_taskid,
            label=lambda item: str(item[0]),
        )
        success_count = sum(1 for _, succeeded in chapter_results if succeeded)
        failed_chapters: Dict[str, Any] = {
            node_id: chapter_prompts[node_id]
            for node_id, succeeded in chapter_results
            if not succeeded
        }

        print(f"\n章节封面生成完成! 成功: {success_count}/{chapter_total_count}")

        # 兜底：对失败的章节封面重新提交一次
        if failed_chapters:
            print(f"\n===== 兜底重试: {len(failed_chapters)} 个失败章节封面 =====")
            retry_results = run_dependency_layer(
                list(failed_chapters.items()),
                generate_chapter,
                concurrency=resolve_image_concurrency("COVER_IMAGE_CONCURRENCY"),
                stage="cover_chapter_retry",
                task_id=selected_taskid,
                label=lambda item: str(item[0]),
            )
            success_count += sum(1 for _, succeeded in retry_results if succeeded)
            print(f"兜底重试完成，章节封面最终成功: {success_count}/{chapter_total_count}")

        # 生成游戏总封面
        novel_cover_failed = False
        if novel_prompt_raw:
            novel_prompt = _ensure_aspect_ratio(apply_asset_style(
                style_plan,
                novel_prompt_raw,
            ))
            novel_negative = novel_cover_metadata.get('negative_prompt', '')
            if novel_prompt:
                print("开始生成游戏总封面...")
                novel_cover_path = os.path.join(self.output_dir, "novel_cover.png")
                novel_digest = build_cover_checkpoint_digest(
                    "novel_cover",
                    novel_prompt,
                    novel_negative,
                    style_plan,
                    model=self.config["MODEL_NAME"],
                    image_size=self.config["IMAGE_SIZE"],
                    quality=self.config.get("QUALITY", "high"),
                )
                try:
                    decision = reusable_or_adopt_existing(
                        selected_taskid,
                        "scene_painter",
                        "generate_cover",
                        "novel_cover",
                        input_digest=novel_digest,
                        generator_version="cover-image-v2-resume",
                        output_paths=[novel_cover_path],
                        validator=validate_image_file,
                    )
                    if decision.reusable:
                        success_count += 1
                        print(
                            f"↷ 复用有效游戏总封面: {novel_cover_path} "
                            f"({decision.reason})"
                        )
                        novel_cover_failed = False
                        raise StopIteration
                    assert_retry_budget(
                        selected_taskid,
                        "scene_painter",
                        "generate_cover",
                        "novel_cover",
                        input_digest=novel_digest,
                        generator_version="cover-image-v2-resume",
                    )
                    checkpoint_mark_running(
                        selected_taskid,
                        "scene_painter",
                        "generate_cover",
                        "novel_cover",
                        input_digest=novel_digest,
                        generator_version="cover-image-v2-resume",
                    )
                    image_data = _generate_style_image(
                        self.image_generator,
                        style_plan,
                        novel_prompt,
                        novel_negative,
                        self.config["IMAGE_SIZE"][0],
                        self.config["IMAGE_SIZE"][1],
                    )
                    if _usage_tracker:
                        _usage_tracker.add_image(
                            1,
                            self.config["MODEL_NAME"],
                            provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
                        )
                    with atomic_image_output(novel_cover_path) as temporary_path:
                        temporary_path.write_bytes(image_data.data)
                    checkpoint_mark_success(
                        selected_taskid,
                        "scene_painter",
                        "generate_cover",
                        "novel_cover",
                        input_digest=novel_digest,
                        generator_version="cover-image-v2-resume",
                        output_paths=[novel_cover_path],
                        validator=validate_image_file,
                    )
                    success_count += 1
                    print(f"✓ 成功生成游戏总封面: {novel_cover_path}")
                except StopIteration:
                    pass
                except Exception as e:
                    try:
                        checkpoint_mark_failed(
                            selected_taskid,
                            "scene_painter",
                            "generate_cover",
                            "novel_cover",
                            input_digest=novel_digest,
                            generator_version="cover-image-v2-resume",
                            error=str(e),
                        )
                    except Exception:
                        print("⚠ 游戏总封面 checkpoint 写入失败")
                    print(f"⚠ 游戏总封面生成失败: {e}")
                    novel_cover_failed = True

                # 兜底：游戏总封面重试
                if novel_cover_failed:
                    print("===== 兜底重试: 游戏总封面 =====")
                    try:
                        image_data = _generate_style_image(
                            self.image_generator,
                            style_plan,
                            novel_prompt,
                            novel_negative,
                            self.config["IMAGE_SIZE"][0],
                            self.config["IMAGE_SIZE"][1],
                        )
                        if _usage_tracker:
                            _usage_tracker.add_image(
                                1,
                                self.config["MODEL_NAME"],
                                provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
                            )
                        novel_cover_path = os.path.join(self.output_dir, "novel_cover.png")
                        with atomic_image_output(novel_cover_path) as temporary_path:
                            temporary_path.write_bytes(image_data.data)
                        checkpoint_mark_success(
                            selected_taskid,
                            "scene_painter",
                            "generate_cover",
                            "novel_cover",
                            input_digest=novel_digest,
                            generator_version="cover-image-v2-resume",
                            output_paths=[novel_cover_path],
                            validator=validate_image_file,
                        )
                        success_count += 1
                        print(f"✓ 兜底成功生成游戏总封面: {novel_cover_path}")
                    except Exception as e:
                        try:
                            checkpoint_mark_failed(
                                selected_taskid,
                                "scene_painter",
                                "generate_cover",
                                "novel_cover",
                                input_digest=novel_digest,
                                generator_version="cover-image-v2-resume",
                                error=str(e),
                            )
                        except Exception:
                            print("⚠ 游戏总封面兜底 checkpoint 写入失败")
                        print(f"⚠ 游戏总封面兜底仍失败: {e}")

        return success_count, total_count

    def run(self, taskid=None):
        """从数据库读取提示词，生成所有封面图片"""
        output_dir, temp_dir, _ = get_output_paths(taskid)
        self.output_dir = output_dir
        self.temp_dir = temp_dir

        success_count, total_count = self.generate_images_from_db_prompts(taskid)

        try:
            import shutil
            if self.temp_dir and os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
        except Exception as e:
            print(f"清理临时目录时出错: {e}")

        return success_count, total_count


def main():
    import argparse
    parser = argparse.ArgumentParser(description="封面图片生成器")
    parser.add_argument("--task-id", type=str, default=None, help="指定task_id")
    args = parser.parse_args()
    taskid = args.task_id

    try:
        generator = CoverGeneratorFromDB(CONFIG)
        if _usage_tracker and taskid:
            _usage_tracker.set_context(
                taskid, "scene_painter", force_new_run=True
            )

        success_count, total_count = generator.run(taskid)

        if _usage_tracker:
            _usage_tracker.flush()

        if total_count <= 0 or success_count != total_count:
            print(f"封面产物不完整: 成功 {success_count}/{total_count}")
            return 2
        return 0

    except ValueError as e:
        print(f"错误: {e}")
        return 1
    except Exception as e:
        print(f"发生错误: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
