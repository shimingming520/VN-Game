#!/usr/bin/env python3
"""生成仅结构化 CG 所需的任务级身份参考图。

普通对话人物复用其已有的 calm LS 图片。对于可见但没有可用普通立绘的人物，会在
``images/references/<entity_id>.png`` 下生成恰好一张可复用的参考图。本阶段不生成
情绪图、景别图、透明抠图、数据库人物记录、音频或背景音乐。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import text


project_root = Path(__file__).resolve().parents[3]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    from dotenv import load_dotenv

    load_dotenv(project_root / ".env", override=False)
except ImportError:
    pass

from common.config import OUTPUTS_PATH  # noqa: E402
from common.cg_data import load_cg_contract  # noqa: E402
from common.platform_db import platform_engine  # noqa: E402
from common.image_concurrency import (  # noqa: E402
    ThreadLocalResource,
    resolve_image_concurrency,
    run_dependency_layer,
)
from services.char_designer.utils.character_identity import (  # noqa: E402
    normalize_resource_id,
)
from common.visual_checkpoint import (  # noqa: E402
    assert_retry_budget,
    canonical_digest,
    mark_failed as checkpoint_mark_failed,
    mark_running as checkpoint_mark_running,
    mark_success as checkpoint_mark_success,
    reusable_or_adopt_existing,
)
from common.visual_debug import record_visual_generation  # noqa: E402
from common.visual_style import (  # noqa: E402
    ASSET_CHARACTER,
    apply_asset_style,
    compose_anchor_guided_prompt,
    resolve_asset_style_plan,
    should_remove_character_background,
)
from services.char_designer.image_providers.gpt_image import (  # noqa: E402
    GPTImageEditProvider,
    GPTImageTextProvider,
)
from services.char_designer.image_providers.qwen_image import (  # noqa: E402
    QwenImageEditProvider,
    QwenTextImageProvider,
)
from services.char_designer.image_providers.wan_image_25 import (  # noqa: E402
    Wan25TextImageProvider,
)
from services.char_designer.utils.prompt_repair import (  # noqa: E402
    execute_with_prompt_repair,
)
from services.char_designer.core.artifact_contract import (  # noqa: E402
    CharacterImageArtifactError,
    atomic_image_output,
    character_resume_source_path,
    publish_character_resume_source,
    restore_character_working_copy,
    validate_image_file,
    validate_transparent_character_file,
)
from services.char_designer.utils.native_transparency import (  # noqa: E402
    inspect_native_transparency,
    is_gpt_image_2,
    resolve_character_output_policy,
    validator_for_policy,
)

try:  # noqa: E402
    from common.usage_tracker import tracker as _usage_tracker
except Exception:  # pragma: no cover - optional runtime integration
    _usage_tracker = None


logger = logging.getLogger("cg_identity_references")
CG_REFERENCE_CONTRACT_SCHEMA = "cg-reference-v1"
CG_IDENTITY_GENERATOR_VERSION = "cg-identity-reference-v3-native-rgba"


def _load_cg_data(task_id: str) -> Dict[str, Any]:
    return load_cg_contract(task_id)


def _safe_task_path(task_dir: Path, relative_path: str) -> Optional[Path]:
    value = str(relative_path or "").strip()
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return None
    resolved = (task_dir / path).resolve()
    try:
        resolved.relative_to(task_dir.resolve())
    except ValueError:
        return None
    return resolved


def collect_identity_requirements(cg_data: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Deduplicate all CG character obligations by stable entity_id."""
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for spec in cg_data.values():
        if not isinstance(spec, Mapping):
            continue
        if spec.get("reference_contract_schema") != CG_REFERENCE_CONTRACT_SCHEMA:
            continue
        for raw in spec.get("reference_requirements") or []:
            if not isinstance(raw, Mapping) or raw.get("role") != "character":
                continue
            policy = str(raw.get("reference_policy") or "").strip()
            if policy not in {"required_identity", "recommended_identity"}:
                continue
            raw_entity_id = raw.get("entity_id")
            entity_id = str(raw_entity_id or "")
            if not entity_id.strip():
                continue
            try:
                entity_id = normalize_resource_id(entity_id, field="CG entity_id")
            except ValueError as exc:
                if raw.get("required"):
                    raise CharacterImageArtifactError(str(exc)) from exc
                logger.warning(
                    "跳过不安全的可选 CG entity_id=%r: %s", entity_id, exc
                )
                continue
            current = merged.get(entity_id)
            if current is None:
                current = dict(raw)
                current["required"] = bool(raw.get("required"))
                merged[entity_id] = current
                order.append(entity_id)
                continue
            current["required"] = bool(current.get("required") or raw.get("required"))
            candidates = list(current.get("file_candidates") or [])
            for candidate in raw.get("file_candidates") or []:
                if candidate not in candidates:
                    candidates.append(candidate)
            current["file_candidates"] = candidates
            if not str(current.get("identity_prompt") or "").strip():
                current["identity_prompt"] = raw.get("identity_prompt")
    return [merged[entity_id] for entity_id in order]


def _existing_candidate(
    task_dir: Path,
    requirement: Mapping[str, Any],
    *,
    validator=validate_image_file,
) -> Optional[Path]:
    for candidate in requirement.get("file_candidates") or []:
        path = _safe_task_path(task_dir, str(candidate))
        if path is not None and validator(path):
            return path
    return None


def _identity_output_path(task_dir: Path, requirement: Mapping[str, Any]) -> Path:
    for candidate in requirement.get("file_candidates") or []:
        if str(candidate).startswith("images/references/"):
            path = _safe_task_path(task_dir, str(candidate))
            if path is not None:
                return path
    raw_entity_id = requirement.get("entity_id") or "CgIdentity"
    try:
        entity_id = normalize_resource_id(raw_entity_id, field="CG entity_id")
    except ValueError as exc:
        raise CharacterImageArtifactError(str(exc)) from exc
    return task_dir / "images" / "references" / f"{entity_id}.png"


def validate_required_identity_references(task_id: str, *, validator=None) -> None:
    """Ensure every required structured-CG identity has a decodable image."""
    if validator is None:
        # The main task resolver defaults missing/unknown model values to
        # gpt-image-2, so validation must use the same fail-closed default.
        image_model = "gpt-image-2"
        try:
            with platform_engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT image_model FROM novel_tasks "
                        "WHERE task_id = :tid LIMIT 1"
                    ),
                    {"tid": task_id},
                ).fetchone()
            image_model = str(row[0] or "gpt-image-2") if row else "gpt-image-2"
        except Exception:
            image_model = "gpt-image-2"
        validator = (
            validate_transparent_character_file
            if is_gpt_image_2(image_model)
            or should_remove_character_background(task_id)
            else validate_image_file
        )
    task_dir = Path(OUTPUTS_PATH).resolve() / task_id
    missing = []
    for requirement in collect_identity_requirements(_load_cg_data(task_id)):
        if not requirement.get("required"):
            continue
        if _existing_candidate(task_dir, requirement, validator=validator) is None:
            missing.append(
                str(requirement.get("entity_name") or requirement.get("entity_id") or "unknown")
            )
    if missing:
        raise CharacterImageArtifactError(
            "required CG identity references are missing or damaged: " + ", ".join(missing)
        )


def _provider_base_url(value: str) -> str:
    base = str(value or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 CG-only 人物身份参考图")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--image-provider", choices=["gpt", "qwen", "wan"], default="gpt")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"))
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--size", default=None)
    parser.add_argument("--quality", default=os.getenv("GPT_IMAGE_QUALITY", "high"))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("API_TIMEOUT", "300")))
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _create_provider(args: argparse.Namespace, uses_anchor: bool, output_policy):
    if args.image_provider in {"qwen", "wan"}:
        if uses_anchor:
            return QwenImageEditProvider(
                api_key=args.api_key,
                base_url=args.base_url,
                model=os.getenv("QWEN_VARIANT_MODEL", "qwen-image-edit-plus"),
                size=args.size or os.getenv("QWEN_EDIT_CHARACTER_SIZE", "1140*1472"),
                max_retries=int(os.getenv("QWEN_MAX_RETRIES", "3")),
                request_timeout=args.timeout,
            )
        if args.image_provider == "qwen":
            return QwenTextImageProvider(
                api_key=args.api_key,
                base_url=args.base_url,
                model=args.model,
                size=args.size,
                max_retries=int(os.getenv("QWEN_MAX_RETRIES", "3")),
                request_timeout=args.timeout,
            )
        return Wan25TextImageProvider(
            api_key=args.api_key,
            model=args.model,
            size=args.size,
            max_retries=int(os.getenv("QWEN_MAX_RETRIES", "3")),
            request_timeout=args.timeout,
        )

    api_key = args.api_key or os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
    provider_class = GPTImageEditProvider if uses_anchor else GPTImageTextProvider
    return provider_class(
        api_key=api_key,
        base_url=_provider_base_url(args.base_url),
        model=args.model or "gpt-image-2",
        size=args.size or "1024x1536",
        quality=args.quality,
        n=1,
        output_format=output_policy.output_format,
        background=output_policy.background,
        timeout_seconds=args.timeout,
        max_retries=int(os.getenv("API_MAX_RETRIES", "3")),
        retry_delay=5,
        transparent_character_output=output_policy.native_transparency,
    )


def _generate_one(
    provider: Any,
    args: argparse.Namespace,
    *,
    prompt: str,
    anchor_path: Optional[Path],
    output_path: Path,
) -> tuple[bool, str]:
    if args.image_provider in {"qwen", "wan"}:
        if anchor_path is not None:
            url = provider.generate(anchor_path, prompt)
        else:
            url = provider.generate(prompt)
        return bool(url and provider.download(url, output_path)), prompt

    operation = (
        (lambda repaired_prompt: provider.edit(anchor_path, repaired_prompt))
        if anchor_path is not None
        else provider.generate
    )
    items, final_prompt = execute_with_prompt_repair(
        operation,
        prompt,
        generation_mode="image_to_image" if anchor_path is not None else "text_to_image",
        stage=f"generate_cg_identity:{output_path.stem}",
    )
    return bool(items and provider.save_image_item(items[0], output_path)), final_prompt


def run(args: argparse.Namespace) -> int:
    cg_data = _load_cg_data(args.task_id)
    requirements = collect_identity_requirements(cg_data)
    if not requirements:
        logger.info("任务 %s 没有新 CG 身份参考需求，跳过", args.task_id)
        return 0

    task_dir = Path(OUTPUTS_PATH).resolve() / args.task_id
    style_plan = resolve_asset_style_plan(args.task_id, ASSET_CHARACTER)
    resolved_model = args.model or {
        "gpt": "gpt-image-2",
        "qwen": "qwen-image-plus",
        "wan": "wan2.5-t2i-preview",
    }[args.image_provider]
    output_policy = resolve_character_output_policy(
        args.task_id,
        image_provider=args.image_provider,
        model=resolved_model,
    )
    asset_validator = validator_for_policy(output_policy)
    logger.info("CG-only 人物输出合同: %s", output_policy.digest_payload())
    pending: List[tuple[Dict[str, Any], Path]] = []
    for requirement in requirements:
        output_path = _identity_output_path(task_dir, requirement)
        existing = _existing_candidate(
            task_dir,
            requirement,
            validator=asset_validator,
        )
        input_digest = canonical_digest(
            {
                "requirement": requirement,
                "style": {
                    "mode": style_plan.mode,
                    "style": style_plan.style,
                    "anchor_sha256": style_plan.anchor_sha256,
                },
                "provider": args.image_provider,
                "model": args.model or "gpt-image-2",
                "quality": args.quality,
                "output_contract": output_policy.digest_payload(),
            }
        )
        try:
            entity_id = normalize_resource_id(
                requirement.get("entity_id") or "CgIdentity",
                field="CG entity_id",
            )
        except ValueError as exc:
            logger.error("CG 身份 entity_id 不安全: %s", exc)
            return False
        if existing is not None and "images/characters" in existing.as_posix():
            checkpoint_mark_success(
                args.task_id,
                "char_designer",
                "generate_cg_identity",
                entity_id,
                input_digest=input_digest,
                generator_version=CG_IDENTITY_GENERATOR_VERSION,
                output_paths=[existing],
                validator=validate_image_file,
                result_mode="reused_character_sprite",
            )
            logger.info(
                "复用 CG 人物身份参考: %s -> %s",
                requirement.get("entity_name"),
                existing,
            )
            continue
        resume_source = character_resume_source_path(
            args.task_id, output_path, fetch_remote=True
        )
        decision = reusable_or_adopt_existing(
            args.task_id,
            "char_designer",
            "generate_cg_identity",
            entity_id,
            input_digest=input_digest,
            generator_version=CG_IDENTITY_GENERATOR_VERSION,
            output_paths=[resume_source],
            validator=asset_validator,
        )
        if decision.reusable:
            restore_character_working_copy(args.task_id, output_path)
            logger.info(
                "复用 CG-only 身份参考 source: %s -> %s",
                requirement.get("entity_name"),
                output_path,
            )
            continue
        pending.append((requirement, output_path))

    if not pending:
        logger.info("全部 CG 人物均已有可复用参考图")
        return 0

    provider = (
        None
        if args.dry_run
        else _create_provider(args, style_plan.uses_anchor, output_policy)
    )
    providers = (
        None
        if args.dry_run
        else ThreadLocalResource(
            lambda: _create_provider(args, style_plan.uses_anchor, output_policy),
            initial=provider,
        )
    )
    if _usage_tracker:
        _usage_tracker.set_context(args.task_id, "char_designer")

    def process_requirement(item):
        requirement, output_path = item
        try:
            entity_id = normalize_resource_id(
                requirement.get("entity_id") or output_path.stem,
                field="CG entity_id",
            )
        except ValueError as exc:
            logger.error("CG 身份 entity_id 不安全: %s", exc)
            return False
        checkpoint_digest = canonical_digest(
            {
                "requirement": requirement,
                "style": {
                    "mode": style_plan.mode,
                    "style": style_plan.style,
                    "anchor_sha256": style_plan.anchor_sha256,
                },
                "provider": args.image_provider,
                "model": args.model or "gpt-image-2",
                "quality": args.quality,
                "output_contract": output_policy.digest_payload(),
            }
        )
        raw_prompt = str(requirement.get("identity_prompt") or "").strip()
        if not raw_prompt:
            logger.error(
                "CG 身份 %s 缺少 identity_prompt",
                requirement.get("entity_name") or requirement.get("entity_id"),
            )
            return bool(not requirement.get("required"))
        styled_prompt = apply_asset_style(style_plan, raw_prompt)
        provider_prompt = compose_anchor_guided_prompt(style_plan, styled_prompt)
        logger.info(
            "CG-only 身份参考: %s -> %s",
            requirement.get("entity_name"),
            output_path,
        )
        if args.dry_run:
            print(provider_prompt)
            return True

        thread_provider = providers.get()
        try:
            assert_retry_budget(
                args.task_id,
                "char_designer",
                "generate_cg_identity",
                entity_id,
                input_digest=checkpoint_digest,
                generator_version=CG_IDENTITY_GENERATOR_VERSION,
            )
            checkpoint_mark_running(
                args.task_id,
                "char_designer",
                "generate_cg_identity",
                entity_id,
                input_digest=checkpoint_digest,
                generator_version=CG_IDENTITY_GENERATOR_VERSION,
            )
            with atomic_image_output(
                output_path,
                artifact_validator=asset_validator,
            ) as temporary_path:
                success, final_prompt = _generate_one(
                    thread_provider,
                    args,
                    prompt=provider_prompt,
                    anchor_path=style_plan.anchor_path if style_plan.uses_anchor else None,
                    output_path=temporary_path,
                )
                if not success:
                    raise CharacterImageArtifactError(
                        f"provider did not produce a valid CG identity image: {output_path}"
                    )
        except Exception as exc:
            logger.error("生成 CG 身份参考失败 %s: %s", output_path, exc)
            try:
                checkpoint_mark_failed(
                    args.task_id,
                    "char_designer",
                    "generate_cg_identity",
                    entity_id,
                    input_digest=checkpoint_digest,
                    generator_version=CG_IDENTITY_GENERATOR_VERSION,
                    error=str(exc),
                )
            except Exception:
                logger.exception("记录 CG-only 身份参考失败 checkpoint 时发生异常")
            success = False
            final_prompt = provider_prompt
        if not success:
            return bool(not requirement.get("required"))

        if _usage_tracker:
            _usage_tracker.add_image(
                1,
                str(getattr(thread_provider, "model", args.model or "")),
                provider=args.image_provider,
            )
        references = (
            [{"path": style_plan.anchor_path, "role": "人物风格 Anchor"}]
            if style_plan.uses_anchor
            else []
        )
        record_visual_generation(
            args.task_id,
            "characters",
            output_path,
            prompt=final_prompt,
            generation_mode="image_to_image" if style_plan.uses_anchor else "text_to_image",
            reference_images=references,
            logic=[
                "CG-only 身份参考图按 entity_id 在整个任务中生成一次并跨章节复用。",
                "该图片只锁定身份，不生成情绪、景别或游戏正式立绘。",
            ],
            model=str(getattr(thread_provider, "model", args.model or "")),
            provider=args.image_provider,
            extra={
                "entity_id": requirement.get("entity_id"),
                "entity_name": requirement.get("entity_name"),
                "cg_identity_reference": True,
                "character_output_policy": output_policy.digest_payload(),
                "transparency_metrics": (
                    inspect_native_transparency(output_path)
                    if output_policy.require_transparency
                    else {}
                ),
            },
        )
        resume_source = publish_character_resume_source(args.task_id, output_path)
        checkpoint_mark_success(
            args.task_id,
            "char_designer",
            "generate_cg_identity",
            entity_id,
            input_digest=checkpoint_digest,
            generator_version=CG_IDENTITY_GENERATOR_VERSION,
            output_paths=[resume_source],
            validator=asset_validator,
        )
        return True

    generation_results = run_dependency_layer(
        pending,
        process_requirement,
        concurrency=resolve_image_concurrency("CHAR_CG_IDENTITY_IMAGE_CONCURRENCY"),
        stage="character_cg_identity",
        task_id=args.task_id,
        label=lambda item: str(
            item[0].get("entity_name") or item[0].get("entity_id")
        ),
        logger=logger,
    )
    failures = sum(1 for success in generation_results if not success)

    if _usage_tracker:
        _usage_tracker.flush()
    if not args.dry_run:
        try:
            validate_required_identity_references(
                args.task_id,
                validator=asset_validator,
            )
        except CharacterImageArtifactError as exc:
            logger.error("CG 身份参考完整性校验失败: %s", exc)
            failures += 1
    # Required identity failures are reported as missing CG dependencies; they
    # must not prevent unrelated character assets from being consumed.
    return 2 if failures else 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
