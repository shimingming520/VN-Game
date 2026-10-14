"""Shared access to the task-level LLM-generated visual style."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple

from sqlalchemy import text

from common.artifact_io import atomic_write_bytes
from common.config import OUTPUTS_PATH, REDIS_URL
from common.platform_db import platform_engine
from common.story_tree_locale import (
    select_story_tree_for_consumer,
    update_story_tree_locale_fields,
)


logger = logging.getLogger(__name__)

GLOBAL_STYLE_MODEL = "gpt-5.6-sol"
GLOBAL_STYLE_PROMPT_VERSION = "global-style-v3-visual-style-context"
VISUAL_STYLE_CONTEXT_SCHEMA = "visual-style-context-v1"
GLOBAL_STYLE_META_KEY = "global_style_generation"


def _calculate_publication_fingerprint(
    md_contents: Mapping[str, str],
    json_data: Mapping[str, Any],
) -> str:
    """Calculate the publication fingerprint without importing story_planner."""
    payload = {
        "md": {str(key): md_contents[key] for key in sorted(md_contents)},
        "json": json_data,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class GlobalStyleMissingError(ValueError):
    """Raised when a task has not published a global style yet."""


def ensure_global_style(task_id: str, prompt: str) -> str:
    """Prefix a provider prompt with the task's strict global style."""
    return prepend_global_style(get_global_style(task_id), prompt)


def prepend_global_style(style: str, prompt: str) -> str:
    """Prefix prompt with a previously resolved non-empty style."""
    style = str(style or "").strip()
    if not style:
        raise GlobalStyleMissingError("global_style 不能为空")
    prompt = str(prompt or "").strip()
    if not prompt:
        return style
    if style in prompt:
        return prompt
    return f"{style}, {prompt}"


def _load_story_artifacts(
    task_id: str,
    *,
    db_engine=platform_engine,
) -> Tuple[Dict[str, Any], Iterable[Tuple[str, str]]]:
    if not task_id:
        raise GlobalStyleMissingError("task_id 不能为空，无法读取 global_style")

    with db_engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM story_tree_json WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id},
        ).fetchone()
        if not row:
            raise GlobalStyleMissingError(
                f"story_tree_json 中找不到 task_id={task_id}，请先运行 story_planner"
            )

        payload: Any = row[0]
        if isinstance(payload, (str, bytes, bytearray)):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise GlobalStyleMissingError(
                f"任务 {task_id} 的 story_tree_json.data 不是有效 JSON object"
            )

        data = select_story_tree_for_consumer(payload)
        if not isinstance(data.get("nodes"), dict) and not str(
            data.get("global_style") or ""
        ).strip():
            raise GlobalStyleMissingError(
                f"任务 {task_id} 的 story_tree_json 缺少 global_style 或可用的语言版本"
            )

        md_rows = []
        if "visual_style_context" not in data:
            try:
                md_rows = conn.execute(
                    text(
                        "SELECT name, data FROM story_tree_md "
                        "WHERE task_id = :tid ORDER BY name"
                    ),
                    {"tid": task_id},
                ).fetchall()
            except Exception:
                # Lightweight unit-test engines may model only story_tree_json.
                # Production preflight separately guarantees story_tree_md exists.
                md_rows = []
    return data, [(str(item[0]), str(item[1] or "")) for item in md_rows]


def get_global_style(task_id: str, *, db_engine=platform_engine) -> str:
    """Return story_tree_json.data.global_style for task_id."""
    data, _ = _load_story_artifacts(task_id, db_engine=db_engine)
    style = str(data.get("global_style") or "").strip()
    if not style:
        raise GlobalStyleMissingError(
            f"任务 {task_id} 缺少 story_tree_json.data.global_style，请先运行 char_designer"
        )
    return style


def get_current_global_style(task_id: str, *, db_engine=platform_engine) -> str:
    """Return the published style only when its input/version contract is current.

    This is the read-only synchronization point used by a concurrently started
    scene_painter.  It must never accept a stale historical value merely because
    ``global_style`` is non-empty.
    """
    data, md_rows = _load_story_artifacts(task_id, db_engine=db_engine)
    style = str(data.get("global_style") or "").strip()
    if not style:
        return ""
    metadata = data.get(GLOBAL_STYLE_META_KEY)
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    if (
        metadata.get("input_digest") != _global_style_input_digest(data, md_rows)
        or metadata.get("prompt_version") != GLOBAL_STYLE_PROMPT_VERSION
        or metadata.get("model") != GLOBAL_STYLE_MODEL
    ):
        return ""
    return style


def _style_context(
    story_tree: Dict[str, Any],
    md_rows: Iterable[Tuple[str, str]],
) -> str:
    sample_limit = max(1000, int(os.getenv("GLOBAL_STYLE_SAMPLE_CHARS", "24000")))
    if "visual_style_context" in story_tree:
        visual_context = story_tree.get("visual_style_context")
        if not isinstance(visual_context, Mapping):
            raise GlobalStyleMissingError(
                "story_tree_json.data.visual_style_context 必须是 JSON object"
            )
        if visual_context.get("schema_version") != VISUAL_STYLE_CONTEXT_SCHEMA:
            raise GlobalStyleMissingError(
                "visual_style_context.schema_version 无效: "
                f"{visual_context.get('schema_version') or '(empty)'}"
            )
        useful_fields = (
            visual_context.get("representative_excerpts"),
            visual_context.get("worldview"),
            visual_context.get("chapter_summaries"),
            visual_context.get("narrative_structure"),
        )
        if not any(useful_fields):
            raise GlobalStyleMissingError(
                "visual_style_context 缺少可用于判断题材与视觉风格的内容"
            )
        serialized = json.dumps(
            dict(visual_context),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (
            "Authoritative whole-book visual style context:\n" + serialized
        )[:sample_limit]

    # Historical tasks created before visual-style-context-v1 keep the old
    # story-tree/Markdown aggregation route. New tasks always publish the
    # structured context in story_planner.
    tree_copy = dict(story_tree)
    tree_copy.pop("global_style", None)
    tree_copy.pop(GLOBAL_STYLE_META_KEY, None)
    tree_copy.pop("publication", None)
    parts = [
        "Story tree metadata:\n"
        + json.dumps(tree_copy, ensure_ascii=False, separators=(",", ":"))
    ]
    for name, content in md_rows:
        if content.strip():
            parts.append(f"Chapter artifact {name}:\n{content.strip()}")
    return "\n\n".join(parts)[:sample_limit]


def _style_context_source(story_tree: Mapping[str, Any]) -> str:
    if "visual_style_context" in story_tree:
        context = story_tree.get("visual_style_context")
        if isinstance(context, Mapping):
            return str(context.get("schema_version") or "visual_style_context")
        return "invalid_visual_style_context"
    return "legacy_story_tree_and_markdown"


def _global_style_input_digest(
    story_tree: Mapping[str, Any], md_rows: Iterable[Tuple[str, str]]
) -> str:
    import hashlib

    context = _style_context(dict(story_tree), md_rows)
    payload = json.dumps(
        {
            "context": context,
            "source": _style_context_source(story_tree),
            "prompt_version": GLOBAL_STYLE_PROMPT_VERSION,
            "model": GLOBAL_STYLE_MODEL,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_style_response(content: str) -> str:
    raw = str(content or "").strip()
    fence = chr(96) * 3
    if raw.startswith(fence):
        raw = re.sub(r"^" + re.escape(fence) + r"(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*" + re.escape(fence) + r"$", "", raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise ValueError("LLM 未返回有效 JSON object")
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("LLM 返回内容不是 JSON object")
    style = re.sub(
        r"\s+", " ", str(payload.get("global_style") or "").strip()
    ).strip(" ,.;")
    if not style:
        raise ValueError("LLM 未返回有效的 global_style")
    if len(style) > 600:
        raise ValueError(f"LLM 返回的 global_style 过长: {len(style)} chars")
    return style


def _generate_style_with_llm(context: str) -> str:
    from openai import OpenAI

    api_key = os.getenv("STORY_PLANNER_LLM_API_KEY", "").strip()
    if not api_key:
        raise GlobalStyleMissingError(
            "缺少 STORY_PLANNER_LLM_API_KEY，char_designer 无法生成 global_style"
        )
    base_url = os.getenv(
        "STORY_PLANNER_LLM_BASE_URL", "https://ai.farmmx.com/v1"
    ).strip()
    timeout = max(1, int(os.getenv("STORY_PLANNER_LLM_TIMEOUT", "1800")))
    system_prompt = (
        "You are the global art director for a visual-novel image pipeline. "
        "Infer one coherent visual style from the supplied whole-book context's genre, era, tone, and world. "
        "Return exactly one JSON object with the key global_style. "
        "The value must be a concise English prompt phrase ready to guide every character, background, "
        "event CG, and cover image. Describe only medium, rendering technique or brushwork, color language, "
        "lighting, material texture, and overall aesthetic. Do not describe specific characters, clothing, "
        "social class, locations, architecture, objects, actions, composition, camera, aspect ratio, or image "
        "quality. Do not name living or dead artists. Do not add markdown or commentary."
    )
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_retries=max(0, int(os.getenv("GLOBAL_STYLE_LLM_MAX_RETRIES", "2"))),
    )
    response = client.chat.completions.create(
        model=GLOBAL_STYLE_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Story artifacts:\n{context}"},
        ],
        temperature=0.2,
        max_tokens=300,
        timeout=timeout,
    )
    try:
        from common.usage_tracker import tracker as usage_tracker
        if getattr(response, "usage", None):
            usage_tracker.add_llm_usage(
                response.usage,
                GLOBAL_STYLE_MODEL,
                provider="openai_compatible",
            )
    except Exception as exc:
        logger.warning("global_style 用量记录失败（不影响生成）: %s", exc)
    return _parse_style_response(response.choices[0].message.content)


def _write_story_tree_file(task_id: str, data: Dict[str, Any]) -> Path:
    target = Path(OUTPUTS_PATH).resolve() / task_id / "story_tree.json"
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    atomic_write_bytes(target, payload)
    return target


def _publish_global_style(
    task_id: str,
    style: str,
    *,
    input_digest: str = "",
    context_source: str = "",
    db_engine=platform_engine,
) -> Dict[str, Any]:
    from common.progress_utils import ensure_visual_stage_ownership

    ensure_visual_stage_ownership(task_id)
    with db_engine.begin() as conn:
        query = "SELECT data FROM story_tree_json WHERE task_id = :tid LIMIT 1"
        if conn.dialect.name == "postgresql":
            query += " FOR UPDATE"
        row = conn.execute(text(query), {"tid": task_id}).fetchone()
        if not row:
            raise GlobalStyleMissingError(
                f"story_tree_json 中找不到 task_id={task_id}，无法发布 global_style"
            )
        payload: Any = row[0]
        if isinstance(payload, (str, bytes, bytearray)):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise GlobalStyleMissingError("story_tree_json.data 不是有效 JSON object")
        updates: Dict[str, Any] = {"global_style": style}
        if input_digest:
            updates[GLOBAL_STYLE_META_KEY] = {
                "input_digest": input_digest,
                "prompt_version": GLOBAL_STYLE_PROMPT_VERSION,
                "model": GLOBAL_STYLE_MODEL,
                "context_source": context_source,
            }
        published_payload = update_story_tree_locale_fields(payload, updates)
        publication = published_payload.get("publication")
        if isinstance(publication, Mapping):
            md_rows = conn.execute(
                text(
                    "SELECT name, data FROM story_tree_md "
                    "WHERE task_id = :tid ORDER BY name"
                ),
                {"tid": task_id},
            ).fetchall()
            fingerprint_payload = deepcopy(published_payload)
            fingerprint_payload.pop("publication", None)
            publication = deepcopy(dict(publication))
            publication["fingerprint"] = _calculate_publication_fingerprint(
                {str(name): str(content or "") for name, content in md_rows},
                fingerprint_payload,
            )
            published_payload["publication"] = publication
        serialized = json.dumps(published_payload, ensure_ascii=False)
        if conn.dialect.name == "postgresql":
            result = conn.execute(
                text(
                    "UPDATE story_tree_json SET data = CAST(:data AS jsonb), "
                    "update_time = NOW() WHERE task_id = :tid"
                ),
                {"data": serialized, "tid": task_id},
            )
        else:
            result = conn.execute(
                text("UPDATE story_tree_json SET data = :data WHERE task_id = :tid"),
                {"data": serialized, "tid": task_id},
            )
        if result.rowcount != 1:
            raise GlobalStyleMissingError(
                f"global_style 发布未命中唯一任务记录: task_id={task_id}"
            )
    ensure_visual_stage_ownership(task_id)
    _write_story_tree_file(task_id, published_payload)
    return select_story_tree_for_consumer(published_payload)


def ensure_global_style_generated(
    task_id: str,
    *,
    db_engine=platform_engine,
) -> str:
    """Generate and publish exactly one task-level style, idempotently."""
    data, md_rows = _load_story_artifacts(task_id, db_engine=db_engine)
    input_digest = _global_style_input_digest(data, md_rows)
    existing = str(data.get("global_style") or "").strip()
    metadata = data.get(GLOBAL_STYLE_META_KEY)
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    metadata_matches = (
        metadata.get("input_digest") == input_digest
        and metadata.get("prompt_version") == GLOBAL_STYLE_PROMPT_VERSION
        and metadata.get("model") == GLOBAL_STYLE_MODEL
    )
    if existing and metadata_matches:
        _write_story_tree_file(task_id, data)
        return existing
    if existing and not metadata:
        # Adopt one historical value against the current whole-book context so
        # the next context/prompt-version change can invalidate it deterministically.
        _publish_global_style(
            task_id,
            existing,
            input_digest=input_digest,
            context_source=_style_context_source(data),
            db_engine=db_engine,
        )
        return existing

    import redis

    redis_client = redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    lock = redis_client.lock(
        f"novel-game:global-style:{task_id}",
        timeout=max(60, int(os.getenv("GLOBAL_STYLE_LOCK_TTL", "900"))),
        blocking_timeout=max(
            1, int(os.getenv("GLOBAL_STYLE_LOCK_WAIT_SECONDS", "900"))
        ),
    )
    if not lock.acquire(blocking=True):
        raise TimeoutError(f"等待 global_style 生成锁超时: task_id={task_id}")
    try:
        story_tree, md_rows = _load_story_artifacts(task_id, db_engine=db_engine)
        input_digest = _global_style_input_digest(story_tree, md_rows)
        existing = str(story_tree.get("global_style") or "").strip()
        metadata = story_tree.get(GLOBAL_STYLE_META_KEY)
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        if existing and (
            metadata.get("input_digest") == input_digest
            and metadata.get("prompt_version") == GLOBAL_STYLE_PROMPT_VERSION
            and metadata.get("model") == GLOBAL_STYLE_MODEL
        ):
            return existing
        context_source = _style_context_source(story_tree)
        context = _style_context(story_tree, md_rows)
        if not context.strip():
            raise GlobalStyleMissingError(
                f"任务 {task_id} 缺少可用于判断题材的故事树内容"
            )
        if context_source == "legacy_story_tree_and_markdown":
            logger.warning(
                "任务 %s 缺少 visual_style_context，按历史任务兼容逻辑生成 global_style",
                task_id,
            )
        logger.info(
            "global_style 输入已解析: task_id=%s source=%s context_chars=%s",
            task_id,
            context_source,
            len(context),
        )
        style = _generate_style_with_llm(context)
        _publish_global_style(
            task_id,
            style,
            input_digest=input_digest,
            context_source=context_source,
            db_engine=db_engine,
        )
        logger.info(
            "global_style 已延迟发布: task_id=%s version=%s source=%s style=%s",
            task_id,
            GLOBAL_STYLE_PROMPT_VERSION,
            context_source,
            style,
        )
        return style
    finally:
        try:
            lock.release()
        except redis.exceptions.LockError:
            logger.warning("global_style 锁已过期或被释放: task_id=%s", task_id)
