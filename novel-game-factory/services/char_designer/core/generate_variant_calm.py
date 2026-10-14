#!/usr/bin/env python3
"""
角色变体立绘生成器。

对有多个激活组合的角色，以数字最小的组合图片为基础，
通过图生图生成其余变体的 calm 状态立绘。

文件名格式：人物名_p-b-c_calm.png
提示词前缀：保证脸部和人物形象基本不变，<原始提示词>

用法：
  python char_designer/core/generate_variant_calm.py --task-id <task_id> --test
  python char_designer/core/generate_variant_calm.py --task-id <task_id> --image-provider qwen
  python char_designer/core/generate_variant_calm.py --task-id <task_id> --image-provider gpt
  python char_designer/core/generate_variant_calm.py --task-id <task_id> --image-provider gpt --overwrite
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

current_dir = Path(__file__).resolve().parent
char_designer_dir = current_dir.parent
workspace_root = char_designer_dir.parent
project_root = workspace_root.parent
for path in (str(workspace_root), str(char_designer_dir), str(current_dir), str(project_root)):
    if path not in sys.path:
        sys.path.insert(0, path)


def add_common_config_search_paths() -> None:
    # 本仓库自带 common/config.py 时，直接用它并返回，绝不去扫描其它仓库副本。
    # 否则 glob 命中的外部仓库 config.py 会被 insert(0) 抢占 sys.path，
    # 而部分旧副本的 DATABASE_URL 未对密码里的 '@' 做 %40 编码，导致连库失败。
    if (project_root / "common" / "config.py").exists():
        candidate_str = str(project_root)
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
    load_dotenv(dotenv_path=Path(project_root) / "novel-game-factory" / ".env")
    load_dotenv(dotenv_path=workspace_root / ".env")
except ImportError:
    pass

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
    ensure_asset_style,
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
from services.char_designer.utils.character_identity import (
    combination_filename,
    resolve_combination_speaker_id,
)
from services.char_designer.core.generate_calm import (
    _init_conversion_cache,
    chinese_to_camel_case,
    resolve_authoritative_speaker_id,
)
from services.char_designer.utils.native_transparency import (
    inspect_native_transparency,
    resolve_character_output_policy,
    validator_for_policy,
)

try:
    from sqlalchemy import text
    HAS_SQLALCHEMY = True
except ImportError:
    HAS_SQLALCHEMY = False

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

try:
    from char_designer.image_providers.gpt_image import GPTImageEditProvider
    from char_designer.image_providers.qwen_image import QwenImageEditProvider
    from char_designer.utils.prompt_repair import (
        PromptPersistenceContext,
        execute_with_prompt_repair,
    )
except ImportError:
    from image_providers.gpt_image import GPTImageEditProvider  # type: ignore
    from image_providers.qwen_image import QwenImageEditProvider  # type: ignore
    from utils.prompt_repair import PromptPersistenceContext, execute_with_prompt_repair  # type: ignore

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

IDENTITY_CONSISTENCY_PREFIX = (
    "原图与目标是同一角色在不同时期或状态下的形象。"
    "保持画风一致和角色的整体识别度，"
    "以下方提示词为准，与原图不一致时以提示词为准。"
    "全身构图，从头到脚完整可见，包括鞋子，不要裁切腿部或脚部，不要拉近镜头。"
    "Do not add outlines around the character. "
)
VARIANT_CALM_GENERATOR_VERSION = "character-variant-calm-v3-native-rgba"


# ==================== 数据加载 ====================

def _parse_combination_str(combination: str) -> Tuple[str, int, int, int]:
    parts = combination.rsplit("-", 3)
    if len(parts) != 4:
        raise ValueError(f"无效的 combination 格式: {combination}")
    name, p, b, c = parts
    return name, int(p), int(b), int(c)


DEFAULT_SHOT = "LS"


def _combination_to_filename(
    combination: str,
    emotion: str = "calm",
    shot: str = DEFAULT_SHOT,
    *,
    prompt_record: Optional[Dict] = None,
    output_dir: Optional[Path] = None,
    prefer_existing: bool = False,
) -> str:
    """'萧无尘-1-1-1' -> 'XiaoWuChen_1-1-1_calm_LS.png'."""
    return combination_filename(
        combination,
        emotion,
        shot,
        prompt_record=prompt_record,
        output_dir=output_dir,
        fallback_converter=chinese_to_camel_case,
        prefer_existing=prefer_existing,
    )


def load_variant_tasks(task_id: str) -> List[Dict]:
    """
    返回需要图生图的任务列表，每项包含：
      - base_combination: 基础组合（数字最小）
      - target_combination: 目标组合
      - prompt: 目标组合的提示词（已加前缀）
      - base_filename: 基础图片文件名
      - target_filename: 目标图片文件名
    """
    with platform_engine.connect() as conn:
        char_row = conn.execute(
            text("SELECT data FROM characters WHERE task_id = :tid"), {"tid": task_id}
        ).fetchone()
        prompt_row = conn.execute(
            text("SELECT data FROM character_prompts WHERE task_id = :tid"), {"tid": task_id}
        ).fetchone()

    if not char_row or not prompt_row:
        raise RuntimeError(f"未找到 task_id={task_id} 的数据")

    char_data = char_row[0]
    prompts_list = prompt_row[0]
    prompt_map = {}
    for raw_item in prompts_list:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        combination = str(item.get("combination") or "").strip()
        raw_name = combination.rsplit("-", 3)[0] if combination else item.get("name")
        resolved_id = resolve_authoritative_speaker_id(
            raw_name,
            item.get("speaker_id") or "",
        )
        if resolved_id:
            item["speaker_id"] = resolved_id
        prompt_map[combination or item.get("speaker_id", "")] = item
    output_dir = Path(OUTPUTS_PATH) / task_id / "images" / "characters"

    tasks = []
    for char_key, char_entry in char_data.items():
        char = char_entry[0] if isinstance(char_entry, list) else char_entry
        if not isinstance(char, dict):
            continue

        combinations = char.get("valid_status_combinations", [])
        active = [c for c in combinations if isinstance(c, dict) and c.get("is_active") and c.get("combination")]
        if len(active) <= 1:
            continue

        def _sort_key(combo):
            try:
                _, p, b, c = _parse_combination_str(combo["combination"])
                return (p, b, c)
            except Exception:
                return (999, 999, 999)

        active_sorted = sorted(active, key=_sort_key)
        base_combo = active_sorted[0]["combination"]
        base_prompt_item = prompt_map.get(base_combo) or {}

        for variant in active_sorted[1:]:
            target_combo = variant["combination"]
            prompt_item = prompt_map.get(target_combo) or {}
            raw_prompt = prompt_item.get("positive_prompt", "")
            if not raw_prompt:
                logger.warning(f"未找到 {target_combo} 的提示词，跳过")
                continue
            raw_prompt = ensure_asset_style(task_id, ASSET_CHARACTER, raw_prompt)
            base_speaker_id = resolve_combination_speaker_id(
                base_combo,
                prompt_record=base_prompt_item or prompt_item,
                output_dir=output_dir,
                fallback_converter=chinese_to_camel_case,
                prefer_existing=True,
            )
            target_prompt_item = dict(prompt_item)
            prompt_speaker_id = str(target_prompt_item.get("speaker_id") or "").strip()
            if prompt_speaker_id and base_speaker_id and prompt_speaker_id != base_speaker_id:
                logger.warning(
                    f"{target_combo} 的 speaker_id={prompt_speaker_id} 与基础图角色 ID="
                    f"{base_speaker_id} 不一致，沿用基础图角色 ID"
                )
            target_prompt_item["speaker_id"] = base_speaker_id or prompt_speaker_id
            tasks.append({
                "base_combination": base_combo,
                "target_combination": target_combo,
                "speaker_id": target_prompt_item.get("speaker_id", ""),
                "raw_prompt": raw_prompt,
                "prompt": IDENTITY_CONSISTENCY_PREFIX + raw_prompt,
                "base_filename": _combination_to_filename(
                    base_combo,
                    prompt_record={"speaker_id": base_speaker_id},
                ),
                "target_filename": _combination_to_filename(
                    target_combo,
                    prompt_record=target_prompt_item,
                ),
            })

    return tasks


# ==================== 图片处理 ====================

def add_image_suffix(filename: str, index: int) -> str:
    path = Path(filename)
    return f"{path.stem}_{index + 1}{path.suffix}"


def build_provider(args, output_policy):
    if args.image_provider == "qwen":
        return QwenImageEditProvider(
            api_key=args.api_key,
            model=args.model or os.environ.get("QWEN_VARIANT_MODEL", "qwen-image-edit-plus"),
            size=args.size or "1140*1472",
            max_retries=args.max_retries,
            retry_delay=args.retry_delay,
        )

    api_key = args.api_key or os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
    return GPTImageEditProvider(
        api_key=api_key,
        base_url=args.base_url,
        model=args.model or "gpt-image-2",
        size=args.size or "1024x1536",
        quality=args.quality,
        n=args.n,
        response_format=args.response_format,
        output_format=output_policy.output_format,
        background=output_policy.background,
        moderation=args.moderation,
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
        retry_delay=args.retry_delay,
        transparent_character_output=output_policy.native_transparency,
    )


def generate_with_qwen(
    provider: QwenImageEditProvider,
    base_path: Path,
    target_path: Path,
    prompt: str,
    *,
    task_id: str,
) -> int:
    url = provider.generate(str(base_path), prompt)
    if not url:
        return 0
    if _usage_tracker:
        _usage_tracker.add_image(1, provider.model, provider="qwen")
    with atomic_image_output(target_path) as temporary_path:
        if not provider.download(url, str(temporary_path)):
            raise CharacterImageArtifactError(
                f"Qwen 未生成有效变体图片: {target_path}"
            )
    record_visual_generation(
        task_id,
        "characters",
        target_path,
        prompt=prompt,
        generation_mode="image_to_image",
        reference_images=[{"path": base_path, "role": "人物基础组合立绘"}],
        logic=[
            "人物服饰/年龄/状态变体以基础组合 calm 立绘做图生图。",
            "提示词中的目标组合内容优先，参考图用于保持人物身份与画风连续性。",
        ],
        model=str(provider.model),
        provider="qwen",
        extra={
            "character_output_mode": "opaque_debug",
        },
    )
    return 1


def generate_with_gpt(
    provider: GPTImageEditProvider,
    base_path: Path,
    target_path: Path,
    prompt: str,
    *,
    task_id: str,
    stage: str,
    output_policy,
    asset_validator,
    persistence: Optional[PromptPersistenceContext] = None,
) -> int:
    data_items, final_prompt = execute_with_prompt_repair(
        lambda repaired_prompt: provider.edit(base_path, repaired_prompt),
        prompt,
        generation_mode="image_to_image",
        stage=stage,
        persistence=persistence,
        immutable_prefix=IDENTITY_CONSISTENCY_PREFIX,
    )
    if data_items and _usage_tracker:
        _usage_tracker.add_image(len(data_items), provider.model, provider="gpt")
    success_count = 0
    for image_index, item in enumerate(data_items):
        output_path = target_path if len(data_items) == 1 else target_path.with_name(add_image_suffix(target_path.name, image_index))
        with atomic_image_output(
            output_path,
            artifact_validator=asset_validator,
        ) as temporary_path:
            if not provider.save_image_item(item, temporary_path):
                raise CharacterImageArtifactError(
                    f"GPT 未生成有效变体图片: {output_path}"
                )
        final_provider_prompt = str(
            getattr(provider, "last_prompt", final_prompt)
        )
        transparency_metrics = (
            inspect_native_transparency(output_path)
            if output_policy.require_transparency
            else {}
        )
        record_visual_generation(
            task_id,
            "characters",
            output_path,
            prompt=final_provider_prompt,
            generation_mode="image_to_image",
            reference_images=[{"path": base_path, "role": "人物基础组合立绘"}],
            logic=[
                "人物服饰/年龄/状态变体以基础组合 calm 立绘做图生图。",
                "提示词中的目标组合内容优先，参考图用于保持人物身份与画风连续性。",
            ],
            model=str(provider.model),
            provider="gpt",
            extra={
                "character_output_policy": output_policy.digest_payload(),
                "transparency_metrics": transparency_metrics,
            },
        )
        success_count += 1
    return success_count


# ==================== 主函数 ====================

def main():
    parser = argparse.ArgumentParser(description="角色变体立绘生成器（图生图）")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--image-provider", choices=["qwen", "gpt"], default="gpt", help="图片模型提供方，默认 gpt")
    parser.add_argument("--provider", choices=["qwen", "gpt"], dest="image_provider", help="兼容旧参数，等同于 --image-provider")
    parser.add_argument("--test", action="store_true", help="测试模式，不调用API")
    parser.add_argument("--overwrite", action="store_true", help="已存在目标图片时覆盖重生成")
    parser.add_argument("--api-key", type=str, default=None, help="当前 provider 使用的 API Key")
    parser.add_argument("--base-url",
                        default=os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"),
                        help="GPT Image 兼容接口 Base URL（默认读环境变量 GPT_IMAGE_BASE_URL）")
    parser.add_argument("--model", default=None, help="模型名；qwen 默认 qwen-image-edit-plus，gpt 默认 gpt-image-2")
    parser.add_argument("--size", default=None, help="图片尺寸；qwen 格式如 1140*1472，gpt 格式如 1024x1536")
    parser.add_argument("--quality", default="high", help="GPT Image 质量：low / medium / high / auto")
    parser.add_argument("--n", type=int, default=1, help="GPT Image 每个变体生成图片数量")
    parser.add_argument("--response-format", default="", help="GPT Image 返回格式：url / b64_json，留空则不传")
    parser.add_argument("--output-format", default="", help="GPT Image 输出格式：png / jpeg / webp，留空则不传")
    parser.add_argument("--background", default="", help="GPT Image 背景；正式人物透明合同会强制使用 transparent")
    parser.add_argument("--moderation", default="", help="GPT Image 审核：auto / low，留空则不传")
    parser.add_argument("--timeout", type=int, default=300, help="GPT Image 请求超时时间（秒）")
    parser.add_argument("--max-retries", type=int, default=3, help="最大重试次数")
    parser.add_argument("--retry-delay", type=int, default=5, help="重试间隔（秒）")
    parser.add_argument("--batch-delay", type=int, default=10, help="变体间隔（秒）")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not HAS_SQLALCHEMY:
        logger.error("sqlalchemy 未安装")
        sys.exit(1)

    _init_conversion_cache(args.task_id)
    tasks = load_variant_tasks(args.task_id)
    if not tasks:
        logger.info("没有需要图生图的变体任务（所有角色只有一个激活组合）")
        return 0

    logger.info(f"共 {len(tasks)} 个变体任务，provider={args.image_provider}")

    output_dir = Path(OUTPUTS_PATH) / args.task_id / "images" / "characters"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.test:
        for task in tasks:
            base_path = output_dir / task["base_filename"]
            target_path = output_dir / task["target_filename"]
            print(f"\n{'=' * 60}")
            print(f"Provider: {args.image_provider}")
            print(f"基础组合: {task['base_combination']}")
            print(f"目标组合: {task['target_combination']}")
            print(f"基础图: {base_path} {'存在' if base_path.exists() else '不存在'}")
            print(f"目标图: {target_path} {'已存在' if target_path.exists() else '未生成'}")
            print(f"提示词: {task['prompt'][:200]}{'...' if len(task['prompt']) > 200 else ''}")
            print(f"{'=' * 60}")
        return 0

    if _usage_tracker and args.task_id:
        _usage_tracker.set_context(
            args.task_id, "char_designer", force_new_run=True
        )

    resolved_model = args.model or (
        "gpt-image-2" if args.image_provider == "gpt" else "qwen-image-edit-plus"
    )
    output_policy = resolve_character_output_policy(
        args.task_id,
        image_provider=args.image_provider,
        model=resolved_model,
        requested_background=args.background,
        requested_output_format=args.output_format,
    )
    asset_validator = validator_for_policy(output_policy)
    provider = build_provider(args, output_policy)
    providers = ThreadLocalResource(
        lambda: build_provider(args, output_policy), initial=provider
    )
    logger.info("变体人物输出合同: %s", output_policy.digest_payload())

    def process_variant(task):
        base_path = output_dir / task["base_filename"]
        target_path = output_dir / task["target_filename"]

        if not validate_image_file(base_path):
            logger.error(f"基础图片不存在、为空或损坏，跳过: {base_path}")
            return "failed", task

        dependency_digest = canonical_digest(
            {"base_path": base_path.name, "base_sha256": file_sha256(base_path)}
        )
        checkpoint_digest = canonical_digest(
            {
                "prompt": task["prompt"],
                "raw_prompt": task.get("raw_prompt", ""),
                "base_combination": task["base_combination"],
                "target_combination": task["target_combination"],
                "provider": args.image_provider,
                "model": args.model or "gpt-image-2",
                "size": args.size or "1024x1536",
                "quality": args.quality,
                "output_contract": output_policy.digest_payload(),
            }
        )
        if not args.overwrite:
            decision = reusable_or_adopt_existing(
                args.task_id,
                "char_designer",
                "generate_variant_calm",
                task["target_combination"],
                input_digest=checkpoint_digest,
                generator_version=VARIANT_CALM_GENERATOR_VERSION,
                dependency_digest=dependency_digest,
                output_paths=[
                    character_resume_source_path(
                        args.task_id, target_path, fetch_remote=True
                    )
                ],
                validator=asset_validator,
            )
            if decision.reusable:
                restore_character_working_copy(args.task_id, target_path)
                logger.info(
                    "复用有效变体立绘: %s (%s)", target_path, decision.reason
                )
                return "skipped", task

        logger.info(f"{task['base_combination']} -> {task['target_combination']}")

        try:
            assert_retry_budget(
                args.task_id,
                "char_designer",
                "generate_variant_calm",
                task["target_combination"],
                input_digest=checkpoint_digest,
                generator_version=VARIANT_CALM_GENERATOR_VERSION,
                dependency_digest=dependency_digest,
            )
            checkpoint_mark_running(
                args.task_id,
                "char_designer",
                "generate_variant_calm",
                task["target_combination"],
                input_digest=checkpoint_digest,
                generator_version=VARIANT_CALM_GENERATOR_VERSION,
                dependency_digest=dependency_digest,
            )
            provider = providers.get()
            if args.image_provider == "qwen":
                generated_count = generate_with_qwen(
                    provider,
                    base_path,
                    target_path,
                    task["prompt"],
                    task_id=args.task_id,
                )
            else:
                repair_context = None
                if task.get("speaker_id"):
                    repair_context = PromptPersistenceContext(
                        task_id=args.task_id,
                        speaker_id=task["speaker_id"],
                        combination=task["target_combination"],
                        immutable_prefix=IDENTITY_CONSISTENCY_PREFIX,
                    )
                generated_count = generate_with_gpt(
                    provider,
                    base_path,
                    target_path,
                    task["prompt"],
                    task_id=args.task_id,
                    stage=f"generate_variant_calm:{task['target_combination']}",
                    output_policy=output_policy,
                    asset_validator=asset_validator,
                    persistence=repair_context,
                )
        except Exception as e:
            logger.error(f"✗ 图生图失败 {task['target_combination']}: {e}")
            try:
                checkpoint_mark_failed(
                    args.task_id,
                    "char_designer",
                    "generate_variant_calm",
                    task["target_combination"],
                    input_digest=checkpoint_digest,
                    generator_version=VARIANT_CALM_GENERATOR_VERSION,
                    dependency_digest=dependency_digest,
                    error=str(e),
                )
            except Exception:
                logger.exception("记录变体立绘失败 checkpoint 时发生异常")
            return "failed", task

        if generated_count:
            resume_source = publish_character_resume_source(
                args.task_id, target_path
            )
            checkpoint_mark_success(
                args.task_id,
                "char_designer",
                "generate_variant_calm",
                task["target_combination"],
                input_digest=checkpoint_digest,
                generator_version=VARIANT_CALM_GENERATOR_VERSION,
                dependency_digest=dependency_digest,
                output_paths=[resume_source],
                validator=asset_validator,
            )
            logger.info(f"✓ 已保存: {task['target_filename']} ({generated_count} 张)")
            return "success", task
        else:
            logger.error(f"✗ 图生图失败: {task['target_combination']}")
            checkpoint_mark_failed(
                args.task_id,
                "char_designer",
                "generate_variant_calm",
                task["target_combination"],
                input_digest=checkpoint_digest,
                generator_version=VARIANT_CALM_GENERATOR_VERSION,
                dependency_digest=dependency_digest,
                error="Provider 未生成有效变体立绘",
            )
            return "failed", task

    variant_results = run_dependency_layer(
        tasks,
        process_variant,
        concurrency=resolve_image_concurrency("CHAR_VARIANT_IMAGE_CONCURRENCY"),
        stage="character_variant_calm",
        task_id=args.task_id,
        label=lambda task: str(task["target_combination"]),
        logger=logger,
    )
    success_count = sum(1 for status, _ in variant_results if status == "success")
    skipped_count = sum(1 for status, _ in variant_results if status == "skipped")
    failed_count = sum(1 for status, _ in variant_results if status == "failed")

    print(f"\n完成：成功 {success_count}，跳过 {skipped_count}，失败 {failed_count}，总任务 {len(tasks)}，输出目录: {output_dir}")

    if _usage_tracker:
        _usage_tracker.flush()

    # Asset-level failures are recoverable by the parent pipeline and are
    # intentionally distinct from a process-level exit (1).
    return 2 if failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
