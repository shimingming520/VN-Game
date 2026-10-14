"""Story planner tasks - parse novel to draft."""
from copy import deepcopy
import json
import errno
import hashlib
import os
import re
import uuid
import time
import logging
from typing import Any
from pathlib import Path
from common.artifact_io import atomic_write_bytes as _common_atomic_write_bytes
from common.celery_app import celery_app
from common.config import RAW_NOVELS_PATH
from common.task_models import StoryMode
from common.platform_db import platform_engine
from services.story_planner.table.models import engine, Worldview, WorldviewStatus, Outline, OutlineStatus, ChapterBranchPlan, StorySummary, StoryTreeJson, StoryTreeMd
from services.story_planner.table.story_tree_to_db import (
    publish_story_tree_transactionally,
    save_story_md_to_db,
)
from services.story_planner.core import WorldAnalyst, StoryGenerator
from services.story_planner.core.llm_config import (
    DEFAULT_MODEL,
    STORY_PLANNER_GATEWAY_PROVIDER,
)
from services.story_planner.core.runtime_config import RUNTIME_SETTINGS
from services.story_planner.core.auxiliary_task_guard import auxiliary_task_guard
from services.story_planner.core.novel_preflight import (
    calibrate_chapter_branch_preflight,
    run_novel_preflight,
)
from services.story_planner.core.novel_text_encoding import read_novel_text
from services.story_planner.core.visual_style_context import (
    build_visual_style_context,
)
from services.story_planner.core.chapter_title import (
    normalize_chapter_title,
    untitled_chapter_title,
)
from services.story_planner.core.story_tree_player_localization import (
    build_story_tree_language_bundle,
    get_story_tree_player_language,
    wrap_story_tree_language_bundles,
)
from services.story_planner.core.runtime_observability import (
    classify_runtime_error,
    stop_runtime_heartbeat as _stop_runtime_heartbeat,
    structured_log as _structured_runtime_log,
)
from sqlmodel import Session, select

try:
    from common.usage_tracker import tracker as _usage_tracker, init_usage_table
    if os.getenv("STORY_PLANNER_SMOKE_MODE", "0") != "1":
        init_usage_table()
except Exception:
    _usage_tracker = None

# 进度更新（解耦，不影响主逻辑）
# 注意：此处不再使用 try/except ImportError fallback，导入失败会直接报错，
# 避免 fail_stage 等函数变成空函数导致任务状态永远卡在"处理中"。
# 原 fallback 代码如下，仅供参考：
#   except ImportError:
#       PROGRESS_ENABLED = False
#       def start_stage(*args, **kwargs): pass
#       def complete_stage(*args, **kwargs): pass
#       def fail_stage(*args, **kwargs): pass
#       def update_task_progress(*args, **kwargs): pass
#       def is_stage_already_running(*args, **kwargs): return False
#       def wait_for_stage_completion(*args, **kwargs): pass
from common.progress_utils import (
    StageLeaseLostError,
    complete_stage,
    ensure_stage_lease_owned,
    fail_stage,
    is_stage_already_running,
    start_stage,
    update_task_progress,
    wait_for_stage_completion,
)
PROGRESS_ENABLED = True
_runtime_logger = logging.getLogger("services.story_planner.chapter_branch_runtime")


def _read_and_normalize_novel(novel_path: Path) -> str:
    result = read_novel_text(novel_path, convert_to_utf8=True)
    if result.converted:
        print(
            f"📝 已将小说从 {result.source_encoding} 原子转换为无 BOM UTF-8: "
            f"{novel_path}（原始 {result.byte_count} 字节）"
        )
    return result.text


def _validate_json_artifact(path) -> bool:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, (dict, list))


def atomic_write_bytes(path, data: bytes, *, validator=None) -> None:
    """Use the common publisher, with a Windows-only local test fallback."""
    try:
        _common_atomic_write_bytes(path, data, validator=validator)
        return
    except OSError as exc:
        if os.name != "nt" or exc.errno != errno.EBADF:
            raise

    final_path = Path(path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = final_path.with_name(
        f".{final_path.name}.{os.getpid()}.{uuid.uuid4().hex}.part"
    )
    try:
        with temporary.open("wb") as file_obj:
            file_obj.write(data)
            file_obj.flush()
        if validator is not None and not validator(temporary):
            raise RuntimeError(f"artifact validation failed: {temporary}")
        os.replace(temporary, final_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_utf8_text_artifact(path) -> bool:
    try:
        content = Path(path).read_bytes()
        decoded = content.decode("utf-8")
    except (OSError, UnicodeError):
        return False
    return bool(decoded.strip()) and not content.startswith(b"\xef\xbb\xbf")


def _atomic_write_utf8_text(path: Path, content: str) -> None:
    atomic_write_bytes(
        path,
        str(content).encode("utf-8"),
        validator=_validate_utf8_text_artifact,
    )


_CONTENT_WARNING_SOURCE_SCHEMA_VERSION = 1
_CHAPTER_PROCESSING_CHECKPOINT_SCHEMA_VERSION = 1


def _chapter_processing_checkpoint_path(task_id: str) -> Path:
    """Return the shared-PVC path for the processed chapter boundary artifact."""

    safe_task_id = re.sub(r"[^0-9A-Za-z._-]+", "_", str(task_id)).strip("._")
    if not safe_task_id:
        raise ValueError("chapter processing task_id cannot be empty")
    return Path(RAW_NOVELS_PATH) / f"{safe_task_id}.chapters.json"


def _persist_chapter_processing_checkpoint(task_id: str, checkpoint: dict) -> dict:
    """Write processed chapters atomically and return compact plan metadata."""

    path = _chapter_processing_checkpoint_path(task_id)
    payload = dict(checkpoint or {})
    payload["schema_version"] = _CHAPTER_PROCESSING_CHECKPOINT_SCHEMA_VERSION
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    atomic_write_bytes(path, raw, validator=_validate_json_artifact)
    return {
        key: value for key, value in payload.items() if key != "chapters"
    } | {
        # Persist a portable filename rather than a host-specific absolute
        # path; the same checkpoint may be resumed by another worker/pod.
        "artifact_path": path.name,
        "artifact_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _load_chapter_processing_checkpoint(task_id: str, metadata: dict | None = None) -> dict | None:
    """Load and validate the shared-PVC chapter artifact, failing closed."""

    value = metadata if isinstance(metadata, dict) else {}
    stored_path = Path(str(value.get("artifact_path") or ""))
    path = Path(RAW_NOVELS_PATH) / (
        stored_path.name or _chapter_processing_checkpoint_path(task_id).name
    )
    try:
        raw = path.read_bytes()
        expected = str(value.get("artifact_sha256") or "")
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _content_warning_sha256(content: str) -> str:
    """Hash sidecar content without importing the runtime module.

    The sidecar helpers run before the chapter-branch runtime imports its own
    digest utility.  Keeping this tiny helper local avoids an import-order
    dependency during task redelivery and startup.
    """

    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _content_warning_source_paths(task_id: str) -> tuple[Path, Path]:
    """Return the shared-PVC source artifact and metadata paths for a task."""

    safe_task_id = re.sub(r"[^0-9A-Za-z._-]+", "_", str(task_id)).strip("._")
    if not safe_task_id:
        raise ValueError("content-warning source task_id 不能为空")
    root = Path(RAW_NOVELS_PATH)
    return (
        root / f"{safe_task_id}.source.txt",
        root / f"{safe_task_id}.source.meta.json",
    )


def _persist_content_warning_source(
    task_id: str,
    *,
    source_text: str,
    working_text: str,
    source_language: str,
    target_language: str,
) -> bool:
    """Persist the canonical pre-translation text for future redeliveries.

    The normal novel file may be overwritten by the language-adaptation step.
    This small sidecar is stored beside it on the shared raw-novel volume and
    is written atomically, so a restarted worker can still perform the same
    content-warning scan instead of silently scanning only translated text.
    """

    # Do not invoke the LLM-backed front-matter cleaner from this persistence
    # path. Sidecar capture runs before the main analysis and must be cheap and
    # deterministic; the detector can safely ignore non-story preamble text.
    canonical_source = _strip_act_title_prefix(str(source_text or ""))
    if not canonical_source.strip():
        return False
    working_value = str(working_text or "")
    source_path, metadata_path = _content_warning_source_paths(task_id)
    source_digest = _content_warning_sha256(canonical_source)
    working_digest = _content_warning_sha256(working_value)
    source_path.parent.mkdir(parents=True, exist_ok=True)

    existing_meta: dict[str, object] = {}
    try:
        if metadata_path.exists():
            loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_meta = loaded
    except (OSError, UnicodeError, json.JSONDecodeError):
        existing_meta = {}

    existing_source_digest = str(existing_meta.get("source_fingerprint") or "")
    existing_working_digest = str(existing_meta.get("working_fingerprint") or "")
    source_file_valid = False
    if source_path.exists() and existing_source_digest:
        try:
            source_file_valid = (
                _content_warning_sha256(source_path.read_text(encoding="utf-8"))
                == existing_source_digest
            )
        except (OSError, UnicodeError):
            source_file_valid = False

    # Do not replace a known canonical source with a translated working file
    # when two deliveries overlap.  A changed source is accepted only when the
    # caller proves that the current working text is itself the new source.
    preserve_existing = source_file_valid and (
        source_digest != existing_source_digest
        and working_digest == existing_working_digest
    )
    if not preserve_existing:
        _atomic_write_utf8_text(source_path, canonical_source)
        existing_source_digest = source_digest

    metadata = {
        "schema_version": _CONTENT_WARNING_SOURCE_SCHEMA_VERSION,
        "task_id": str(task_id),
        "source_fingerprint": existing_source_digest or source_digest,
        "working_fingerprint": working_digest,
        "source_language": str(
            existing_meta.get("source_language")
            if source_file_valid and source_digest == existing_source_digest
            else (source_language or "unknown")
        ),
        "target_language": str(
            existing_meta.get("target_language")
            if source_file_valid and working_digest == existing_working_digest
            else (target_language or source_language or "unknown")
        ),
    }
    atomic_write_bytes(
        metadata_path,
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
        validator=_validate_json_artifact,
    )
    return True


def _load_content_warning_source(task_id: str, working_text: str) -> str | None:
    """Load a matching canonical source sidecar, or return ``None`` safely."""

    try:
        source_path, metadata_path = _content_warning_source_paths(task_id)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or int(metadata.get("schema_version", 0) or 0) != _CONTENT_WARNING_SOURCE_SCHEMA_VERSION:
            return None
        current_digest = _content_warning_sha256(str(working_text or ""))
        known_digests = {
            str(metadata.get("source_fingerprint") or ""),
            str(metadata.get("working_fingerprint") or ""),
        }
        if not current_digest or current_digest not in known_digests:
            return None
        source = source_path.read_text(encoding="utf-8")
        if not source.strip() or _content_warning_sha256(source) != str(metadata.get("source_fingerprint") or ""):
            return None
        return source
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _get_task_config(task_id: str) -> dict:
    """
    从 novel_tasks 表查询任务配置。

    Returns:
        dict: {novel_filename, project_name, generated_project_name,
        output_language, art_style, story_mode}
    """
    from common.task_models import NovelTask
    from common.platform_db import platform_engine as task_engine  # novel_tasks 已迁平台库
    with Session(task_engine) as session:
        task = session.exec(
            select(NovelTask).where(NovelTask.task_id == task_id)
        ).first()
        if not task:
            raise ValueError(f"novel_tasks 中找不到 task_id={task_id}")
        return {
            "novel_filename": task.novel_filename,
            "project_name": getattr(task, "project_name", "") or "",
            "generated_project_name": getattr(task, "generated_project_name", "") or "",
            "output_language": task.output_language,
            "art_style": task.art_style,
            "story_mode": task.story_mode,
        }


def _build_story_tree_book_metadata(
    config: dict | None,
    *,
    novel_filename: str = "",
    task_id: str = "",
    narrative_arc: str = "",
    chapter_summaries: list[str] | None = None,
    story_summary: dict | str | None = None,
) -> dict[str, str]:
    """Build canonical work-level metadata before locale localization.

    The planner owns the source-language copy.  Every locale snapshot is then
    produced from this same canonical title/summary, so downstream publishers
    never need to guess from a missing locale or a generated node title.
    """
    values = config if isinstance(config, dict) else {}

    def _clean(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    title = next(
        (
            _clean(values.get(key))
            for key in ("project_name", "generated_project_name")
            if _clean(values.get(key))
        ),
        "",
    )
    if not title:
        title = _clean(Path(str(novel_filename or "")).stem)
    title = title or _clean(task_id) or "未命名作品"

    summary = ""
    if isinstance(story_summary, dict):
        for key in ("book_summary", "summary", "one_liner", "outline", "background"):
            summary = _clean(story_summary.get(key))
            if summary:
                break
    elif story_summary:
        summary = _clean(story_summary)
    if not summary:
        summary = _clean(narrative_arc)
    if not summary:
        for item in chapter_summaries or []:
            summary = _clean(item)
            if summary:
                break
    if not summary:
        summary = f"{title}的互动故事，玩家的选择将影响后续剧情。"
    return {
        "book_title": title[:90],
        "book_summary": summary[:450],
    }


def _extract_source_chapter_title(chapter_text: str, chapter_number: int) -> str:
    """Extract a real heading from a source chapter, never prose mentions."""
    fallback = untitled_chapter_title("zh-CN")
    for line in str(chapter_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _EXPLICIT_CHAPTER_HEADING_RE.fullmatch(stripped):
            return normalize_chapter_title(stripped, language="zh-CN") or fallback
        # The first non-empty line is narrative; do not scan later lines where
        # phrases such as “第一章” may be mentioned in dialogue.
        break
    return fallback


def _extract_source_chapter_summary(chapter_text: str, chapter_number: int) -> str:
    """Build a deterministic player summary from the chapter's narrative text."""
    lines = [line.strip() for line in str(chapter_text or "").splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    if lines and _EXPLICIT_CHAPTER_HEADING_RE.fullmatch(lines[0]):
        lines.pop(0)
    narrative = re.sub(r"\s+", " ", " ".join(line for line in lines if line)).strip()
    if not narrative:
        return f"第{max(int(chapter_number or 1), 1)}章"
    if len(narrative) <= 450:
        return narrative
    boundary = max(
        narrative.rfind(mark, 0, 450)
        for mark in ("。", "！", "？", ".", "!", "?")
    )
    return narrative[: boundary + 1 if boundary >= 80 else 450].rstrip()


def _detect_novel_language(text: str) -> str:
    """检测小说文本语言，返回 'zh'/'en'/'ja'/'other'"""
    return WorldAnalyst.detect_language(text)


def _need_translation(novel_lang: str, output_language: str) -> bool:
    """
    判断是否需要翻译。
    规则：小说语言与输出语言不一致时需要翻译。
    例：小说中文(zh) + 输出英文(en-US) -> 需要翻译
        小说中文(zh) + 输出中文(zh-CN) -> 不需要
    """
    lang_prefix_map = {
        "zh-CN": "zh", "zh-TW": "zh",
        "en-US": "en", "en-GB": "en",
        "ja-JP": "ja",
        "ko-KR": "ko",
        "fr-FR": "fr",
        "de-DE": "de",
        "es-ES": "es",
    }
    output_prefix = lang_prefix_map.get(output_language, output_language.split("-")[0])
    return novel_lang != output_prefix


def _strip_front_matter(chapter_text: str, provider: str = None) -> str:
    """Use an LLM to locate and remove only confirmed opening metadata.

    The model sees at most the first 10,000 characters and returns a verbatim
    boundary anchor. The source text is sliced locally, so narrative content is
    never rewritten by the model.
    """
    from services.story_planner.core.llm_client import LLMClient
    if provider is None:
        provider = STORY_PLANNER_GATEWAY_PROVIDER

    # 只将开头最多 10000 字交给 LLM；正文始终从原文切片，不接受模型改写。
    sample_limit = 10000
    sample = chapter_text[:sample_limit]

    system_prompt = (
        "You are a text analysis assistant. "
        "The user gives you the beginning of a novel text (at most 10,000 characters). "
        "It may contain front matter before the actual story: things like title, author name, genre tags, "
        "publisher info, word count, URLs, status (Completed/Ongoing), warnings, table of contents chapter list, etc. "
        "These are NOT story content. Any narrative content in the sample must be preserved. "
        "Your task:\n"
        "1. Decide if there is any front matter at the very beginning.\n"
        "2. If YES: output ONLY the last line of the front matter (the line immediately before the story begins), copied verbatim.\n"
        "3. If NO front matter: output exactly: NO_FRONT_MATTER\n"
        "Output nothing else."
    )
    user_prompt = sample
    try:
        client = LLMClient(provider=provider, temperature=0.0, max_tokens=300)
        result = client.chat_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout=60,
        ).strip()
        if result == "NO_FRONT_MATTER" or not result:
            print("   未检测到头部元信息，保持原文")
            return chapter_text
        # 只在采样前缀内定位锚点，避免正文后部重复短语被误识别。
        anchor = result[:200].strip()
        idx = sample.find(anchor)
        if idx > 0:
            # 模型偶尔会把首个章节标题误当成“元信息最后一行”。标题本身
            # 必须保留，此时仅切到该标题起点，不删除标题行。
            first_heading = _EXPLICIT_CHAPTER_HEADING_RE.search(sample)
            if first_heading and idx >= first_heading.start():
                if idx == first_heading.start():
                    cut_pos = first_heading.start()
                    cleaned = chapter_text[cut_pos:].lstrip()
                    print(f"   已通过 LLM 定位首个章节标题，跳过 {cut_pos} 字的头部元信息")
                    return cleaned
                print("   LLM 返回的边界位于正文中，保持原文避免误删")
                return chapter_text
            # 跳过该行到行尾
            end_of_line = chapter_text.find('\n', idx, sample_limit)
            cut_pos = end_of_line + 1 if end_of_line != -1 else idx + len(result)
            # 安全上限：只允许清除采样前缀，绝不触及后续正文。
            if cut_pos > sample_limit:
                print(f"⚠️  元信息边界超过安全上限，跳过清洗")
                return chapter_text
            print(f"   已通过 LLM 跳过 {cut_pos} 字的头部元信息")
            cleaned = chapter_text[cut_pos:].lstrip()

            # 转载文本可能在首个标题后继续放置署名；只扫描标题后的前几行，
            # 且不再使用正则寻找整篇正文，避免误删剧情中的“翻译”等词。
            lines = cleaned.splitlines(keepends=True)
            if lines and _EXPLICIT_CHAPTER_HEADING_RE.match(lines[0].lstrip()):
                credit_re = re.compile(
                    r"^\s*(?:网译版(?:\s+转自.*)?|转自\s*[:：]?|"
                    r"翻译\s*[:：]|校对\s*[:：]|录入\s*[:：]|来源\s*[:：])",
                    re.IGNORECASE,
                )
                retained = lines[:1]
                scan_limit = min(len(lines), 9)
                removed = 0
                for index in range(1, scan_limit):
                    line = lines[index]
                    if credit_re.match(line):
                        removed += 1
                        continue
                    retained.append(line)
                retained.extend(lines[scan_limit:])
                if removed:
                    print(f"   已清理首章标题后的转载/翻译署名 {removed} 行")
                cleaned = "".join(retained).lstrip()
            return cleaned
        print("   未能定位元信息边界，保持原文")
        return chapter_text
    except Exception as e:
        print(f"⚠️  front matter 清洗失败，跳过（原因: {e}）")
        return chapter_text


def _translate_novel_text(text: str, src_lang: str, tgt_lang: str) -> str:
    """
    使用 CTranslate2 + OPUS-MT 翻译小说文本。
    调用 services/translator/batch_translate.py 中的 translate_text_batch 函数。

    Args:
        text: 原文
        src_lang: 源语言代码（zh/en/ja等）
        tgt_lang: 目标语言代码（en/zh/ja等）

    Returns:
        翻译后的文本
    """
    try:
        from services.story_planner.batch_translate import translate_text_batch
        translated = translate_text_batch(text, src_lang=src_lang, tgt_lang=tgt_lang)
        if not isinstance(translated, str) or not translated.strip():
            raise RuntimeError("翻译结果为空")
        return translated
    except ImportError:
        print(f"⚠️  translator 模块未安装，跳过翻译（src={src_lang} -> tgt={tgt_lang}）")
        return text
    except Exception as e:
        print(f"⚠️  翻译失败，使用原文继续: {e}")
        return text


def _generate_global_style(novel_text: str, provider: str = None) -> str:
    """Legacy helper for historical offline chapter-branch tooling.

    Production task entry points no longer call this function: the fixed
    automatic visual route generates and publishes ``global_style`` in
    ``char_designer`` together with the style Anchors.
    """
    from services.story_planner.core.llm_client import LLMClient

    provider = provider or os.getenv("LLM_PROVIDER", "openai")
    sample_limit = int(os.getenv("GLOBAL_STYLE_SAMPLE_CHARS", "24000"))
    sample = novel_text[:sample_limit]
    system_prompt = (
        "You are the global art director for a visual-novel image pipeline. "
        "Infer one coherent visual style from the novel's genre, era, tone, and world. "
        "Return exactly one JSON object with the key global_style. "
        "The value must be a concise English prompt phrase ready to prepend to every character, "
        "background, event CG, and cover prompt. Describe medium, rendering technique or brushwork, "
        "color language, lighting, material texture, and overall aesthetic. Do not describe specific "
        "characters, locations, actions, composition, camera, aspect ratio, or image quality. "
        "Do not name living or dead artists. Do not add markdown or commentary."
    )
    client = LLMClient(provider=provider, temperature=0.2, max_tokens=300)
    result = client.chat_completion_json(
        system_prompt=system_prompt,
        user_prompt=f"Novel text:\n{sample}",
        timeout=90,
    )
    global_style = str(result.get("global_style") or "").strip()
    if not global_style:
        raise ValueError("LLM 未返回有效的 global_style")
    global_style = re.sub(r"\s+", " ", global_style).strip(" ,.;")
    if len(global_style) > 600:
        raise ValueError(f"LLM 返回的 global_style 过长: {len(global_style)} chars")
    return global_style


def _chapter_branch_source_fingerprint(novel_text: str) -> str:
    """Hash the canonical chapter_branch source before any generation work."""

    from services.story_planner.core.chapter_branch_runtime import content_sha256

    canonical = _strip_act_title_prefix(_strip_front_matter(str(novel_text or "")))
    return content_sha256(canonical)


def _resolve_chapter_branch_global_style(
    task_id: str,
    novel_text: str,
    *,
    provider: str = None,
) -> str:
    """Legacy chapter-branch checkpoint helper, outside the production path.

    New pipeline executions leave style generation to the visual stage.  This
    helper remains only so historical diagnostic callers can inspect/recover
    their old checkpoint format without changing the normal route.
    """

    from services.story_planner.core.chapter_branch_resume import (
        build_global_style_checkpoint,
        get_reusable_global_style,
    )
    from services.story_planner.core.chapter_branch_runtime import (
        initialize_chapter_branch_plan,
    )

    model = DEFAULT_MODEL
    source_fingerprint = _chapter_branch_source_fingerprint(novel_text)
    existing_plan = _load_chapter_branch_plan(task_id)
    style = get_reusable_global_style(
        existing_plan,
        source_fingerprint=source_fingerprint,
        model=model,
    )
    if style:
        print(f"♻️  复用 chapter_branch global_style 断点: {style}")
        return style
    style = _generate_global_style(novel_text, provider=provider)
    topology_result = (
        existing_plan.get("topology_result")
        if isinstance(existing_plan.get("topology_result"), dict)
        else {"topology": [], "variable_defs": []}
    )
    checkpoint_plan = initialize_chapter_branch_plan(
        task_id,
        topology_result,
        str(existing_plan.get("branch_type") or "pending"),
        existing_plan=existing_plan if existing_plan.get("version") else None,
        source_fingerprint=source_fingerprint,
    )
    previous_run_id = str(existing_plan.get("run_id") or "")
    from datetime import datetime as _dt, timezone as _timezone

    checkpoint_plan["run_id"] = uuid.uuid4().hex
    checkpoint_plan["run_started_at"] = _dt.now(_timezone.utc).isoformat()
    checkpoint_plan["heartbeat_at"] = checkpoint_plan["run_started_at"]
    checkpoint_plan["global_style_checkpoint"] = build_global_style_checkpoint(
        style,
        source_fingerprint=source_fingerprint,
        model=model,
    )
    _save_chapter_branch_plan(
        task_id,
        checkpoint_plan,
        expected_run_id=previous_run_id,
        allow_run_takeover=bool(previous_run_id != checkpoint_plan["run_id"]),
    )
    return style


@celery_app.task(
    name="services.story_planner.tasks.parse_novel_to_draft",
    soft_time_limit=RUNTIME_SETTINGS.task_soft_time_limit,
    time_limit=RUNTIME_SETTINGS.task_time_limit,
)
def parse_novel_to_draft(task_id: str):
    """
    Parse novel text into story tree structure.
    从 novel_tasks 表读取配置（story_mode / output_language / art_style），
    根据 story_mode 决定走原文适应还是多剧情改写，
    并在语言不匹配时自动翻译原文。

    Args:
        task_id: novel_tasks 表中的任务ID

    Returns:
        task_id (str)，传递给下游服务
    """
    if is_stage_already_running(task_id, 'story_planner'):
        wait_for_stage_completion(task_id, 'story_planner')
        completed_config = _get_task_config(task_id)
        if completed_config.get("story_mode") == StoryMode.CHAPTER_BRANCH:
            _validate_completed_chapter_branch_outputs(task_id)
        return task_id

    usage_run_id = None
    try:
        if _usage_tracker:
            usage_run_id = _usage_tracker.set_context(
                task_id, "story_planner", force_new_run=True
            )

        # 从 DB 读取任务配置
        config = _get_task_config(task_id)
        novel_filename = config["novel_filename"]
        output_language = config["output_language"]
        story_mode = config["story_mode"]

        # 小说文件路径（网关保存时以 task_id.txt 命名）
        novel_path = Path(RAW_NOVELS_PATH) / f"{task_id}.txt"
        if not novel_path.exists():
            fail_stage(task_id, 'story_planner', 10, f"Novel file not found: {novel_path}")
            # chain 编排下必须 raise 中断，不能 return dict：
            # 否则错误 dict 会作为参数传给下游 extract_info，掩盖真实失败原因。
            raise FileNotFoundError(f"Novel file not found: {novel_path}")

        print(f"📖 开始解析小说: {novel_filename}")
        print(f"🎮 任务ID: {task_id}")
        print(f"🌐 输出语言: {output_language}")
        print(f"🎭 剧情模式: {story_mode}")

        # ==================== 语言自适应翻译 ====================
        novel_text = _read_and_normalize_novel(novel_path)
        # Preserve the canonical source for chapter_branch's work-level
        # content-warning detector before the legacy translation step possibly
        # overwrites the working file.  This is intentionally kept separate
        # from ``novel_text`` because generation still consumes the selected
        # output language.
        content_warning_source_text = novel_text
        if story_mode == StoryMode.CHAPTER_BRANCH:
            # A previous delivery may have translated the working file in
            # place. Prefer the shared-PVC canonical source sidecar whenever
            # its working fingerprint matches this file.
            persisted_source = _load_content_warning_source(task_id, novel_text)
            if persisted_source:
                content_warning_source_text = persisted_source
        novel_lang = _detect_novel_language(novel_text)
        print(f"🔍 检测到小说语言: {novel_lang}")

        lang_prefix_map = {
            "zh-CN": "zh", "zh-TW": "zh",
            "en-US": "en", "en-GB": "en",
            "ja-JP": "ja", "ko-KR": "ko",
            "fr-FR": "fr", "de-DE": "de", "es-ES": "es",
        }
        tgt_lang = lang_prefix_map.get(output_language, output_language.split("-")[0])

        chapter_branch_translation_persisted = False
        if _need_translation(novel_lang, output_language):
            print(f"🔄 需要翻译: {novel_lang} -> {tgt_lang}，开始翻译原文...")
            translated_novel_text = _translate_novel_text(
                novel_text, src_lang=novel_lang, tgt_lang=tgt_lang
            )
            if story_mode == StoryMode.CHAPTER_BRANCH:
                try:
                    chapter_branch_translation_persisted = bool(
                        _persist_content_warning_source(
                            task_id,
                            source_text=content_warning_source_text,
                            working_text=translated_novel_text,
                            source_language=novel_lang,
                            target_language=tgt_lang,
                        )
                    )
                except Exception as sidecar_error:
                    print("sidecar persistence failed; continuing: " + str(sidecar_error))
            novel_text = translated_novel_text
            _atomic_write_utf8_text(novel_path, novel_text)
            print(f"✅ 翻译完成，已覆盖写入: {novel_path}")
        else:
            print(f"✅ 语言一致（{novel_lang}），无需翻译")

        if story_mode == StoryMode.CHAPTER_BRANCH:
            if not chapter_branch_translation_persisted:
                try:
                    _persist_content_warning_source(
                        task_id,
                        source_text=content_warning_source_text,
                        working_text=novel_text,
                        source_language=novel_lang,
                        target_language=tgt_lang,
                    )
                except Exception as sidecar_error:
                    print("sidecar persistence failed; continuing: " + str(sidecar_error))

            from services.story_planner.core.chapter_branch_runtime import (
                content_sha256,
            )

            checkpoint_source = None
            try:
                existing_plan = _load_chapter_branch_plan(task_id)
            except Exception as checkpoint_error:
                # Published-resume probing is opportunistic.  A checkpoint
                # read failure must not replace the primary generation error
                # (the authoritative failure path reports it later).
                print("   ⚠️  无法读取章节处理断点，继续常规发布恢复检查: " + str(checkpoint_error))
                existing_plan = {}
            processing_metadata = (
                existing_plan.get("preprocessing_checkpoint")
                if isinstance(existing_plan, dict)
                else None
            )
            processing_payload = _load_chapter_processing_checkpoint(
                task_id,
                processing_metadata,
            )
            if isinstance(processing_payload, dict):
                checkpoint_source_fingerprint = str(
                    processing_payload.get("cleaned_source_fingerprint") or ""
                )
                if checkpoint_source_fingerprint:
                    checkpoint_source = checkpoint_source_fingerprint
            if not checkpoint_source:
                checkpoint_source = content_sha256(
                    _strip_act_title_prefix(_strip_front_matter(novel_text))
                )
            if _try_resume_published_chapter_branch(
                task_id,
                source_fingerprint=checkpoint_source,
            ):
                return task_id

        # 全自动视觉路线固定由 char_designer 在视觉阶段生成/复用
        # global_style；story_planner 不再走旧的纯文本画风分支。
        try:
            from common.global_style import get_global_style

            global_style = get_global_style(task_id)
            print("🎨 复用已有 global_style；视觉阶段仍负责 Anchor 合同")
        except Exception:
            global_style = None
            print("🎨 尚无已发布 global_style；交由视觉阶段生成并发布 Anchor")

        # ==================== 根据 story_mode 分流 ====================
        if story_mode == StoryMode.ORIGINAL:
            return _parse_single_mode(novel_path, novel_filename, task_id, global_style)
        elif story_mode == StoryMode.CHAPTER_BRANCH:
            return _parse_chapter_branch_mode(
                novel_path,
                novel_filename,
                task_id,
                global_style,
                content_warning_source_text=content_warning_source_text,
            )
        else:
            return _parse_multi_mode(novel_path, novel_filename, task_id, global_style)

    except StageLeaseLostError:
        raise
    except Exception as e:
        if locals().get("story_mode") == StoryMode.CHAPTER_BRANCH:
            _stop_runtime_heartbeat(task_id, final_status="failed")
            try:
                failed_plan = _load_chapter_branch_plan(task_id)
                _structured_runtime_log(
                    _runtime_logger,
                    "chapter_branch_run_failed",
                    level="error",
                    task_id=task_id,
                    run_id=failed_plan.get("run_id", ""),
                    stage="story_planner",
                    branch_type=failed_plan.get("branch_type", "unknown"),
                    error_category=classify_runtime_error(e),
                    error_type=type(e).__name__,
                )
                from services.story_planner.core.chapter_branch_runtime import (
                    mark_running_nodes_failed,
                )

                if mark_running_nodes_failed(failed_plan, str(e)):
                    _save_chapter_branch_plan(task_id, failed_plan)
            except Exception as checkpoint_error:
                print(
                    "⚠️  chapter_branch 失败状态读取/落盘失败，保留原始异常："
                    f"{checkpoint_error}"
                )
        fail_stage(task_id, 'story_planner', 10, str(e))
        import traceback
        error_msg = f"Error in pipeline: {str(e)}\n{traceback.format_exc()}"
        print(f"❌ {error_msg}")
        # chain 编排下必须 raise 中断，不能 return dict（见上方说明）。
        raise
    finally:
        _stop_runtime_heartbeat(task_id, final_status="stopped")
        if _usage_tracker and usage_run_id:
            _usage_tracker.flush(run_id=usage_run_id)
            _usage_tracker.clear_context(usage_run_id)



def _trigger_downstream(task_id: str, game_project: str):
    """标记 story_planner 阶段完成。

    下游编排（info_extractor -> [char->scene | audio | sound] -> game_builder）
    由 api_gateway 的 Celery chain 统一负责，此处不再自行触发任何下游任务，
    否则会与 chain 形成两套并行编排互相抢跑（典型表现：audio 在 info_extractor
    之前就启动，因 plots 未就绪而失败）。
    """
    complete_stage(game_project, 'story_planner', 20)


def _validate_completed_chapter_branch_outputs(task_id: str) -> None:
    """Verify the authoritative story tree before accepting a completed redelivery."""
    with Session(platform_engine) as session:
        story_tree = session.exec(
            select(StoryTreeJson).where(StoryTreeJson.task_id == task_id)
        ).first()
        md_rows = list(
            session.exec(
                select(StoryTreeMd).where(StoryTreeMd.task_id == task_id)
            ).all()
        )
    with Session(engine) as session:
        plan_row = session.exec(
            select(ChapterBranchPlan).where(ChapterBranchPlan.task_id == task_id)
        ).first()
        summary_row = session.exec(
            select(StorySummary).where(StorySummary.task_id == task_id)
        ).first()
        worldview_row = session.exec(
            select(Worldview).where(Worldview.task_id == task_id)
        ).first()

    from services.story_planner.core.chapter_branch_artifacts import (
        validate_completed_chapter_branch_artifacts,
    )

    validate_completed_chapter_branch_artifacts(
        task_id=task_id,
        story_tree_data=getattr(story_tree, "data", None),
        md_artifacts=md_rows,
        plan=getattr(plan_row, "topology_json", None),
        summary=summary_row,
        worldview=worldview_row,
    )


def _try_resume_published_chapter_branch(
    task_id: str,
    *,
    source_fingerprint: str,
) -> bool:
    """Finish a published chapter branch without re-running generation.

    This covers the crash window between the transactional Story Tree publish
    and the required ``story_summary``/stage completion writes.
    """

    with Session(platform_engine) as session:
        story_tree = session.exec(
            select(StoryTreeJson).where(StoryTreeJson.task_id == task_id)
        ).first()
        if story_tree is None:
            return False
        md_rows = list(
            session.exec(
                select(StoryTreeMd).where(StoryTreeMd.task_id == task_id)
            ).all()
        )
    with Session(engine) as session:
        plan_row = session.exec(
            select(ChapterBranchPlan).where(ChapterBranchPlan.task_id == task_id)
        ).first()
        summary_row = session.exec(
            select(StorySummary).where(StorySummary.task_id == task_id)
        ).first()
        worldview_row = session.exec(
            select(Worldview).where(Worldview.task_id == task_id)
        ).first()

    from services.story_planner.core.chapter_branch_artifacts import (
        ChapterBranchArtifactValidationError,
        validate_completed_chapter_branch_artifacts,
    )

    plan = getattr(plan_row, "topology_json", None)
    if (
        not isinstance(plan, dict)
        or not str(plan.get("source_fingerprint") or "")
        or str(plan.get("source_fingerprint")) != str(source_fingerprint)
    ):
        print("♻️  已发布 Story Tree 的原文指纹与当前输入不一致，按断点重建")
        return False
    from services.story_planner.core.soft_branch import (
        plan_has_current_soft_branch_generation_identity,
    )

    if not plan_has_current_soft_branch_generation_identity(plan):
        print("♻️  已发布 Story Tree 的软分支生成规则已升级，按断点重建")
        return False
    try:
        validate_completed_chapter_branch_artifacts(
            task_id=task_id,
            story_tree_data=getattr(story_tree, "data", None),
            md_artifacts=md_rows,
            plan=plan,
            summary=summary_row,
            worldview=worldview_row,
            require_summary=False,
        )
    except ChapterBranchArtifactValidationError as exc:
        print(f"⚠️  已发布 chapter_branch 核心产物不可复用，将按断点重建：{exc}")
        return False

    if int(plan.get("version", 0) or 0) == 5:
        from services.story_planner.core.chapter_branch_resume import (
            upgrade_legacy_completed_plan,
        )

        plan = upgrade_legacy_completed_plan(
            plan,
            publication_fingerprint=str(
                (getattr(story_tree, "data", {}) or {})
                .get("publication", {})
                .get("fingerprint", "")
            ),
        )

    from datetime import datetime as _dt, timezone as _timezone

    previous_run_id = str(plan.get("run_id") or "")
    plan["run_id"] = uuid.uuid4().hex
    plan["run_started_at"] = _dt.now(_timezone.utc).isoformat()
    plan["heartbeat_at"] = plan["run_started_at"]
    _save_chapter_branch_plan(
        task_id,
        plan,
        expected_run_id=previous_run_id,
        allow_run_takeover=True,
    )

    summary_data = getattr(summary_row, "data", None)
    summary_ready = bool(
        getattr(summary_row, "status", None) == 1
        and isinstance(summary_data, dict)
        and all(
            str(summary_data.get(field) or "").strip()
            for field in ("outline", "background", "one_liner")
        )
    )
    if not summary_ready:
        from services.story_planner.core.chapter_branch_resume import (
            build_summary_from_checkpoint,
        )

        worldview_data = getattr(worldview_row, "data", {})
        rebuilt_summary = build_summary_from_checkpoint(
            plan,
            worldview_data if isinstance(worldview_data, dict) else {},
        )
        _save_required_chapter_branch_summary(
            task_id=task_id,
            **rebuilt_summary,
        )
        with Session(engine) as session:
            summary_row = session.exec(
                select(StorySummary).where(StorySummary.task_id == task_id)
            ).first()
        print("♻️  Story Tree 已完整发布，本次仅补齐 story_summary")
    else:
        print("♻️  Story Tree 与 story_summary 均已完整，跳过全部生成步骤")

    if isinstance(plan, dict):
        from services.story_planner.core.chapter_branch_runtime import (
            content_sha256,
            update_plan_step,
        )

        update_plan_step(
            plan,
            "story_publish",
            status="completed",
            output_digest=str(
                (getattr(story_tree, "data", {}) or {})
                .get("publication", {})
                .get("fingerprint", "")
            ),
        )
        update_plan_step(
            plan,
            "summary_publish",
            status="completed",
            output_digest=content_sha256(
                json.dumps(
                    getattr(summary_row, "data", {}) or {},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ),
        )
        update_plan_step(
            plan,
            "stage_complete",
            status="completed",
            output_digest="completion-request-validated",
        )
        _save_chapter_branch_plan(task_id, plan)
    validate_completed_chapter_branch_artifacts(
        task_id=task_id,
        story_tree_data=getattr(story_tree, "data", None),
        md_artifacts=md_rows,
        plan=plan,
        summary=summary_row,
        worldview=worldview_row,
    )
    _trigger_downstream(task_id, task_id)
    return True


def _publish_chapter_branch_story_tree(**publish_kwargs):
    """Fence the irreversible chapter_branch story release publication."""
    task_id = str(publish_kwargs.get("task_id") or "")
    ensure_stage_lease_owned(task_id, "story_planner")
    return publish_story_tree_transactionally(**publish_kwargs)


def _save_owned_chapter_branch_md(**save_kwargs):
    """Fence each chapter_branch Markdown row write against stale owners."""
    task_id = str(save_kwargs.get("task_id") or "")
    ensure_stage_lease_owned(task_id, "story_planner")
    return save_story_md_to_db(**save_kwargs)


def _save_chapter_branch_diagnostics(
    task_id: str,
    candidate_story_tree: dict,
    quality_report: dict,
) -> bool:
    """Persist candidate tree and quality report even when publication is rejected."""
    root = Path(
        os.getenv(
            "STORY_PLANNER_DIAGNOSTIC_DIR",
            "/storage/story_planner/diagnostics",
        )
    )
    safe_task_id = re.sub(r"[^0-9A-Za-z._-]+", "_", str(task_id)).strip("._") or "unknown"
    output_dir = root / safe_task_id
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, payload in (
        ("candidate_story_tree.json", candidate_story_tree),
        ("quality_report.json", quality_report),
    ):
        target = output_dir / filename
        atomic_write_bytes(
            target,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            validator=_validate_json_artifact,
        )
    print(f"🧪 chapter_branch 诊断产物已保存: {output_dir}")
    return True


def _parse_multi_mode(
    novel_path,
    novel_name: str,
    game_project: str,
    global_style: str = None,
):
    """多剧情模式：世界观分析 -> 大纲 -> LLM改写生成多分支故事树"""
    source_text = _read_and_normalize_novel(novel_path)
    # ==================== 第1步：世界观分析 ====================
    analyst = WorldAnalyst(provider=STORY_PLANNER_GATEWAY_PROVIDER)
    world_bible = analyst.analyze_novel_file(
        filepath=str(novel_path),
        project_name=game_project,
        output_dir="world_db"
    )

    task_id = world_bible.task_id

    # 进度更新：世界观分析完成
    update_task_progress(game_project, progress=15)

    print(f"✅ 世界观分析完成:")
    print(f"   task_id: {task_id}")
    print(f"   项目名称: {world_bible.project_name}")
    print(f"   角色数量: {len(world_bible.characters)}")
    print(f"   场景数量: {len(world_bible.scenes)}")

    visual_style_context = build_visual_style_context(
        source_text=source_text,
        story_mode=StoryMode.MULTI,
        worldview=world_bible.worldview,
        narrative_structure=world_bible.narrative_structure,
    )
    print(
        "🎨 visual_style_context 已整理: "
        f"coverage={visual_style_context.get('coverage_strategy')} "
        f"source_chars={visual_style_context.get('source_char_count')} "
        f"sampled_chars={visual_style_context.get('sampled_char_count')}"
    )

    # 从 narrative_structure 提取摘要，写入 story_summary 表（供前端展示）
    ns = world_bible.narrative_structure or {}
    wv = world_bible.worldview or {}
    _save_summary(
        task_id=task_id,
        outline="; ".join(ns.get("major_conflicts", [])) or ns.get("inciting_incident", ""),
        background=wv.get("history", "") or wv.get("geography", {}).get("regions", [""])[0],
        one_liner=ns.get("inciting_incident", ""),
    )

    # ==================== 第2步：等待世界观确认 ====================
    print(f"\n🔍 等待世界观状态变为 CONFIRMED...")
    if not wait_for_worldview_confirmation(
        task_id,
        poll_interval=10,
        timeout=RUNTIME_SETTINGS.confirmation_timeout,
    ):
        fail_stage(game_project, 'story_planner', 15, "Worldview confirmation timeout or failed")
        raise RuntimeError("Worldview confirmation timeout or failed")

    update_task_progress(game_project, progress=18)

    # ==================== 第3步：生成大纲 ====================
    print(f"\n📝 开始生成大纲...")
    try:
        from services.story_planner.table.outline_fetcher import fetch_outline
        depth = 8
        outline_task_id, outline_chapters = fetch_outline(
            worldview_task_id=task_id,
            world_bible=world_bible,
            max_depth=depth
        )
        print(f"✅ 大纲生成成功! 章节数: {len(outline_chapters)}")
    except Exception as e:
        print(f"❌ 大纲生成失败: {e}")
        import traceback
        traceback.print_exc()
        fail_stage(game_project, 'story_planner', 15, f"Outline generation failed: {str(e)}")
        raise

    # ==================== 第4步：等待大纲确认 ====================
    print(f"\n🔍 等待大纲状态变为 CONFIRMED...")
    if not wait_for_outline_confirmation(
        task_id,
        poll_interval=10,
        timeout=RUNTIME_SETTINGS.confirmation_timeout,
    ):
        fail_stage(game_project, 'story_planner', 18, "Outline confirmation timeout or failed")
        raise RuntimeError("Outline confirmation timeout or failed")

    # ==================== 第5步：生成故事树 ====================
    print(f"\n📖 开始生成故事树...")
    try:
        tree, session_dir = generate_story_tree(
            world_bible=world_bible,
            width=3,
            depth=depth,
            provider=STORY_PLANNER_GATEWAY_PROVIDER,
            task_id=task_id,
            global_style=global_style,
            visual_style_context=visual_style_context,
        )
        print(f"✅ 故事树生成成功! 存档目录: {session_dir}")
    except Exception as e:
        print(f"❌ 故事树生成失败: {e}")
        import traceback
        traceback.print_exc()
        fail_stage(game_project, 'story_planner', 18, f"Story tree generation failed: {str(e)}")
        raise

    # ==================== 第6步：触发后续服务 ====================
    print(f"\n🚀 触发后续服务...")
    _trigger_downstream(task_id, game_project)

    print(f"\n🎉 多剧情全流程完成!")
    if _usage_tracker:
        _usage_tracker.flush()
    return task_id


def _parse_single_mode(
    novel_path,
    novel_name: str,
    game_project: str,
    global_style: str = None,
):
    """
    单剧情模式：原文直接拆分成章节，不经过LLM改写。
    生成与多剧情相同格式的 md + story_tree.json，但只有一条线。
    章节大小：2w-4w字自适应拆分。
    """
    from datetime import datetime as _dt
    from services.story_planner.core.story_generator import StoryTree, StoryNode, StoryChoice
    from services.story_planner.core.ui_mode import infer_ui_mode
    from services.story_planner.table.story_tree_to_db import (
        save_story_md_to_db,
        save_story_json_to_db,
    )

    print(f"📖 单剧情模式：读取原文并拆分章节...")

    # 读取原文
    text = _read_and_normalize_novel(novel_path)
    total_chars = len(text)
    print(f"   原文总字数: {total_chars}")

    # ==================== 自适应拆分 ====================
    # 目标：每章 8k-13k 字，优先 1w；先算章节数，再均分
    MIN_CHAPTER_SIZE = 8000
    MAX_CHAPTER_SIZE = 13000
    TARGET_SIZE = 10000  # 优先目标

    if total_chars <= MAX_CHAPTER_SIZE:
        chapter_count = 1
    else:
        chapter_count = round(total_chars / TARGET_SIZE)
        chapter_count = max(chapter_count, total_chars // MAX_CHAPTER_SIZE)
        chapter_count = min(chapter_count, total_chars // MIN_CHAPTER_SIZE + 1)
        chapter_count = max(chapter_count, 1)

    chunk_size = total_chars // chapter_count
    print(f"   拆分章节数: {chapter_count}，每章约 {chunk_size} 字")

    chapters = []
    start = 0
    for i in range(chapter_count):
        if i == chapter_count - 1:
            chapters.append(text[start:])
        else:
            end = start + chunk_size
            # 向后找最近的换行符，避免截断句子（最多多找300字）
            newline_pos = text.find('\n', end, end + 300)
            if newline_pos != -1:
                end = newline_pos + 1
            chapters.append(text[start:end])
            start = end

    _preflight_report = run_novel_preflight(
        text=text,
        chapters=chapters,
        story_mode=StoryMode.ORIGINAL,
    )
    print(_preflight_report.format_text())

    # ==================== 第一章正文头部元信息清洗 ====================
    config = _get_task_config(game_project)
    output_language = config.get("output_language")
    print(f"\n🧹 清洗第一章正文头部元信息...")
    chapters[0] = _strip_front_matter(chapters[0])

    book_metadata = _build_story_tree_book_metadata(
        config,
        novel_filename=config.get("novel_filename") or novel_name,
        task_id=game_project,
        chapter_summaries=[
            _extract_source_chapter_summary(chapter, i + 1)
            for i, chapter in enumerate(chapters[:3])
        ],
    )

    visual_style_context = build_visual_style_context(
        source_text="\n\n".join(chapters),
        story_mode=StoryMode.ORIGINAL,
        chapters=chapters,
    )
    print(
        "🎨 visual_style_context 已整理: "
        f"coverage={visual_style_context.get('coverage_strategy')} "
        f"source_chars={visual_style_context.get('source_char_count')} "
        f"sampled_chars={visual_style_context.get('sampled_char_count')}"
    )

    # ==================== 构建故事树（单线） ====================
    task_id = game_project  # 单剧情直接用 game_project 作为 task_id

    tree = StoryTree(root_id="")
    prev_node_id = None

    for idx, chapter_text in enumerate(chapters):
        depth = idx + 1
        # 使用与多剧情一致的命名规则
        if idx == 0:
            node_id = "chapter_1"
        else:
            node_id = f"node_{uuid.uuid4().hex[:8]}"
        title = _extract_source_chapter_title(chapter_text, depth)
        parent_id = prev_node_id

        # md 内容：直接用原文片段，加章节标题头
        md_content = f"# {title}\n\n{chapter_text.strip()}\n"

        node = StoryNode(
            id=node_id,
            depth=depth,
            title=title,
            parent_id=parent_id,
            content_file="",
            children=[],
            summary=_extract_source_chapter_summary(chapter_text, depth),
            generated_at=_dt.now().isoformat()
        )

        # 如果有上一节点，把当前节点作为上一节点的唯一子节点
        if prev_node_id is not None:
            prev_node = tree.get_node(prev_node_id)
            prev_node.children = [StoryChoice(choice_text="继续", child_id=node_id)]

        tree.add_node(node)

        if idx == 0:
            tree.root_id = node_id

        # 保存 md 到数据库
        save_story_md_to_db(task_id=task_id, name=node_id, content=md_content, overwrite=True)

        prev_node_id = node_id

    print(f"✅ 故事树构建完成，共 {len(tree.nodes)} 个节点")

    # ==================== 保存 story_tree.json 到数据库 ====================
    json_data = {
        "root_id": tree.root_id,
        **book_metadata,
        # 保留给 game_builder/UI 的主题兼容；生图链路不得读取此字段。
        "art_style": config.get("art_style", "ink_chinese"),
        "book_language": output_language,
        "UI_mode": infer_ui_mode(text=text),
        "visual_style_context": visual_style_context,
        "nodes": {nid: node.to_dict() for nid, node in tree.nodes.items()}
    }
    if global_style:
        json_data["global_style"] = global_style
    json_data = wrap_story_tree_language_bundles(
        json_data,
        build_story_tree_language_bundle(
            json_data,
            selected_language=output_language,
            source_text=text,
            provider=STORY_PLANNER_GATEWAY_PROVIDER,
        ),
        default_language=get_story_tree_player_language(),
    )
    save_story_json_to_db(task_id=task_id, json_data=json_data, overwrite=True)
    print(f"✅ story_tree.json 已保存到数据库")

    # 从原文第一章提取摘要（异步 LLM 调用，失败不影响主流程）
    if chapters:
        _generate_summary_from_text(task_id=task_id, text_sample=chapters[0])

    # ==================== 触发后续服务 ====================
    print(f"\n🚀 触发后续服务...")
    _trigger_downstream(task_id, game_project)

    print(f"\n🎉 单剧情全流程完成!")
    if _usage_tracker:
        _usage_tracker.flush()
    return task_id


# ────────────────────────────────────────────────────────────────────────────
# 软分支 MD 标记（供下游 script_director 解析）
# 软分支 MD 标记（供下游 char_designer/converter 解析）
# ────────────────────────────────────────────────────────────────────────────
SOFT_BRANCH_OPEN = (
    "\n\n"
    "════════════════════════════════════════\n"
    "◆◆◆  软分支开始  ◆◆◆\n"
    "════════════════════════════════════════\n"
)
SOFT_BRANCH_CHOICE_A = (
    "────────────────────────────────────────\n"
    "▶ 选项A：{choice_a}\n"
    "────────────────────────────────────────\n"
)
SOFT_BRANCH_CHOICE_B = (
    "────────────────────────────────────────\n"
    "▶ 选项B：{choice_b}\n"
    "────────────────────────────────────────\n"
)
SOFT_BRANCH_CLOSE = (
    "════════════════════════════════════════\n"
    "◆◆◆  软分支结束  ◆◆◆\n"
    "════════════════════════════════════════\n\n"
)


def _strip_act_title_prefix(text: str) -> str:
    """Strip embedded act-title prefixes from section text.

    Original novel format:
        ## 第二幕 说话
        （空行）
        第二幕说话1632年正文...   ← 标题词拼接年份嵌在正文开头

    After fix:
        ## 第二幕 说话
        （空行）
        正文...
    """
    import re
    lines = text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('## '):
            result.append(line)
            title_words = re.sub(r'\s+', '', line[3:])  # 去掉 ## 和所有空格
            i += 1
            while i < len(lines) and not lines[i].strip():
                result.append(lines[i])
                i += 1
            if i < len(lines):
                prefix_pat = re.escape(title_words) + r'\d{4}'
                result.append(re.sub('^' + prefix_pat + r'年?\s*', '', lines[i]))
                i += 1
        else:
            result.append(line)
            i += 1
    return '\n'.join(result)


_EXPLICIT_CHAPTER_HEADING_RE = re.compile(
    r"(?m)^[^\S\r\n]*(?:"
    r"第[0-9零〇一二三四五六七八九十百千万两]+[章节回卷]"
    r"(?:\s*$|[ \t:：、.．-]+[^\n]{0,60}$)"
    r"|[NＮ][OＯ]\s*[.．。]?\s*[0-9０-９]+"
    r"(?:\s*$|[ \t:：、.．。\-－]+[^\n]{0,60}$)"
    r"|Chapter\s+\d+\b[^\n]{0,60}$"
    r")",
    re.IGNORECASE,
)

_CHINESE_NUMERAL_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "两": 2,
}
_CHINESE_NUMERAL_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _parse_chapter_number(heading: str) -> tuple[int | None, str]:
    """Return (normalized number, heading family) for one title line."""
    import unicodedata

    normalized = unicodedata.normalize("NFKC", str(heading or "")).strip()
    no_match = re.match(r"^NO\s*[.]?\s*(\d+)", normalized, re.IGNORECASE)
    if no_match:
        return int(no_match.group(1)), "no"
    chapter_match = re.match(r"^Chapter\s+(\d+)", normalized, re.IGNORECASE)
    if chapter_match:
        return int(chapter_match.group(1)), "chapter"
    chinese_match = re.match(
        r"^第([0-9零〇一二三四五六七八九十百千万两]+)([章节回卷])",
        normalized,
    )
    if not chinese_match:
        return None, "unknown"
    token, marker = chinese_match.groups()
    if token.isdigit():
        return int(token), marker
    total = 0
    section = 0
    number = 0
    for char in token:
        if char in _CHINESE_NUMERAL_DIGITS:
            number = number * 10 + _CHINESE_NUMERAL_DIGITS[char]
            continue
        unit = _CHINESE_NUMERAL_UNITS.get(char)
        if unit is None:
            return None, marker
        if unit == 10000:
            section = (section + number) * unit
            total += section
            section = 0
            number = 0
        else:
            section += (number or 1) * unit
            number = 0
    return total + section + number, marker


def _validated_explicit_heading_matches(text: str) -> list[re.Match]:
    """Use explicit boundaries only when their normalized sequence is complete."""
    matches = list(_EXPLICIT_CHAPTER_HEADING_RE.finditer(text))
    if len(matches) < 2:
        return []
    parsed = []
    for match in matches:
        number, family = _parse_chapter_number(match.group(0))
        if number is None:
            return []
        parsed.append((match, number, family))

    # A volume title is a fallback boundary only when no finer chapter/return
    # headings are present. Otherwise it must not turn an entire volume into a
    # single chapter.
    fine = [item for item in parsed if item[2] != "卷"]
    if fine:
        parsed = fine

    numbers = [item[1] for item in parsed]
    if not numbers or numbers[0] != 1:
        return []
    if any(current != previous + 1 for previous, current in zip(numbers, numbers[1:])):
        return []
    return [item[0] for item in parsed]

_SUPPLEMENTAL_SECTION_TITLE_RE = re.compile(
    r"^(?:作者)?(?:后记|插图(?:页)?|作者感言|创作谈|版权(?:页|信息)?|附录|"
    r"特别短篇|短篇|番外(?:篇)?|外传)(?:[\s:：、.．-].*)?$",
    re.IGNORECASE,
)


def _is_supplemental_chapter_heading(heading: str) -> bool:
    """Return True for back matter/extras that must not enter main topology."""
    normalized = re.sub(r"\s+", " ", str(heading or "").strip())
    normalized = re.sub(
        r"^第[0-9零〇一二三四五六七八九十百千万两]+[章节回卷部篇]\s*",
        "",
        normalized,
    ).strip()
    return bool(_SUPPLEMENTAL_SECTION_TITLE_RE.fullmatch(normalized))


def _should_rebalance_chapters(chapters: list) -> bool:
    """Only rebalance synthetic size-based chunks, never explicit original chapters."""
    if len(chapters) < 2:
        return False
    # A recognized stream may contain continuation chunks without a repeated
    # heading when an oversized source chapter was split. Revalidate the
    # concatenated stream so a non-contiguous heading fallback is not mistaken
    # for an explicit-source stream.
    if _EXPLICIT_CHAPTER_HEADING_RE.match((chapters[0] or "").lstrip()) and _validated_explicit_heading_matches(
        "".join(str(chapter or "") for chapter in chapters)
    ):
        return False
    return not all(
        _EXPLICIT_CHAPTER_HEADING_RE.match((chapter or "").lstrip())
        for chapter in chapters[-2:]
    )


_SECTION_HEADING_RE = re.compile(
    r"(?m)^[^\S\r\n]*(?:(?:第)?[0-9一二三四五六七八九十百千零〇两]+[章节回卷部篇]|"
    r"[NＮ][OＯ]\s*[.．。]?\s*[0-9０-９]+|"
    r"[一二三四五六七八九十百千零〇两]+)[ \t\u3000、.．。:：\-－]+[^\n]{1,30}\n"
)


def _find_safe_chapter_boundary(
    text: str,
    target: int,
    min_pos: int,
    max_pos: int,
) -> int:
    """Find a nearby narrative boundary without splitting a sentence or title.

    Returned positions are raw string offsets, so joining all chunks always
    reconstructs the source text byte-for-byte after decoding.
    """
    min_pos = max(1, min(min_pos, len(text) - 1))
    max_pos = max(min_pos, min(max_pos, len(text) - 1))
    target = max(min_pos, min(target, max_pos))
    window = text[min_pos:max_pos]

    section_positions = [
        min_pos + match.start()
        for match in _SECTION_HEADING_RE.finditer(window)
    ]
    if section_positions:
        return min(section_positions, key=lambda pos: abs(pos - target))

    paragraph_positions = []
    for match in re.finditer(r"\n[ \t]*\n+", window):
        boundary = min_pos + match.end()
        before = text[: match.start() + min_pos].rstrip()
        if before.endswith(("。", "！", "？", "!", "?", "…", "”", "’", "」", "』")):
            paragraph_positions.append(boundary)
    if paragraph_positions:
        return min(paragraph_positions, key=lambda pos: abs(pos - target))

    sentence_positions = [
        min_pos + match.end()
        for match in re.finditer(r"[。！？!?…](?:[”’」』】）》）])?", window)
    ]
    if sentence_positions:
        return min(sentence_positions, key=lambda pos: abs(pos - target))

    newline_positions = [
        min_pos + match.end()
        for match in re.finditer(r"\n", window)
    ]
    if newline_positions:
        boundary = min(newline_positions, key=lambda pos: abs(pos - target))
        preceding_line_start = text.rfind("\n", min_pos, boundary - 1) + 1
        preceding_line = text[preceding_line_start : boundary - 1] + "\n"
        if _SECTION_HEADING_RE.fullmatch(preceding_line):
            return preceding_line_start
        return boundary

    return target


_GAME_CHAPTER_MIN_CHARS = 10_000
_GAME_CHAPTER_MAX_CHARS = 15_000
_GAME_CHAPTER_TARGET_CHARS = 12_000
_GAME_CHAPTER_BOUNDARY_TOLERANCE = 100


def _choose_size_based_chapter_count(
    total_chars: int,
    *,
    min_chars: int = _GAME_CHAPTER_MIN_CHARS,
    max_chars: int = _GAME_CHAPTER_MAX_CHARS,
    target_chars: int = _GAME_CHAPTER_TARGET_CHARS,
) -> int:
    """Choose a chapter count while handling ranges with no exact solution.

    For most novel sizes there is a count whose average falls in the required
    range.  Small inputs can fall between ranges (for example 18,000 chars
    cannot form only 10,000--15,000-char chapters).  In that case choose the
    count with the smallest range violation, preferring the target average and
    then fewer chapters so we do not create needless tiny nodes.
    """
    total_chars = max(0, int(total_chars or 0))
    if total_chars <= max_chars:
        return 1

    preferred = max(1, round(total_chars / max(target_chars, 1)))
    minimum_count = max(1, (total_chars + max_chars - 1) // max_chars)
    maximum_count = max(1, total_chars // max(min_chars, 1))
    if minimum_count <= maximum_count:
        return max(minimum_count, min(preferred, maximum_count))

    candidates = {preferred, minimum_count, maximum_count}
    if total_chars > max_chars:
        candidates.discard(1)
    scored = []
    for count in candidates:
        if count <= 0:
            continue
        average = total_chars / count
        range_violation = max(0, min_chars - average) + max(0, average - max_chars)
        scored.append(
            (
                max(0, average - max_chars),
                max(0, min_chars - average),
                range_violation,
                abs(average - target_chars),
                count,
            )
        )
    return min(scored)[4] if scored else 1


def _split_text_by_game_chapter_size(
    text: str,
    *,
    min_chars: int = _GAME_CHAPTER_MIN_CHARS,
    max_chars: int = _GAME_CHAPTER_MAX_CHARS,
    target_chars: int = _GAME_CHAPTER_TARGET_CHARS,
    force_maximum: bool = False,
) -> list[str]:
    """Split text into game-sized chapters without changing source text."""
    source = str(text or "")
    if not source:
        return []
    total_chars = len(source)
    chapter_count = (
        max(1, (total_chars + max_chars - 1) // max_chars)
        if force_maximum and total_chars > max_chars
        else _choose_size_based_chapter_count(
            total_chars,
            min_chars=min_chars,
            max_chars=max_chars,
            target_chars=target_chars,
        )
    )
    if chapter_count <= 1:
        return [source]

    globally_meets_min = total_chars >= chapter_count * min_chars
    globally_meets_max = total_chars <= chapter_count * max_chars
    chapters: list[str] = []
    start = 0
    for index in range(chapter_count):
        remaining = chapter_count - index - 1
        if remaining == 0:
            chapters.append(source[start:])
            break

        lower = start + (min_chars if globally_meets_min else 1)
        upper = start + (max_chars if globally_meets_max else max_chars)
        # Keep enough text for the remaining chapters to stay under the hard
        # maximum.  The lower-bound counterpart is only enforceable when the
        # total input can actually provide the requested minimum to every
        # chapter; short inputs are allowed to have a short final chapter.
        lower = max(lower, total_chars - remaining * max_chars)
        if globally_meets_min:
            upper = min(upper, total_chars - remaining * min_chars)
        lower = min(max(lower, start + 1), total_chars - remaining)
        upper = max(lower, min(upper, total_chars - remaining))
        target = start + round((total_chars - start) / (remaining + 1))
        heading_window_start = max(start + 1, lower - _GAME_CHAPTER_BOUNDARY_TOLERANCE)
        nearby_headings = list(
            _SECTION_HEADING_RE.finditer(
                source[
                    heading_window_start : min(
                        len(source),
                        lower + _GAME_CHAPTER_BOUNDARY_TOLERANCE + 100,
                    )
                ]
            )
        )
        nearby_headings = [
            match
            for match in nearby_headings
            if heading_window_start + match.start() < lower
        ]
        if nearby_headings:
            end = heading_window_start + nearby_headings[-1].start()
        else:
            end = _find_safe_chapter_boundary(
                source,
                target=target,
                min_pos=lower,
                max_pos=upper,
            )
        if end <= start:
            end = min(start + max(1, round(total_chars / chapter_count)), total_chars - remaining)
        chapters.append(source[start:end])
        start = end
    return chapters


def _merge_short_explicit_chapters(chapters: list[str]) -> list[str]:
    """Pack recognized source chapters into 10k--15k game chapters.

    Original chapter boundaries are preferred when a valid packing exists.
    If an individual source chapter is too long or its boundaries make the
    target range impossible, fall back to a lossless narrative boundary split
    over the combined text.
    """
    source_chapters = [str(chapter or "") for chapter in chapters if str(chapter or "")]
    if len(source_chapters) <= 1:
        return source_chapters

    expanded: list[str] = []
    for chapter in source_chapters:
        if len(chapter) > _GAME_CHAPTER_MAX_CHARS:
            expanded.extend(
                _split_text_by_game_chapter_size(
                    chapter,
                    force_maximum=True,
                )
            )
        else:
            expanded.append(chapter)
    source_chapters = expanded
    total_chars = sum(len(chapter) for chapter in source_chapters)
    # There is no way to satisfy the 10k lower bound for a shorter source, but
    # one short game chapter is still preferable to several tiny menu nodes.
    if total_chars < _GAME_CHAPTER_MIN_CHARS:
        return ["".join(source_chapters)]

    # Do not rewrite healthy explicit chapters.
    if all(
        _GAME_CHAPTER_MIN_CHARS <= len(chapter) <= _GAME_CHAPTER_MAX_CHARS
        for chapter in source_chapters
    ):
        return source_chapters

    chapter_count = _choose_size_based_chapter_count(total_chars)
    if chapter_count <= 1:
        return ["".join(source_chapters)]

    # Find a contiguous partition that keeps as many original boundaries as
    # possible while meeting the game range.  The number of source chapters is
    # small enough for memoized boundary search, and this avoids a greedy
    # merge producing an unusable short tail.
    lengths = [len(chapter) for chapter in source_chapters]
    prefix = [0]
    for length in lengths:
        prefix.append(prefix[-1] + length)
    from functools import lru_cache

    @lru_cache(maxsize=None)
    def _partition(start_index: int, groups_left: int):
        if groups_left == 1:
            segment_chars = prefix[-1] - prefix[start_index]
            if _GAME_CHAPTER_MIN_CHARS <= segment_chars <= _GAME_CHAPTER_MAX_CHARS:
                return (len(source_chapters),)
            return None
        desired = total_chars / chapter_count
        candidates = []
        for end_index in range(start_index + 1, len(source_chapters) - groups_left + 2):
            segment_chars = prefix[end_index] - prefix[start_index]
            if not (_GAME_CHAPTER_MIN_CHARS <= segment_chars <= _GAME_CHAPTER_MAX_CHARS):
                continue
            suffix = _partition(end_index, groups_left - 1)
            if suffix is not None:
                candidates.append((abs(segment_chars - desired), end_index, suffix))
        if not candidates:
            return None
        _, end_index, suffix = min(candidates)
        return (end_index, *suffix)

    boundaries = _partition(0, chapter_count)
    if boundaries is None:
        return _split_text_by_game_chapter_size("".join(source_chapters))
    packed = []
    start_index = 0
    for end_index in boundaries:
        packed.append("".join(source_chapters[start_index:end_index]))
        start_index = end_index
    return packed


def _split_chapters(text: str):
    """
    自适应切分原文为章节列表（目标约 1.2w 字/章，通常 1w-1.5w，在安全边界处切割）。
    按游戏章节目标控制长度；过短原文章节会在无损前提下合并。
    """
    # 优先尊重原文显式章节标题。chapter_branch 的拓扑、原著兄弟章和收束点
    # 仍优先使用真实章节边界，但过短章节会合并为游戏章节。
    heading_matches = _validated_explicit_heading_matches(text)
    if heading_matches:
        chapters = []
        skipped_supplements = []
        prefix = text[: heading_matches[0].start()]
        for index, match in enumerate(heading_matches):
            end = (
                heading_matches[index + 1].start()
                if index + 1 < len(heading_matches)
                else len(text)
            )
            chapter = text[match.start() : end]
            heading = match.group(0).strip()
            if _is_supplemental_chapter_heading(heading):
                skipped_supplements.append(heading)
                continue
            if index == 0 and prefix.strip():
                chapter = prefix + chapter
            if chapter.strip():
                chapters.append(chapter)
        if len(chapters) >= 2:
            if skipped_supplements:
                print(
                    "   🧹 已从主线拓扑过滤非正文附加部分："
                    + "、".join(skipped_supplements)
                )
            normalized = _merge_short_explicit_chapters(chapters)
            if len(normalized) != len(chapters):
                print(
                    f"   📚 原文章节已按游戏章节长度归一化：{len(chapters)} → {len(normalized)} 章"
                )
            return normalized

    total_chars = len(text)
    return _split_text_by_game_chapter_size(text)


def _rebalance_last_two_chapters(chapters: list, provider: str) -> list:
    """
    对最后两章做 LLM 语义重平衡：让 LLM 在合并文本中找到最自然的叙事分割点，
    返回分割标记字符串，再定位切回两章。其余章不动。
    """
    if len(chapters) < 2:
        return chapters
    from services.story_planner.core.llm_client import LLMClient
    combined = chapters[-2] + chapters[-1]
    # 1w 章长方案下，combined ≈ 2w；结局章下限按合并文本的 35% 计算，并设硬下限 4500
    # （原硬下限 10000 在 1w 章长下会让结局章吞掉整个合并文本，已废弃）
    min_ending_chars = max(4500, int(len(combined) * 0.35))
    client = LLMClient(provider=provider)
    system_prompt = (
        "你是专业文学编辑，擅长识别叙事结构。\n"
        "你将收到一段小说原文（由两章合并而成），请在原文中找到最自然的叙事分割点：\n"
        "- 【重要】原文中可能存在章节标题、章节编号、分隔线等章节边界标记，你必须完全忽略这些标记，不得以章节边界作为分割点。\n"
        "- 分割点必须基于纯叙事语义：一场对决结束、一段情感爆发的落幕、一次空间或时间的跳转、命运的重大转折点。\n"
        "- 分割点之前为'倒数第二章'（故事主体推进），之后为'原著结局章'（命运收束）。\n"
        f"- 【硬性约束】分割点之后的结局章必须不少于{min_ending_chars}字，请在文本前80%范围内寻找分割点。\n"
        "- 你不能修改任何原文文字，只需从原文中抄录分割点处连续的30-50个字（必须是原文的精确子串）作为标记。\n"
        "输出严格 JSON：{\"split_marker\": \"原文中的30-50字精确子串，紧接在分割点之后开始\"}\n"
        "注意：split_marker 必须是原文的精确子串，区分大小写，不得有任何改动。"
    )
    user_prompt = (
        f"以下是合并的两章原文（共{len(combined)}字），请通读全文后找出最佳叙事分割点：\n\n"
        f"{combined}\n\n"
        "请输出 split_marker（原文精确子串，30-50字，作为结局章的开头标志）。"
    )
    try:
        result = client.chat_completion_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=200,
        )
        marker = result.get("split_marker", "").strip()
        if marker and marker in combined:
            split_pos = combined.index(marker)
            new_second_last = combined[:split_pos].rstrip()
            new_last = combined[split_pos:]
            if len(new_second_last) >= 4500 and len(new_last) >= min_ending_chars:
                chapters = chapters[:-2] + [new_second_last, new_last]
                print(f"   ✅ 最后两章语义重平衡完成：倒数第二章 {len(new_second_last)} 字，结局章 {len(new_last)} 字")
                return chapters
        print("   ⚠️  语义重平衡：LLM未找到有效分割点，启用启发式切分...")
    except Exception as e:
        print(f"   ⚠️  语义重平衡LLM失败（{e}），启用启发式切分...")
    # 启发式兜底：仍须满足两侧硬下限，不能为了靠近 70% 位置把结局切成短残章。
    min_split_pos = 4500
    max_split_pos = len(combined) - min_ending_chars
    target = min(int(len(combined) * 0.70), max_split_pos)
    if max_split_pos <= min_split_pos:
        split_pos = max(1, len(combined) // 2)
    else:
        split_pos = _find_safe_chapter_boundary(
            combined,
            target=target,
            min_pos=min_split_pos,
            max_pos=max_split_pos,
        )
    chapters = chapters[:-2] + [combined[:split_pos], combined[split_pos:]]
    print(f"   ✅ 启发式切分完成：倒数第二章 {len(chapters[-2])} 字，结局章 {len(chapters[-1])} 字")
    return chapters


def _save_novel_ctx_to_world_bible(task_id: str, novel_ctx, art_style: str):
    """将 NovelContext 世界观写入 world_bible 表，供 chapter_branch 和 multi 共用。"""
    _ART_STYLE_MAP = {
        "ink_chinese": 1, "cyberpunk": 2, "japanese": 2,
        "dark_european": 3, "modern_realism": 3,
    }
    art_style_code = _ART_STYLE_MAP.get(art_style, 0)
    data = {
        "worldview": novel_ctx.worldview,
        "char_cards": novel_ctx.char_cards,
        "relationship_map": novel_ctx.relationship_map,
        "world_rules": novel_ctx.world_rules,
        "narrative_arc": novel_ctx.narrative_arc,
    }
    try:
        ensure_stage_lease_owned(task_id, "story_planner")
        with Session(engine) as session:
            existing = session.exec(
                select(Worldview).where(Worldview.task_id == task_id)
            ).first()
            if existing:
                existing.data = data
                existing.art_style = art_style_code
                existing.status = WorldviewStatus.CONFIRMED
                session.add(existing)
            else:
                session.add(Worldview(
                    task_id=task_id,
                    data=data,
                    art_style=art_style_code,
                    status=WorldviewStatus.CONFIRMED,
                ))
            session.commit()
        print(f"✅ 世界观已写入 world_bible 表: task_id={task_id}")
    except StageLeaseLostError:
        raise
    except Exception as e:
        raise RuntimeError(f"世界观写入 world_bible 失败: {e}") from e


def _load_chapter_branch_plan(task_id: str) -> dict:
    """读取可恢复的 chapter_branch 计划；不存在时返回空字典。"""
    from services.story_planner.core.chapter_branch_checkpoint import (
        load_chapter_branch_plan,
    )

    return load_chapter_branch_plan(task_id)


def _save_chapter_branch_plan(
    task_id: str,
    plan: dict,
    *,
    require_stage_ownership: bool = True,
    expected_run_id: str = None,
    allow_run_takeover: bool = False,
):
    """持久化断点计划；失败时终止任务，避免生成进度与数据库正文失配。"""
    try:
        if require_stage_ownership:
            ensure_stage_lease_owned(task_id, "story_planner")
        from services.story_planner.core.chapter_branch_runtime import (
            refresh_plan_topology_fingerprint,
        )
        from services.story_planner.core.chapter_branch_checkpoint import (
            save_chapter_branch_plan,
        )
        refresh_plan_topology_fingerprint(plan)
        save_chapter_branch_plan(
            task_id,
            plan,
            expected_run_id=(
                str(plan.get("run_id") or "")
                if expected_run_id is None
                else str(expected_run_id or "")
            ),
            allow_run_takeover=allow_run_takeover,
            ownership_check=(
                (lambda: ensure_stage_lease_owned(task_id, "story_planner"))
                if require_stage_ownership
                else None
            ),
        )
        print(f"✅ chapter_branch 断点计划已保存: task_id={task_id}")
    except StageLeaseLostError:
        raise
    except Exception as e:
        raise RuntimeError(f"chapter_branch 断点计划保存失败: {e}") from e


def _record_chapter_branch_heartbeat(
    task_id: str,
    payload: dict,
    *,
    db_engine=engine,
) -> bool:
    """Persist one fenced node heartbeat without overwriting another run."""
    with Session(db_engine) as session:
        statement = select(ChapterBranchPlan).where(
            ChapterBranchPlan.task_id == task_id
        )
        if session.connection().dialect.name == "postgresql":
            statement = statement.with_for_update()
        existing = session.exec(statement).first()
        if not existing or not isinstance(existing.topology_json, dict):
            return False
        plan = dict(existing.topology_json)
        if plan.get("run_id") != payload.get("run_id"):
            return False

        topo_id = str(payload.get("topo_id", ""))
        nodes = {
            key: dict(value) if isinstance(value, dict) else {}
            for key, value in plan.get("nodes", {}).items()
        }
        node = nodes.get(topo_id)
        if node is None:
            return False
        node.update(
            {
                "real_id": payload.get("real_id", node.get("real_id", "")),
                "started_at": payload.get("started_at", node.get("started_at", "")),
                "heartbeat_at": payload.get("heartbeat_at", ""),
                "elapsed_seconds": payload.get("elapsed_seconds", 0),
            }
        )
        nodes[topo_id] = node
        plan["nodes"] = nodes
        plan["heartbeat_at"] = payload.get("heartbeat_at", "")
        plan["runtime"] = {
            "stage": payload.get("stage", "story_planner"),
            "status": payload.get("status", "running"),
            "current_topo_id": topo_id,
            "current_real_id": payload.get("real_id", ""),
            "heartbeat_at": payload.get("heartbeat_at", ""),
            "elapsed_seconds": payload.get("elapsed_seconds", 0),
        }
        existing.topology_json = plan
        session.add(existing)
        session.commit()
        return True


def _parse_chapter_branch_mode_once(
    novel_path,
    novel_name: str,
    game_project: str,
    global_style: str = None,
    content_warning_source_text: str | None = None,
):
    """
    章节分支模式（升级版）：
    1. 原文自适应切章 + 头部元信息清洗
    2. 小说预分析（世界观 + 章节摘要 + 风格样本）
    3. LLM 设计故事拓扑图（支持中期 What-If 硬分支 + 收束 DAG）
    4. 原著节点注入场景级软分支（差分剧场）
    5. What-If 章节由 LLM 全新生成
    6. 结局节点：分场大纲 → 循环扩写
    7. 保存 MD + story_tree.json，触发下游
    """
    from datetime import datetime as _dt, timezone as _timezone
    from services.story_planner.core.story_generator import StoryTree, StoryNode, StoryChoice
    from services.story_planner.core.ui_mode import infer_ui_mode
    from services.story_planner.table.story_tree_to_db import (
        get_story_md_by_name_strict,
    )
    from services.story_planner.core.branch_designer import (
        design_story_topology,
        generate_whatif_chapter,
        enforce_whatif_content_budget,
        WhatIfContentBudgetError,
        repair_whatif_convergence_segment,
        try_repair_whatif_convergence_segment,
        repair_whatif_until_compatible,
        review_whatif_downstream_transition,
        is_acceptable_whatif_transition_review,
        classify_whatif_validation_issue,
        refresh_whatif_choice_text,
        normalize_whatif_merge_policy,
        normalize_topology_choice_texts,
        resolve_whatif_merge_window,
    )
    from services.story_planner.core.soft_branch import (
        inject_soft_branches,
        merge_soft_branch_metrics,
        new_soft_branch_metrics,
        remember_choices_from_markdown,
        soft_branch_generation_identity,
        strip_soft_branch_blocks,
    )
    from services.story_planner.core.effects_validator import (
        build_effect_key_schedule,
        calculate_effect_caps,
        calibrate_ending_rules,
        extract_effect_pairs_from_markdown,
        merge_effect_caps_with_observed,
        simulate_ending_route_reachability,
        validate_threshold_reachability,
    )
    from services.story_planner.core.ending_writer import (
        generate_ending_content,
        review_variable_legacy_evidence,
        review_ending_variable_causality,
    )
    from services.story_planner.core.novel_reader import (
        ContentConsistencyRepairRequired,
        NARRATIVE_POV_CONTRACT_VERSION,
        analyze_novel,
        detect_narrative_pov_drift,
        ensure_novel_context_worldview,
        infer_narrative_perspective_contract,
        infer_narrative_reality_contract,
        terminal_states_are_cacheable,
        terminal_states_have_life_schema,
    )
    from services.story_planner.utils.result_cache import ResultCache
    from services.story_planner.core.chapter_branch_runtime import (
        NodeSelfHealRequired,
        PLAN_VERSION,
        CHAPTER_BRANCH_STEP_VERSIONS,
        ReleaseGateError,
        ReleaseQualityRepairRequired,
        build_route_metadata,
        can_reuse_plan_step,
        can_resume_node_checkpoint,
        content_sha256,
        deterministic_node_id,
        ending_semantic_review_fingerprint,
        estimate_soft_branch_counts,
        get_node_regeneration_reason,
        get_reusable_plan_step_output,
        get_reusable_ending_semantic_review,
        get_reusable_chapter_substep,
        get_active_regeneration_targets,
        get_active_node_self_heal_targets,
        has_reusable_topology_checkpoint,
        plan_original_chapter_scene_count,
        plan_whatif_budget,
        initialize_chapter_branch_plan,
        invalidate_chapter_substeps_from,
        invalidate_checkpoint_outputs_for_steps,
        mark_regeneration_request_completed,
        mark_node_self_heal_completed,
        reconcile_plan_steps,
        record_chapter_substep,
        record_ending_semantic_review,
        record_plan_step_output,
        stable_payload_digest,
        estimate_total_soft_branches,
        update_plan_step,
        update_plan_node,
        validate_generated_content,
        prepare_generated_content_for_release,
        record_optional_branch_failure,
        new_optional_branch_attempt_budget,
        consume_optional_branch_attempt,
        remaining_optional_branch_attempts,
        schedule_node_self_heal,
        schedule_release_quality_repair,
        evaluate_convergence_states,
        classify_convergence_repair,
        escalate_state_repair_strategy,
        validate_release_integrity,
        mark_incompatible_whatif_terminal,
        omit_optional_topology_branch,
        should_force_whatif_convergence,
        topology_fingerprint,
        build_run_metrics,
    )
    from services.story_planner.core.chapter_branch_quality import (
        append_quality_warnings,
        build_release_repair_directive,
        infer_ending_legacy_flags,
        validate_ending_variable_causality,
        validate_chapter_branch_release,
    )
    from services.story_planner.core.content_warnings import (
        CONTENT_WARNING_DETECTOR_VERSION,
        build_work_content_warnings,
        content_warning_ruleset_digest,
        content_warnings_payload_digest,
        is_reusable_content_warnings_checkpoint,
        normalize_content_warnings,
        validate_content_warnings_schema,
    )
    from services.story_planner.core.runtime_observability import (
        detect_stalled_node,
        start_runtime_heartbeat,
        stop_runtime_heartbeat,
        structured_log,
    )
    from common.progress_utils import renew_stage_lease

    print(f"📖 章节分支模式（升级版）：读取原文并拆分章节...")

    encoding_result = read_novel_text(novel_path, convert_to_utf8=True)
    if encoding_result.converted:
        print(
            f"📝 已将小说从 {encoding_result.source_encoding} 原子转换为无 BOM UTF-8: "
            f"{novel_path}（原始 {encoding_result.byte_count} 字节）"
        )
    text = encoding_result.text
    working_text_before_cleanup = text
    _raw_source_fingerprint = _content_warning_sha256(working_text_before_cleanup)
    # Preprocessing is checkpointed below after cleanup; on a redelivery the
    # verified result is restored before any LLM-backed cleanup is attempted.
    from services.story_planner.core.chapter_branch_resume import (
        build_chapter_processing_checkpoint,
        get_reusable_chapter_processing_checkpoint,
    )
    _preprocess_plan = _load_chapter_branch_plan(game_project)
    _processing_checkpoint_metadata = _preprocess_plan.get("preprocessing_checkpoint")
    _processing_checkpoint_payload = _load_chapter_processing_checkpoint(
        game_project,
        _processing_checkpoint_metadata,
    )
    _processing_checkpoint = _processing_checkpoint_metadata
    _processing_checkpoint_for_validation = (
        _processing_checkpoint_payload or _processing_checkpoint_metadata
    )
    if _processing_checkpoint_payload and not _processing_checkpoint_metadata:
        _processing_checkpoint = {
            key: value
            for key, value in _processing_checkpoint_payload.items()
            if key != "chapters"
        }
    elif isinstance(_processing_checkpoint_metadata, dict) and "chapters" in _processing_checkpoint_metadata:
        _processing_checkpoint = {
            key: value
            for key, value in _processing_checkpoint_metadata.items()
            if key != "chapters"
        }
    if (
        isinstance(_processing_checkpoint_for_validation, dict)
        and _preprocess_plan.get("source_fingerprint")
        and _processing_checkpoint_for_validation.get("cleaned_source_fingerprint")
        != _preprocess_plan.get("source_fingerprint")
    ):
        _processing_checkpoint_for_validation = None
    _reused_processing_chapters = get_reusable_chapter_processing_checkpoint(
        _processing_checkpoint_for_validation,
        raw_text=working_text_before_cleanup,
    )
    if _reused_processing_chapters is not None:
        chapters = list(_reused_processing_chapters)
        text = "".join(chapters)
        print("   ♻️  复用 chapter_branch 预处理断点：跳过头部清洗和章节切分")
    else:
        print(f"馃Ч 娓呮礂鍘熸枃澶撮儴鍏冧俊鎭紙浣滆€?绠€浠?瀛楁暟鏍囩绛夛級...")
        text = _strip_front_matter(text)
        text = _strip_act_title_prefix(text)
        chapters = _split_chapters(text)
        _processing_checkpoint = build_chapter_processing_checkpoint(
            working_text_before_cleanup,
            chapters,
        )
        _processing_checkpoint = _persist_chapter_processing_checkpoint(
            game_project,
            _processing_checkpoint,
        )
    text = "".join(chapters)
    total_chars = len(text)
    _source_fingerprint = content_sha256(text)
    print(f"   原文总字数: {total_chars}")

    # ==================== 1. 切章 ====================
    visual_style_source_chapters = list(chapters)
    chapter_count = len(chapters)

    # The task may have translated and overwritten the working novel file
    # before entering chapter_branch.  Keep a canonical source copy in memory
    # for the work-level content-warning detector so source-language signals
    # are not lost.  Direct callers (smoke runner/regeneration) simply fall
    # back to the current working text.
    if not content_warning_source_text:
        # Regeneration and direct diagnostic callers do not pass the source
        # captured by parse_novel_to_draft. Recover it from the shared sidecar
        # when the current working text matches a known delivery.
        content_warning_source_text = _load_content_warning_source(
            game_project,
            working_text_before_cleanup,
        )
    _content_warning_source_text = str(content_warning_source_text or text)
    if (
        content_warning_source_text
        and _reused_processing_chapters is not None
        and _content_warning_sha256(content_warning_source_text)
        == _raw_source_fingerprint
    ):
        _content_warning_source_chapters = list(chapters)
        _content_warning_source_text = "".join(chapters)
    elif content_warning_source_text:
        _content_warning_source_text = _strip_front_matter(
            _content_warning_source_text
        )
        _content_warning_source_text = _strip_act_title_prefix(
            _content_warning_source_text
        )
        _content_warning_source_chapters = _split_chapters(
            _content_warning_source_text
        )
        if not _content_warning_source_chapters:
            _content_warning_source_chapters = [_content_warning_source_text]
        if len(_content_warning_source_chapters) != chapter_count:
            print(
                "   ⚠️  内容提示源文本章节数与工作文本不一致："
                f"source={len(_content_warning_source_chapters)}，working={chapter_count}；"
                "证据章节按源文本编号"
            )
    else:
        _content_warning_source_chapters = list(chapters)
    _content_warning_source_fingerprint = content_sha256(
        _content_warning_source_text
    )
    print(f"   拆分章节数: {chapter_count}")

    _preflight_report = run_novel_preflight(
        text=text,
        chapters=chapters,
        story_mode=StoryMode.CHAPTER_BRANCH,
    )
    print(_preflight_report.format_text())

    # ==================== 2. 获取配置 ====================
    config = _get_task_config(game_project)
    output_language = config.get("output_language")
    provider = os.getenv("LLM_PROVIDER", "openai")
    task_id = game_project
    if not global_style:
        # 定向重生成应复用已发布的画风，避免只重开部分节点时画风漂移。
        try:
            from common.global_style import get_global_style
            global_style = get_global_style(task_id)
            print(f"🎨 复用已发布 global_style: {global_style}")
        except Exception:
            global_style = None
            print("🎨 尚无已发布 global_style；交由视觉阶段生成并发布 Anchor")
    _run_id = uuid.uuid4().hex
    _run_started_at = _dt.now(_timezone.utc).isoformat()
    _run_started = time.perf_counter()
    _run_metrics = {
        "cache_hits": 0,
        "cache_misses": 0,
        "nodes_resumed": 0,
        "nodes_generated": 0,
        "optional_branches_omitted": 0,
        "soft_branches": new_soft_branch_metrics(),
        "phase_seconds": {},
    }
    structured_log(
        _runtime_logger,
        "chapter_branch_run_started",
        task_id=task_id,
        run_id=_run_id,
        stage="story_planner",
        branch_type="pending",
        source_chars=total_chars,
        chapter_count=chapter_count,
    )
    _result_cache = ResultCache(
        RUNTIME_SETTINGS.cache_dir,
        namespace=RUNTIME_SETTINGS.cache_version,
        max_age_seconds=RUNTIME_SETTINGS.cache_max_age_days * 86400,
    )

    if _reused_processing_chapters is None and _should_rebalance_chapters(chapters):
        print("   🔄 对最后两章进行语义重平衡...")
        chapters = _rebalance_last_two_chapters(chapters, provider)
        chapter_count = len(chapters)
        _processing_checkpoint = _persist_chapter_processing_checkpoint(
            game_project,
            build_chapter_processing_checkpoint(
                working_text_before_cleanup,
                chapters,
            ),
        )
    elif len(chapters) >= 2:
        print("   ✅ 显式章节边界已验证；短章已按游戏章节长度合并，跳过语义重平衡")

    # Rebalancing changes chapter boundaries, so the canonical text and its
    # fingerprint must be refreshed before any downstream checkpoint identity
    # is computed.
    text = "".join(chapters)
    total_chars = len(text)
    _source_fingerprint = content_sha256(text)
    chapter_count = len(chapters)

    _existing_plan = _load_chapter_branch_plan(task_id)
    _loaded_plan_run_id = str(_existing_plan.get("run_id") or "")
    if (
        _existing_plan.get("source_fingerprint")
        and _existing_plan.get("source_fingerprint") != _source_fingerprint
    ):
        print("\n⚠️  原文指纹已变化，旧 chapter_branch_plan 不再复用")
        _existing_plan = {}
    _previous_run_metrics = (
        _existing_plan.get("metrics", {})
        if isinstance(_existing_plan.get("metrics", {}), dict)
        else {}
    )

    # ==================== 3. 小说预分析（世界观 + 章节摘要 + 角色卡） ====================
    print(f"\n🔍 小说预分析（世界观+角色+摘要）...")
    _analysis_started = time.perf_counter()
    from services.story_planner.core.chapter_branch_resume import (
        build_global_style_checkpoint,
        build_novel_analysis_step_input,
        deserialize_novel_context,
        serialize_novel_context,
    )

    _analysis_step_input = build_novel_analysis_step_input(
        source_fingerprint=_source_fingerprint,
        chapters=chapters,
        output_language=output_language,
        provider=provider,
        model=DEFAULT_MODEL,
    )

    _saved_analysis_payload = get_reusable_plan_step_output(
        _existing_plan,
        "novel_analysis",
        input_payload=_analysis_step_input,
        producer_version=CHAPTER_BRANCH_STEP_VERSIONS["novel_analysis"],
    )
    novel_ctx = deserialize_novel_context(_saved_analysis_payload)
    _analysis_resume_source = "database" if novel_ctx is not None else ""
    if novel_ctx is not None and len(novel_ctx.chapter_summaries) != len(chapters):
        print("   ⚠️  数据库预分析断点章节数不匹配，忽略并重新分析")
        novel_ctx = None
        _analysis_resume_source = ""
    _novel_context_key = _result_cache.get_cache_key(
        text,
        {
            "stage": "novel_context",
            "provider": provider,
            "model": DEFAULT_MODEL,
            "output_language": output_language,
            "chapter_count": len(chapters),
            "chapter_boundaries": [len(chapter) for chapter in chapters],
            "chapter_starts": [chapter[:80] for chapter in chapters],
            # Bump the local analysis cache namespace after introducing the
            # persisted POV contract.  The explicit contract version below
            # also makes future POV rule changes invalidate this key even if
            # the generic cache version is left unchanged.
            "cache_version": 5,
            "summary_schema_version": 2,
            "narrative_pov_contract_version": NARRATIVE_POV_CONTRACT_VERSION,
            "producer_version": CHAPTER_BRANCH_STEP_VERSIONS["novel_analysis"],
        },
    )
    if novel_ctx is None:
        novel_ctx = _result_cache.get(_novel_context_key)
        if novel_ctx is not None:
            _analysis_resume_source = "local_cache"
    if novel_ctx is None:
        _run_metrics["cache_misses"] += 1
        novel_ctx = analyze_novel(
            chapters=chapters,
            full_text=text,
            provider=provider,
            output_language=output_language,
        )
        _result_cache.set(_novel_context_key, novel_ctx)
    else:
        _run_metrics["cache_hits"] += 1
        if _analysis_resume_source == "database":
            print("   ♻️  命中数据库小说预分析断点")
        else:
            print("   ♻️  命中小说预分析缓存")
        novel_ctx = ensure_novel_context_worldview(
            novel_ctx,
            full_text=text,
            provider=provider,
            output_language=output_language,
        )
        _result_cache.set(_novel_context_key, novel_ctx)
    _run_metrics["phase_seconds"]["analysis"] = round(
        time.perf_counter() - _analysis_started, 3
    )
    summaries = novel_ctx.chapter_summaries
    style_ref = novel_ctx.style_samples
    branch_type = getattr(novel_ctx, "branch_type", "default")
    story_ui_mode = infer_ui_mode(text=text, metadata=novel_ctx.worldview)
    _narrative_reality_contract = infer_narrative_reality_contract(
        novel_ctx.worldview
    )
    _narrative_reality_contract["branch_type"] = branch_type
    novel_ctx.worldview["narrative_reality_contract"] = dict(
        _narrative_reality_contract
    )
    # 叙事人称是所有生成正文共享的契约。它与 reality contract 完全独立：
    # 前者约束“谁在叙述”，后者约束“故事发生在哪一层现实/时间线”。
    # 旧缓存没有该字段时按当前原文样本确定性推断，并立即写回上下文，
    # 确保后续断点恢复和所有生成入口都拿到同一份契约。
    _narrative_pov_contract = infer_narrative_perspective_contract(
        novel_ctx.worldview,
        chapters=chapters,
        style_samples=style_ref,
        text=text,
    )
    novel_ctx.narrative_pov_contract = dict(_narrative_pov_contract)
    novel_ctx.worldview["narrative_pov_contract"] = dict(_narrative_pov_contract)
    print(
        "   👁️  叙事视角契约："
        f"{_narrative_pov_contract.get('perspective', 'unknown')}"
        f"（主角={_narrative_pov_contract.get('protagonist_name') or '未确定'}，"
        f"置信度={_narrative_pov_contract.get('confidence', 0):.2f}）"
    )
    structured_log(
        _runtime_logger,
        "phase_completed",
        task_id=task_id,
        run_id=_run_id,
        stage="story_planner",
        branch_type=branch_type,
        phase="analysis",
        duration_seconds=_run_metrics["phase_seconds"]["analysis"],
    )
    print(f"   🔀 分支类型自动检测结果：{branch_type}")

    # 作品级内容提示是独立于玩法类型的附加元数据。它不并入 worldview
    # JSON，避免提高世界观主响应的复杂度；结果按输入指纹写入断点，重跑时
    # 只有原文、摘要、模型或检测器版本变化才重新检测。
    _content_warning_input = {
        "source_fingerprint": _source_fingerprint,
        "source_detection_fingerprint": _content_warning_source_fingerprint,
        "chapter_count": len(chapters),
        "chapter_sha256": [content_sha256(chapter) for chapter in chapters],
        "source_detection_chapter_sha256": [
            content_sha256(chapter) for chapter in _content_warning_source_chapters
        ],
        "summary_digest": stable_payload_digest(summaries),
        "provider": provider,
        "model": DEFAULT_MODEL,
        "detector_version": CONTENT_WARNING_DETECTOR_VERSION,
        "ruleset_digest": content_warning_ruleset_digest(),
        "prompt_version": os.getenv(
            "STORY_PLANNER_CONTENT_WARNING_PROMPT_VERSION", "work-content-warning-prompt-v1"
        ),
        "localization_version": os.getenv(
            "STORY_PLANNER_CONTENT_WARNING_LOCALIZATION_VERSION",
            "work-content-warning-l10n-v1",
        ),
        "verify_enabled": os.getenv(
            "STORY_PLANNER_CONTENT_WARNING_LLM_VERIFY", "1"
        ),
        "base_url": os.getenv("STORY_PLANNER_LLM_BASE_URL", ""),
    }
    _content_warning_input_digest = stable_payload_digest(_content_warning_input)
    _saved_content_warning_checkpoint = (
        _existing_plan.get("content_warnings_checkpoint")
        if isinstance(_existing_plan.get("content_warnings_checkpoint"), dict)
        else {}
    )
    _saved_content_warnings = _saved_content_warning_checkpoint.get("output")
    if is_reusable_content_warnings_checkpoint(
        _saved_content_warning_checkpoint,
        input_digest=_content_warning_input_digest,
        detector_version=CONTENT_WARNING_DETECTOR_VERSION,
        ruleset_digest=_content_warning_input["ruleset_digest"],
    ):
        _content_warnings = normalize_content_warnings(
            _saved_content_warnings,
            chapter_count=len(chapters),
        )
        print("   ♻️  复用作品级内容提示断点")
    else:
        _content_warnings = build_work_content_warnings(
            text,
            chapters=chapters,
            source_text=_content_warning_source_text,
            source_chapters=_content_warning_source_chapters,
            chapter_summaries=summaries,
            worldview=novel_ctx.worldview,
            provider=provider,
            output_language=output_language,
        )
        _content_warnings = normalize_content_warnings(
            _content_warnings,
            chapter_count=len(chapters),
        )
        _content_warning_issues = validate_content_warnings_schema(
            _content_warnings,
            require_localized=True,
        )
        if _content_warning_issues:
            # 附加提示不能阻塞主流程；出现结构异常时使用明确的人工复核
            # 结果，避免把“未检测到”误报成“没有敏感内容”。
            print(
                "   ⚠️  作品级内容提示结构异常，降级为人工复核："
                + "；".join(_content_warning_issues)
            )
            _content_warnings = normalize_content_warnings(
                {
                    "work": {
                        "status": "review_required",
                        "has_sensitive_content": False,
                        "detector": {
                            "method": "planner_fallback",
                            "review_required": True,
                        },
                    }
                },
                chapter_count=len(chapters),
            )
    _content_warning_checkpoint = {
        "schema_version": 1,
        "input_digest": _content_warning_input_digest,
        "input": _content_warning_input,
        "output": _content_warnings,
        "output_digest": content_warnings_payload_digest(_content_warnings),
        "detector_version": CONTENT_WARNING_DETECTOR_VERSION,
        "ruleset_digest": _content_warning_input["ruleset_digest"],
    }
    _content_warning_work = _content_warnings.get("work", {})
    print(
        "   ⚠️  作品级内容提示："
        f"status={_content_warning_work.get('status', 'none')}，"
        f"categories={len(_content_warning_work.get('categories', []) or [])}，"
        f"severity={_content_warning_work.get('severity', 'none')}"
    )
    # 世界观入库（与 multi 模式共用 world_bible 表）
    _save_novel_ctx_to_world_bible(
        task_id,
        novel_ctx,
        config.get("art_style", "ink_chinese"),
    )

    _saved_topology_result = (
        _existing_plan.get("topology_result")
        if isinstance(_existing_plan.get("topology_result"), dict)
        else (
            _existing_plan
            if isinstance(_existing_plan.get("topology"), list)
            else {"topology": [], "variable_defs": []}
        )
    )
    _analysis_checkpoint_plan = initialize_chapter_branch_plan(
        task_id,
        _saved_topology_result,
        branch_type,
        existing_plan=_existing_plan if _existing_plan.get("version") else None,
        source_fingerprint=_source_fingerprint,
    )
    _analysis_checkpoint_plan["run_id"] = _run_id
    _analysis_checkpoint_plan["run_started_at"] = _run_started_at
    _analysis_checkpoint_plan["heartbeat_at"] = _run_started_at
    # Keep the independent work-level warning checkpoint available even if a
    # later topology/node step fails.  It is intentionally not part of the
    # ordered pipeline steps so adding this optional metadata does not migrate
    # historical PLAN_VERSION values.
    _analysis_checkpoint_plan["content_warnings_checkpoint"] = (
        _content_warning_checkpoint
    )
    _analysis_checkpoint_plan["preprocessing_checkpoint"] = _processing_checkpoint
    if global_style:
        _analysis_checkpoint_plan["global_style_checkpoint"] = (
            build_global_style_checkpoint(
                global_style,
                source_fingerprint=_source_fingerprint,
                model=DEFAULT_MODEL,
            )
        )
    record_plan_step_output(
        _analysis_checkpoint_plan,
        "novel_analysis",
        input_payload=_analysis_step_input,
        output=serialize_novel_context(novel_ctx),
        producer_version=CHAPTER_BRANCH_STEP_VERSIONS["novel_analysis"],
    )
    _save_chapter_branch_plan(
        task_id,
        _analysis_checkpoint_plan,
        expected_run_id=_loaded_plan_run_id,
        allow_run_takeover=bool(_loaded_plan_run_id != _run_id),
    )
    _existing_plan = _analysis_checkpoint_plan
    _loaded_plan_run_id = _run_id

    # ==================== 4. LLM 设计故事拓扑 ====================
    _topology_started = time.perf_counter()
    _stalled_nodes = []
    for _stalled_topo_id, _stalled_state in _existing_plan.get("nodes", {}).items():
        _stall_issue = detect_stalled_node(
            _stalled_topo_id,
            _stalled_state,
            timeout_seconds=RUNTIME_SETTINGS.node_stall_timeout_seconds,
        )
        if _stall_issue:
            _stalled_nodes.append(_stall_issue)
            structured_log(
                _runtime_logger,
                "stalled_node_detected",
                level="warning",
                task_id=task_id,
                run_id=_existing_plan.get("run_id", ""),
                stage="story_planner",
                branch_type=branch_type,
                **_stall_issue,
            )
    _saved_topology_result = (
        _existing_plan.get("topology_result")
        if isinstance(_existing_plan.get("topology_result"), dict)
        else (_existing_plan if isinstance(_existing_plan.get("topology"), list) else None)
    )
    _analysis_output_digest = stable_payload_digest(
        {
            "worldview": novel_ctx.worldview,
            "chapter_summaries": summaries,
            "branch_type": branch_type,
        }
    )
    _saved_analysis_step = (
        (_existing_plan.get("steps", {}) or {}).get("novel_analysis", {})
        if isinstance(_existing_plan.get("steps"), dict)
        else {}
    )
    _analysis_step_reusable = bool(
        not _saved_analysis_step.get("input_digest")
        or can_reuse_plan_step(
            _existing_plan,
            "novel_analysis",
            input_payload=_analysis_step_input,
        )
    )
    _topology_step_input = {
        "source_fingerprint": _source_fingerprint,
        "analysis_output_digest": _analysis_output_digest,
        "branch_type": branch_type,
        "output_language": output_language,
        "model": DEFAULT_MODEL,
    }
    _saved_topology_step = (
        (_existing_plan.get("steps", {}) or {}).get("topology", {})
        if isinstance(_existing_plan.get("steps"), dict)
        else {}
    )
    _topology_step_reusable = bool(
        _analysis_step_reusable
        and (
            not _saved_topology_step.get("input_digest")
            or can_reuse_plan_step(
                _existing_plan,
                "topology",
                input_payload=_topology_step_input,
            )
        )
    )
    if has_reusable_topology_checkpoint(
        _existing_plan,
        branch_type=branch_type,
        analysis_step_reusable=_analysis_step_reusable,
        topology_step_reusable=_topology_step_reusable,
    ):
        topo_result = _saved_topology_result
        print(f"\n♻️  复用 chapter_branch_plan 中已确认的故事拓扑")
    else:
        if _saved_topology_result and not _topology_step_reusable:
            print("\n⚠️  拓扑步骤输入或生产版本变化，重新设计故事拓扑")
        if (
            _saved_topology_result
            and int(_existing_plan.get("version", 0) or 0) not in {5, PLAN_VERSION}
        ):
            print(
                "\n⚠️  chapter_branch_plan 版本已升级，旧拓扑不再复用："
                f"{_existing_plan.get('version', 0)} → {PLAN_VERSION}"
            )
        print(f"\n🗺️  设计故事拓扑图...")
        topo_result = design_story_topology(
            chapters,
            summaries,
            provider,
            persona_axes=novel_ctx.persona_axes,
            output_language=output_language,
            is_romance_oriented=novel_ctx.is_romance_oriented,
            romance_targets=novel_ctx.worldview.get("romance_targets", []),
            ending_dimensions=novel_ctx.ending_dimensions,
            branch_type=branch_type,
        )
    topo_result = normalize_topology_choice_texts(topo_result)
    topology = topo_result.get("topology", [])
    for _topology_node in topology:
        if (
            isinstance(_topology_node, dict)
            and _topology_node.get("type") == "chapter"
            and not _topology_node.get("is_original")
        ):
            normalize_whatif_merge_policy(_topology_node)
    variable_defs = topo_result.get("variable_defs", [])
    print(f"   拓扑节点数: {len(topology)}，数值变量: {[v['key'] for v in variable_defs]}")

    _ending_semantic_review_cache: dict[str, dict] = {}
    _ending_semantic_review_fields = {
        "variable_behavior_linked",
        "observable_payoff",
        "causal_payoff",
        "route_specific",
        "target_linked",
        "relationship_confirmation",
        "shared_future",
        "fatal_state_consistent",
    }

    def _is_cacheable_ending_review(value: object) -> bool:
        return bool(
            isinstance(value, dict)
            and "passed" in value
            and isinstance(value.get("core_chain"), dict)
            and _ending_semantic_review_fields.issubset(value["core_chain"])
            and isinstance(value.get("evidence"), dict)
        )

    def _review_ending_causality(payload: dict) -> dict:
        variable = (payload or {}).get("variable", {})
        if not isinstance(variable, dict):
            variable = {}
        context = (payload or {}).get("context", {})
        if not isinstance(context, dict):
            context = {}
        content = str((payload or {}).get("content") or "")
        _review_key = ending_semantic_review_fingerprint(
            variable=variable,
            content=content,
            provider=provider,
            model=DEFAULT_MODEL,
            context=context,
            reviewer_version=os.getenv(
                "STORY_PLANNER_ENDING_SEMANTIC_REVIEW_VERSION",
                "ending-causality-semantic-v1",
            ),
        )
        if _review_key in _ending_semantic_review_cache:
            return dict(_ending_semantic_review_cache[_review_key])
        # Reuse only a review whose identity is tied to the exact current
        # prose, variable definition, provider and reviewer version.  This is
        # separate from the node artifact checkpoint so a changed ending can
        # never inherit a stale semantic decision.
        topology_node_id = str(
            context.get("topology_node_id") or ""
        ).strip()
        if not topology_node_id:
            ending_real_id = str(context.get("ending_id") or "").strip()
            for candidate_id, candidate_entry in (
                (_chapter_plan.get("nodes", {}) if isinstance(_chapter_plan, dict) else {})
                or {}
            ).items():
                if (
                    isinstance(candidate_entry, dict)
                    and str(candidate_entry.get("real_id") or "") == ending_real_id
                ):
                    topology_node_id = str(candidate_id)
                    break
        if topology_node_id:
            reviewed = get_reusable_ending_semantic_review(
                _chapter_plan,
                topology_node_id,
                fingerprint=_review_key,
            )
            if _is_cacheable_ending_review(reviewed):
                _ending_semantic_review_cache[_review_key] = reviewed
                print(
                    "   ♻️  复用结局语义校验断点："
                    f"topo_id={topology_node_id}"
                )
                return reviewed
        print(
            "   🔎 执行大模型语义因果校验："
            f"variable={variable.get('key', '')}, ending_id={context.get('ending_id', '')}"
        )
        _review = review_ending_variable_causality(
            ending_text=content,
            variable_definition=variable,
            provider=provider,
            worldview=getattr(novel_ctx, "worldview", None),
            context=context,
        )
        # Transport/format failures must not be persisted as reusable reviews;
        # a later resume should get another chance against the gateway.  A
        # model's valid ``passed=false`` decision is cacheable because it is
        # tied to the exact artifact fingerprint and will be cleared on repair.
        if _is_cacheable_ending_review(_review):
            _ending_semantic_review_cache[_review_key] = dict(_review)
            if topology_node_id:
                record_ending_semantic_review(
                    _chapter_plan,
                    topology_node_id,
                    fingerprint=_review_key,
                    review=_review,
                )
        return _review

    # 把逐章摘要回写进拓扑的原著章节节点 summary 字段（LLM 规划拓扑时 summary 可能为 null）。
    # 原著节点的 summary = 真实剧情摘要（来自 analyze_novel）；
    # 同时把规划期给的"走向方向"（如有）保留到 plan_direction，避免被覆盖。
    # 注意：branch_point（岔口节点）也是原著章节，必须一并写回，否则前端列表会出现空 summary。
    for t in topology:
        if t.get("is_original") and t.get("type") in ("chapter", "branch_point"):
            idx = t.get("source_chapter_idx")
            if idx is not None and idx < len(summaries):
                if t.get("summary") and t["summary"] != summaries[idx]:
                    t["plan_direction"] = t.get("summary", "")
                t["summary"] = summaries[idx]
    # 原著结局节点（最后一章）也写实摘要，避免列表里看到 null。
    for t in topology:
        if t.get("is_original") and t.get("type") == "ending":
            idx = t.get("source_chapter_idx")
            if idx is not None and idx < len(summaries):
                if t.get("summary") and t["summary"] != summaries[idx]:
                    t["plan_direction"] = t.get("summary", "")
                t["summary"] = summaries[idx]

    # 根据 next 关系反向计算每个节点的 parent_ids（LLM 不需要输出此字段）
    _parent_map: dict = {t["node_id"]: [] for t in topology}
    for t in topology:
        for nxt in t.get("next", []):
            if nxt in _parent_map:
                _parent_map[nxt].append(t["node_id"])
    for t in topology:
        t["parent_ids"] = _parent_map.get(t["node_id"], [])

    # 拓扑排序：保证父节点在子节点之前处理，防止 terminal_states / depth 静默错误
    from collections import deque as _deque
    _node_by_topo_id = {t["node_id"]: t for t in topology}
    _in_degree = {t["node_id"]: len(t.get("parent_ids", [])) for t in topology}
    _topo_queue = _deque(nid for nid, deg in _in_degree.items() if deg == 0)
    _sorted_topology = []
    while _topo_queue:
        _nid = _topo_queue.popleft()
        if _nid in _node_by_topo_id:
            _sorted_topology.append(_node_by_topo_id[_nid])
        for _nxt in _node_by_topo_id.get(_nid, {}).get("next", []):
            if _nxt in _in_degree:
                _in_degree[_nxt] -= 1
                if _in_degree[_nxt] == 0:
                    _topo_queue.append(_nxt)
    if len(_sorted_topology) == len(topology):
        topology = _sorted_topology
        print(f"   ✅ 拓扑排序完成（{len(topology)} 个节点，父节点已确保先于子节点）")
    else:
        raise ValueError("拓扑排序失败：" + (
            f"仅排序出 {len(_sorted_topology)}/{len(topology)} 个节点，禁止继续生成正文"
        ))

    topo_result["topology"] = topology
    _preflight_report = calibrate_chapter_branch_preflight(
        _preflight_report,
        topology=topology,
        branch_type=branch_type,
        chapters=chapters,
    )
    print("\n📐 按实际分支类型校准预检估算")
    print(_preflight_report.format_text())
    _chapter_plan = initialize_chapter_branch_plan(
        task_id,
        topo_result,
        branch_type,
        existing_plan=_existing_plan if _existing_plan.get("version") else None,
        source_fingerprint=_source_fingerprint,
    )
    # Persist the contract alongside the resumable plan.  This makes a resumed
    # run auditable and prevents an old checkpoint from silently using a new
    # narrator rule.
    _chapter_plan["narrative_pov_contract"] = dict(_narrative_pov_contract)
    _chapter_plan["preflight"] = _preflight_report.to_dict()
    _chapter_plan["run_id"] = _run_id
    _chapter_plan["run_started_at"] = _run_started_at
    _chapter_plan["heartbeat_at"] = _run_started_at
    _chapter_plan["content_warnings_checkpoint"] = _content_warning_checkpoint
    _chapter_plan["preprocessing_checkpoint"] = _processing_checkpoint
    _chapter_plan["stalled_nodes_detected"] = _stalled_nodes
    _chapter_plan["soft_branch_generation_identity"] = (
        soft_branch_generation_identity(branch_type)
    )
    _step_inputs = {
        "novel_analysis": _analysis_step_input,
        "topology": _topology_step_input,
        "terminal_states": {
            "source_fingerprint": _source_fingerprint,
            "branch_type": branch_type,
            "chapter_count": len(chapters),
            "life_state_schema_version": 2,
            "producer_version": CHAPTER_BRANCH_STEP_VERSIONS["terminal_states"],
            "narrative_contract": _narrative_reality_contract,
        },
        "node_generation": {
            "topology_fingerprint": topology_fingerprint(topo_result),
            "output_language": output_language,
            "model": DEFAULT_MODEL,
            "branch_type": branch_type,
            "narrative_pov_contract": _narrative_pov_contract,
            **soft_branch_generation_identity(branch_type),
        },
        "release_gate": {
            "topology_fingerprint": topology_fingerprint(topo_result),
            "min_generated_chars": int(
                os.getenv("STORY_PLANNER_RELEASE_MIN_GENERATED_CHARS", "300")
            ),
            "min_endings": int(
                os.getenv("STORY_PLANNER_RELEASE_MIN_ENDINGS", "1")
            ),
            "min_score": int(
                os.getenv("STORY_PLANNER_RELEASE_MIN_SCORE", "30")
            ),
            "soft_branch_transition_drop_limit": float(
                os.getenv("STORY_PLANNER_SOFT_BRANCH_TRANSITION_DROP_LIMIT", "0.65")
            ),
            "whatif_transition_min_score": int(
                os.getenv("STORY_PLANNER_TRANSITION_MIN_SCORE", "5")
            ),
        },
        "story_publish": {
            "topology_fingerprint": topology_fingerprint(topo_result),
            "player_language": get_story_tree_player_language(),
        },
        "summary_publish": {
            "topology_fingerprint": topology_fingerprint(topo_result),
            "summary_schema_version": 1,
        },
        "stage_complete": {"task_id": task_id, "stage": "story_planner"},
    }
    _invalidated_steps = reconcile_plan_steps(_chapter_plan, _step_inputs)
    if _invalidated_steps:
        print(f"   ♻️  步骤输入/版本变化，已失效: {_invalidated_steps}")
        # A node self-heal/release repair is local.  Even when the shared
        # node-generation step identity changed (for example because a
        # contract gained a non-semantic audit field), preserve every other
        # completed node and reopen only the requested target.
        _target_node_ids = get_active_node_self_heal_targets(_chapter_plan)
        if not _target_node_ids:
            _target_node_ids = get_active_regeneration_targets(_chapter_plan)
        _reopened_by_step_version = invalidate_checkpoint_outputs_for_steps(
            _chapter_plan,
            _invalidated_steps,
            target_node_ids=_target_node_ids or None,
        )
        if _reopened_by_step_version:
            if _target_node_ids:
                print(
                    "   ♻️  节点生成规则变化，按定向修复范围重开节点: "
                    f"{_reopened_by_step_version}"
                )
            else:
                print(
                    "   ♻️  节点生成规则变化，已重开受影响节点: "
                    f"{len(_reopened_by_step_version)} 个"
                )
    update_plan_step(
        _chapter_plan,
        "novel_analysis",
        status="completed",
        output_digest=_analysis_output_digest,
    )
    update_plan_step(
        _chapter_plan,
        "topology",
        status="completed",
        output_digest=topology_fingerprint(topo_result),
    )
    _save_chapter_branch_plan(
        task_id,
        _chapter_plan,
        expected_run_id=_loaded_plan_run_id,
        allow_run_takeover=bool(_loaded_plan_run_id != _run_id),
    )
    _run_metrics["phase_seconds"]["topology"] = round(
        time.perf_counter() - _topology_started, 3
    )
    structured_log(
        _runtime_logger,
        "phase_completed",
        task_id=task_id,
        run_id=_run_id,
        stage="story_planner",
        branch_type=branch_type,
        phase="topology",
        duration_seconds=_run_metrics["phase_seconds"]["topology"],
    )

    # ==================== 5. 构建故事树 ====================
    # 预算全书软分支总数，供 inject_soft_branches 自适应 effects 上限使用。
    # 与实际场景策略共用同一 helper，避免原著结局 0 场被错误计为 -1。
    _pre_release_quality_warnings: list[str] = []
    _total_soft_branches = estimate_total_soft_branches(
        topology,
        chapters,
        branch_type=branch_type,
    )
    _node_soft_branch_counts = estimate_soft_branch_counts(
        topology,
        chapters,
        branch_type=branch_type,
    )
    _effect_key_schedule = build_effect_key_schedule(variable_defs, _total_soft_branches)
    _effect_schedule_by_topo: dict[str, list[str]] = {}
    _schedule_offset = 0
    for _scheduled_node, _branch_count in zip(topology, _node_soft_branch_counts):
        _scheduled_topo_id = _scheduled_node.get("node_id")
        if _scheduled_topo_id:
            _effect_schedule_by_topo[_scheduled_topo_id] = _effect_key_schedule[
                _schedule_offset : _schedule_offset + _branch_count
            ]
        _schedule_offset += _branch_count
    print(f"   📊 全书软分支总数（自适应校准用）：{_total_soft_branches}")
    _calculated_effect_caps = calculate_effect_caps(variable_defs, _total_soft_branches)
    _saved_effect_caps = _existing_plan.get("effect_caps", {})
    _effect_caps = (
        {str(key): int(value) for key, value in _saved_effect_caps.items()}
        if isinstance(_saved_effect_caps, dict) and _saved_effect_caps
        else _calculated_effect_caps
    )
    _chapter_plan["effect_caps"] = _effect_caps
    _thresholds_ok, _threshold_issues = validate_threshold_reachability(
        variable_defs,
        _total_soft_branches,
        _effect_caps,
    )
    if not _thresholds_ok:
        _pre_release_quality_warnings.append(
            "玩家变量阈值可达性告警：" + "；".join(_threshold_issues)
        )
        print(
            "   ⚠️  玩家变量阈值可达性存在告警，继续生成并由最终质量评分决定："
            + "；".join(_threshold_issues)
        )
    # 原著各章平均字数（Bad End 和 What-If 都要用）
    avg_chapter_chars = sum(len(c) for c in chapters) // max(len(chapters), 1)

    # ==================== 5.0 预先抽取每个原著章节末态 ====================
    # What-If 章节生成时需要拿到「岔路口章节末态」+「兄弟章节末态」作为硬约束；
    # 结局生成时需要拿到「pivot（倒数第二章）末态」。原著章节末态在 topology 顺序里
    # 不一定是早于 What-If 节点先生成的，所以这里统一预扫一遍。
    from services.story_planner.core.novel_reader import (
        extract_terminal_states,
        summarize_ending_chapter,
    )
    print(f"\n🧬  抽取原著章节末态（防穿帮硬约束源）...")
    _terminal_scan_started = time.perf_counter()
    chapter_idx_to_states: dict = {}
    _prev_states: dict = {}
    for _i, _ch_text in enumerate(chapters):
        _ch_title = _extract_source_chapter_title(_ch_text, _i + 1)
        import json as _json
        _terminal_substep_input = {
            "chapter_sha256": content_sha256(_ch_text),
            "chapter_title": _ch_title,
            "inherited_states_digest": stable_payload_digest(_prev_states),
            "provider": provider,
            "model": DEFAULT_MODEL,
            "output_language": output_language,
            "characters": [
                c.get("name")
                for c in novel_ctx.worldview.get("characters", [])
                if isinstance(c, dict) and c.get("name")
            ],
            "life_state_schema_version": 2,
            "narrative_contract": _narrative_reality_contract,
            "narrative_pov_contract": _narrative_pov_contract,
            "lightweight": branch_type == "modular",
        }
        _states = get_reusable_chapter_substep(
            _chapter_plan,
            "terminal_states",
            _i,
            input_payload=_terminal_substep_input,
            producer_version=CHAPTER_BRANCH_STEP_VERSIONS["terminal_states"],
        )
        if _states is not None and not terminal_states_are_cacheable(_states):
            _states = None
        if _states is not None:
            _run_metrics["cache_hits"] += 1
            print(f"   ♻️  恢复第{_i+1}章末态断点")
        elif str(_i) in (
            (_chapter_plan.get("chapter_substeps", {}) or {})
            .get("terminal_states", {})
        ):
            _invalidated_terminal_chapters = invalidate_chapter_substeps_from(
                _chapter_plan,
                "terminal_states",
                _i,
            )
            print(
                f"   ♻️  第{_i+1}章末态输入/继承链变化，已失效本章及以后断点: "
                f"{_invalidated_terminal_chapters}"
            )
        _terminal_states_key = _result_cache.get_cache_key(
            _ch_text,
            {
                "stage": "terminal_states",
                "provider": provider,
                "model": DEFAULT_MODEL,
                "output_language": output_language,
                "chapter_index": _i,
                "chapter_title": _ch_title,
                "inherited_states": _json.dumps(
                    _prev_states, ensure_ascii=False, sort_keys=True
                ),
                "characters": [
                    c.get("name")
                    for c in novel_ctx.worldview.get("characters", [])
                    if isinstance(c, dict) and c.get("name")
                ],
                "cache_version": 5,
                "life_state_schema_version": 2,
                "narrative_pov_contract": _narrative_pov_contract,
                "narrative_pov_contract_version": NARRATIVE_POV_CONTRACT_VERSION,
                "producer_version": CHAPTER_BRANCH_STEP_VERSIONS["terminal_states"],
                "lightweight": branch_type == "modular",
            },
        )
        if _states is None:
            _states = _result_cache.get(_terminal_states_key)
        if _states is not None and not terminal_states_are_cacheable(_states):
            print(
                f"   ⚠️  第{_i+1}章命中低置信度/兜底末态缓存，忽略并重新抽取"
            )
            _states = None
        if _states is None:
            _run_metrics["cache_misses"] += 1
            _states = extract_terminal_states(
                chapter_text=_ch_text,
                chapter_title=_ch_title,
                provider=provider,
                worldview=novel_ctx.worldview,
                output_language=output_language,
                inherited_states=_prev_states,
                narrative_contract=_narrative_reality_contract,
                lightweight=(branch_type == "modular"),
            )
            if terminal_states_are_cacheable(_states):
                _result_cache.set(_terminal_states_key, _states)
            else:
                print(
                    f"   ⚠️  第{_i+1}章末态为低置信度兜底，本次继续运行但不写长期缓存"
                )
        else:
            _run_metrics["cache_hits"] += 1
        chapter_idx_to_states[_i] = _states
        if _states:
            _prev_states = _states
        record_chapter_substep(
            _chapter_plan,
            "terminal_states",
            _i,
            input_payload=_terminal_substep_input,
            output=_states,
            producer_version=CHAPTER_BRANCH_STEP_VERSIONS["terminal_states"],
        )
        _save_chapter_branch_plan(task_id, _chapter_plan)
        print(f"   · 第{_i+1}章末态抽取完成（{len(_states)} 个角色）")
    _run_metrics["phase_seconds"]["terminal_states"] = round(
        time.perf_counter() - _terminal_scan_started, 3
    )
    structured_log(
        _runtime_logger,
        "phase_completed",
        task_id=task_id,
        run_id=_run_id,
        stage="story_planner",
        branch_type=branch_type,
        phase="terminal_states",
        duration_seconds=_run_metrics["phase_seconds"]["terminal_states"],
    )
    update_plan_step(
        _chapter_plan,
        "terminal_states",
        status="completed",
        output_digest=stable_payload_digest(chapter_idx_to_states),
    )
    _save_chapter_branch_plan(task_id, _chapter_plan)

    tree = StoryTree(root_id="")
    # node_id_map: topo node_id -> 实际 tree node_id
    node_id_map: dict = {}
    # node_id -> terminal_states：用于 What-If 拿岔路口末态、结局拿 pivot 末态
    real_id_to_terminal_states: dict = {}
    # 整次构建保留完整按钮历史做精确去重；近义判断只读取最近 30 条。
    _choice_text_history: list[str] = []
    saved_md_ids = set()
    node_md_contents: dict[str, str] = {}
    _node_generation_started = time.perf_counter()

    for topo_node in topology:
        topo_id = topo_node["node_id"]
        node_type = topo_node.get("type", "chapter")  # chapter | ending
        is_original = topo_node.get("is_original", True)
        title = normalize_chapter_title(
            topo_node.get("title", topo_id),
            language=output_language,
        ) or untitled_chapter_title(output_language)
        # 章节序号（用于原著节点映射与确定性 ID）
        ch_idx = topo_node.get("source_chapter_idx", None)
        if is_original and isinstance(ch_idx, int) and 0 <= ch_idx < len(chapters):
            title = _extract_source_chapter_title(chapters[ch_idx], ch_idx + 1)
        _node_soft_branch_metrics = new_soft_branch_metrics()
        _optional_attempt_budget = (
            new_optional_branch_attempt_budget(
                int(os.getenv("STORY_PLANNER_OPTIONAL_BRANCH_MAX_ATTEMPTS", "3"))
            )
            if not is_original
            else None
        )

        # 确定 tree node id：相同 task_id + topo_id 重跑时保持稳定，避免孤儿 MD。
        real_id = (
            _chapter_plan.get("real_id_map", {}).get(topo_id)
            or deterministic_node_id(task_id, topo_id, ch_idx)
        )
        node_id_map[topo_id] = real_id

        # 确定父节点列表
        parent_topo_ids = topo_node.get("parent_ids", [])
        real_parent_ids = [node_id_map[p] for p in parent_topo_ids if p in node_id_map]
        parent_id_single = real_parent_ids[0] if real_parent_ids else None

        # 深度：取父节点最大深度 + 1
        depth = 1
        for pid in real_parent_ids:
            pnode = tree.get_node(pid)
            if pnode:
                depth = max(depth, pnode.depth + 1)

        # 已完成且 MD 仍完整的节点直接恢复，不重复消耗 LLM。
        _plan_entry = _chapter_plan.get("nodes", {}).get(topo_id, {})
        merge_soft_branch_metrics(
            _node_soft_branch_metrics,
            _plan_entry.get("soft_branch_metrics"),
        )

        def _omit_current_optional_branch(reason: str) -> None:
            omission = omit_optional_topology_branch(
                topology,
                topo_id,
                reason=reason,
            )
            if not omission["omitted"]:
                raise ValueError(
                    f"节点 '{topo_id}' 不是可省略的生成分支：{reason}"
                )
            omitted_node_ids = omission.get("omitted_node_ids") or [topo_id]
            omission_records = _chapter_plan.setdefault(
                "omitted_optional_branches",
                [],
            )
            omission_records[:] = [
                item
                for item in omission_records
                if not isinstance(item, dict)
                or item.get("topo_id") not in omitted_node_ids
            ]
            for omitted_topo_id in omitted_node_ids:
                omitted_real_id = (
                    _chapter_plan.get("real_id_map", {}).get(omitted_topo_id)
                    or deterministic_node_id(task_id, omitted_topo_id)
                )
                node_id_map.pop(omitted_topo_id, None)
                real_id_to_terminal_states.pop(omitted_real_id, None)
                tree.nodes.pop(omitted_real_id, None)
                saved_md_ids.discard(omitted_real_id)
                node_md_contents.pop(omitted_real_id, None)
                if tree.root_id == omitted_real_id:
                    tree.root_id = ""
                update_plan_node(
                    _chapter_plan,
                    omitted_topo_id,
                    status="omitted",
                    real_id=omitted_real_id,
                    error=omission["reason"],
                    artifact_sha256="",
                )
                mark_node_self_heal_completed(
                    _chapter_plan,
                    omitted_topo_id,
                    status="omitted",
                )
                omission_records.append(
                    {
                        "topo_id": omitted_topo_id,
                        "real_id": omitted_real_id,
                        "reason": omission["reason"],
                        "run_id": _run_id,
                        "omitted_at": _dt.now(_timezone.utc).isoformat(),
                    }
                )
            _chapter_plan["topology_result"] = topo_result
            _chapter_plan["topology_fingerprint"] = topology_fingerprint(topo_result)
            _run_metrics["optional_branches_omitted"] += len(omitted_node_ids)
            _save_chapter_branch_plan(task_id, _chapter_plan)
            stop_runtime_heartbeat(task_id, final_status="omitted")
            structured_log(
                _runtime_logger,
                "optional_branch_omitted",
                level="warning",
                task_id=task_id,
                run_id=_run_id,
                stage="story_planner",
                branch_type=branch_type,
                topo_id=topo_id,
                real_id=real_id,
                reason=omission["reason"],
            )
            print(
                f"   🗑️  可选 What-If 节点 {topo_id} 重试后仍不合规，"
                f"已省略并保留主线：{omission['reason']}"
            )

        if topo_node.get("omitted_branch"):
            node_id_map.pop(topo_id, None)
            if _plan_entry.get("status") != "omitted":
                update_plan_node(
                    _chapter_plan,
                    topo_id,
                    status="omitted",
                    real_id=real_id,
                    error=topo_node.get("omission_reason") or "optional_branch_failed",
                )
                _save_chapter_branch_plan(task_id, _chapter_plan)
            print(f"   ♻️  跳过已省略的可选 What-If 节点：{topo_id}")
            continue

        def _node_with_consistency_directive(candidate_node: dict) -> dict:
            retry_reason = get_node_regeneration_reason(_plan_entry)
            if not retry_reason:
                return candidate_node
            effective_node = dict(candidate_node)
            effective_node["summary"] = (
                str(candidate_node.get("summary") or "")
                + "\n【上轮节点/发布门禁定向修复要求】"
                + retry_reason
                + "。必须在相关场景中补齐完整因果链，不得只在结尾补一句解释。"
            )
            return effective_node

        def _generated_pov_source(content: str, *, original_node: bool) -> str:
            """Return only generated prose for POV checks.

            Original chapter text is immutable.  For an original node, inspect
            only the injected soft-branch theatres; never feed the source
            chapter to a repairer.  Generated What-If/ending text is safe to
            inspect as a whole.
            """
            value = str(content or "")
            if not original_node:
                return value
            blocks = re.findall(
                r"◆◆◆\s*软分支(?:开始|选择)\s*◆◆◆.*?"
                r"◆◆◆\s*软分支结束\s*◆◆◆",
                value,
                flags=re.DOTALL,
            )
            return "\n\n".join(blocks)

        def _pov_gate_repair_text(
            content: str,
            *,
            title: str,
            attempt_budget: dict[str, Any] | None = None,
        ) -> tuple[str, list[str], bool]:
            """Check generated prose and perform at most one targeted repair."""
            source = _generated_pov_source(content, original_node=False)
            issues = detect_narrative_pov_drift(source, _narrative_pov_contract)
            if not issues:
                return str(content or ""), [], False
            pov_rule = str(
                (_narrative_pov_contract or {}).get("rule_text") or ""
            ).strip()
            perspective = str(
                (_narrative_pov_contract or {}).get("perspective") or "unknown"
            )
            protagonist = str(
                (_narrative_pov_contract or {}).get("protagonist_name") or "主角"
            )
            if attempt_budget is not None:
                budget_result = consume_optional_branch_attempt(
                    attempt_budget,
                    phase="pov_repair",
                )
                if not budget_result["allowed"]:
                    return str(content or ""), issues, False
            try:
                from services.story_planner.core.llm_client import LLMClient

                client = LLMClient(provider=provider)
                repaired = client.chat_completion(
                    system_prompt=(
                        "你是视觉小说正文叙事人称修复器。只修正叙述视角，不改剧情事实、"
                        "人物动作、时间地点、对白和段落结构；输出修复后的完整正文，不要解释。\n"
                        f"叙事契约：perspective={perspective}；主角={protagonist}。\n"
                        f"{pov_rule}\n"
                        "对白、书信、日记中的人称必须原样保留；不要修改选择按钮或控制标记。"
                    ),
                    user_prompt=(
                        f"节点：{title}\n"
                        f"检测到的问题：{json.dumps(issues, ensure_ascii=False)}\n"
                        "请仅把正文旁白改回契约要求的人称，保持原文信息完整。\n\n"
                        f"待修复正文：\n{str(content or '').strip()}"
                    ),
                    temperature=0.1,
                    max_tokens=max(1200, min(12000, len(str(content or "")) // 2 + 800)),
                ).strip()
            except Exception as exc:
                print(f"   ⚠️  {title} POV 定向修复调用失败：{exc}")
                return str(content or ""), issues, False
            if not repaired:
                return str(content or ""), issues, False
            remaining = detect_narrative_pov_drift(
                repaired,
                _narrative_pov_contract,
            )
            if remaining:
                print(
                    f"   ⚠️  {title} POV 定向修复后仍漂移："
                    + "；".join(remaining[:4])
                )
                return str(content or ""), remaining, False
            print(f"   ✅ {title} POV 定向修复完成（仅 1 次）")
            return repaired, [], True

        def _check_original_injected_pov(content: str, *, title: str) -> str:
            """Keep malformed optional theatres and expose POV drift as warning."""
            source = _generated_pov_source(content, original_node=True)
            issues = detect_narrative_pov_drift(source, _narrative_pov_contract)
            if not issues:
                return content
            warning = (
                "注入软分支叙事视角不一致（定向修复后仍存在）："
                + "；".join(issues[:4])
            )
            topo_node.setdefault("validation_warnings", []).append(warning)
            topo_node["validation_status"] = "warning"
            _plan_entry.setdefault("validation_warnings", []).append(warning)
            _save_chapter_branch_plan(task_id, _chapter_plan)
            print(
                f"   ⚠️  {title} {warning}，保留软分支并记录 warning"
            )
            return content

        def _injected_pov_issues(content: str) -> list[str]:
            """Check only soft-branch prose embedded in an already-generated node."""
            return detect_narrative_pov_drift(
                _generated_pov_source(content, original_node=True),
                _narrative_pov_contract,
            )

        def _schedule_optional_branch_self_heal(reason: str) -> str:
            # Optional What-If/Bad-End failures stay local.  Do not call the
            # generic node self-heal scheduler: it raises NodeSelfHealRequired
            # and re-enters the whole chapter_branch pipeline, invalidating
            # unrelated terminal-state checkpoints.  The generator already
            # owns its bounded local retries; this record is for diagnostics.
            optional_result = record_optional_branch_failure(
                _chapter_plan,
                topo_id,
                reason,
                max_attempts=int(
                    os.getenv("STORY_PLANNER_OPTIONAL_BRANCH_MAX_ATTEMPTS", "3")
                ),
            )
            _save_chapter_branch_plan(task_id, _chapter_plan)
            if optional_result["exhausted"]:
                print(
                    f"   ⚠️  可选分支 {topo_id} 局部重试已耗尽，等待质量分级（"
                    f"attempt={optional_result['attempt']}）：{reason}"
                )
            else:
                print(
                    f"   ⚠️  可选分支 {topo_id} 记录失败（"
                    f"attempt={optional_result['attempt']}），不触发节点自愈：{reason}"
                )
            return str(reason or "optional_branch_failed")

        def _record_optional_quality_warning(
            reason: str,
            *,
            content: str = "",
            category: str | None = None,
            review: dict | None = None,
        ) -> bool:
            """Persist a non-fatal optional-branch quality finding."""
            if classify_whatif_validation_issue(
                reason,
                content=content,
                category=category,
                review=review,
            ) != "warning":
                return False
            warning = str(reason or "可选分支质量问题").strip()
            warnings = topo_node.setdefault("validation_warnings", [])
            if warning not in warnings:
                warnings.append(warning)
            topo_node["validation_status"] = "warning"
            plan_warnings = _plan_entry.setdefault("validation_warnings", [])
            if warning not in plan_warnings:
                plan_warnings.append(warning)
            _save_chapter_branch_plan(task_id, _chapter_plan)
            return True

        def _schedule_consistency_self_heal(
            exc: ContentConsistencyRepairRequired,
            *,
            allow_optional_omission: bool = False,
        ) -> str:
            if exc.content:
                candidate_record = _save_owned_chapter_branch_md(
                    task_id=task_id,
                    name=f"{real_id}.consistency-candidate",
                    content=f"# {title}\n\n{exc.content}\n",
                    overwrite=True,
                )
                if candidate_record is None:
                    print(f"   ⚠️  节点 {topo_id} 一致性候选正文保存失败")
            consistency_reason = str(exc.issues or exc)
            _plan_entry.setdefault("consistency_repair_history", []).append(
                {
                    "reason": consistency_reason,
                    "run_id": _run_id,
                    "recorded_at": _dt.now(_timezone.utc).isoformat(),
                }
            )
            node_label = "结局节点" if node_type == "ending" else "生成节点"
            if allow_optional_omission:
                return _schedule_optional_branch_self_heal(consistency_reason)
            heal_result = schedule_node_self_heal(
                _chapter_plan,
                topo_id,
                consistency_reason,
                max_attempts=int(
                    os.getenv(
                        "STORY_PLANNER_NODE_SELF_HEAL_MAX_ATTEMPTS",
                        "3",
                    )
                ),
            )
            _save_chapter_branch_plan(task_id, _chapter_plan)
            if heal_result["scheduled"]:
                raise NodeSelfHealRequired(
                    f"{node_label} '{topo_id}' 一致性自愈已安排："
                    f"attempt={heal_result['attempt']}，{consistency_reason}"
                ) from exc
            raise ValueError(
                f"{node_label}一致性门禁失败：{consistency_reason}"
            ) from exc

        def _run_optional_generator(
            generator,
            *,
            label: str,
            attempt_budget: dict[str, Any] | None = None,
            retain_quality_candidate: bool = False,
        ):
            """Run one disposable branch locally with a bounded retry budget."""
            limit = max(
                1,
                int(os.getenv("STORY_PLANNER_OPTIONAL_BRANCH_MAX_ATTEMPTS", "3")),
            )
            last_reason = "optional_branch_failed"
            last_usable_content = ""
            for attempt in range(1, limit + 1):
                if attempt_budget is not None:
                    budget_result = consume_optional_branch_attempt(
                        attempt_budget,
                        phase="generate",
                    )
                    if not budget_result["allowed"]:
                        return None, "optional branch attempt budget exhausted"
                reset_scene_checkpoint = False
                try:
                    return generator(), ""
                except ContentConsistencyRepairRequired as exc:
                    reset_scene_checkpoint = True
                    if str(exc.content or "").strip():
                        last_usable_content = str(exc.content).strip()
                    last_reason = _schedule_consistency_self_heal(
                        exc,
                        allow_optional_omission=True,
                    )
                except WhatIfContentBudgetError as exc:
                    reset_scene_checkpoint = True
                    if str(getattr(exc, "content", "") or "").strip():
                        last_usable_content = str(exc.content).strip()
                    last_reason = _schedule_optional_branch_self_heal(str(exc))
                except StageLeaseLostError:
                    raise
                except Exception as exc:
                    last_reason = _schedule_optional_branch_self_heal(
                        f"{label}生成异常：{exc}"
                    )
                if attempt < limit:
                    if reset_scene_checkpoint:
                        # A completed checkpoint that failed consistency or
                        # budget validation would otherwise be replayed
                        # verbatim on the next local attempt.  Reset only this
                        # optional branch; original terminal-state checkpoints
                        # remain untouched.
                        _plan_entry["whatif_scene_checkpoint"] = {}
                        _save_chapter_branch_plan(task_id, _chapter_plan)
                    print(
                        f"   🔁 {label}局部重试 attempt={attempt + 1}/{limit}："
                        f"{last_reason}"
                    )
                    continue
                if retain_quality_candidate and last_usable_content:
                    if _record_optional_quality_warning(
                        last_reason,
                        content=last_usable_content,
                        category=(
                            "underlong_repair"
                            if "长度" in str(last_reason or "")
                            else None
                        ),
                    ):
                        print(
                            f"   ⚠️  {label}局部重试已耗尽，保留最后可运行正文并记录 warning："
                            f"{last_reason}"
                        )
                        return last_usable_content, ""
                return None, last_reason
            return None, last_reason

        def _persist_whatif_scene(scene_state, _topo_id=topo_id):
            _entry = _chapter_plan.setdefault("nodes", {}).setdefault(_topo_id, {})
            _entry["whatif_scene_checkpoint"] = scene_state
            _save_chapter_branch_plan(task_id, _chapter_plan)

        def _persist_soft_branch_gap_state(gap_state, _topo_id=topo_id):
            _entry = _chapter_plan.setdefault("nodes", {}).setdefault(_topo_id, {})
            _entry["soft_branch_gap_checkpoint"] = gap_state
            _entry["soft_branch_metrics"] = _node_soft_branch_metrics
            _save_chapter_branch_plan(task_id, _chapter_plan)
        _resume_md_record = None
        _resume_md_content = None
        if _plan_entry.get("status") in {"completed", "persisting"}:
            _candidate_md = get_story_md_by_name_strict(task_id, real_id)
            _candidate_content = getattr(_candidate_md, "data", "") if _candidate_md else ""
            _candidate_ok = can_resume_node_checkpoint(
                _plan_entry,
                _candidate_content,
            )
            if _candidate_ok and not is_original:
                _candidate_ok, _ = validate_generated_content(
                    _candidate_content,
                    min_chars=max(300, min(1200, avg_chapter_chars // 3)),
                )
            if _candidate_ok:
                _cached_pov_source = _generated_pov_source(
                    _candidate_content,
                    original_node=is_original,
                )
                _cached_pov_issues = detect_narrative_pov_drift(
                    _cached_pov_source,
                    _narrative_pov_contract,
                )
                if _cached_pov_issues:
                    _candidate_ok = False
                    print(
                        f"   ⚠️  已保存节点 POV 契约不匹配，忽略断点并重新生成："
                        f"topo_id={topo_id}，"
                        + "；".join(_cached_pov_issues[:3])
                    )
            if _candidate_ok:
                _resume_md_record = _candidate_md
                _resume_md_content = _candidate_content
                remember_choices_from_markdown(
                    _choice_text_history,
                    _candidate_content,
                )
                _saved_states = _plan_entry.get("terminal_states", {})
                if _saved_states and not terminal_states_have_life_schema(
                    _saved_states
                ):
                    print(
                        f"   ♻️  已保存节点使用旧生命状态结构，重新抽取：topo_id={topo_id}"
                    )
                    _saved_states = {}
                if not _saved_states and is_original and ch_idx in chapter_idx_to_states:
                    _saved_states = chapter_idx_to_states[ch_idx]
                    print(f"   ♻️  恢复原著节点末态：topo_id={topo_id}")
                elif not _saved_states and not is_original and node_type == "chapter":
                    _inherited_resume_states = {}
                    for _resume_parent_id in real_parent_ids:
                        if real_id_to_terminal_states.get(_resume_parent_id):
                            _inherited_resume_states = real_id_to_terminal_states[_resume_parent_id]
                            break
                    try:
                        _saved_states = extract_terminal_states(
                            chapter_text=strip_soft_branch_blocks(_candidate_content),
                            chapter_title=title,
                            provider=provider,
                            worldview=novel_ctx.worldview,
                            output_language=output_language,
                            inherited_states=_inherited_resume_states,
                            narrative_contract=_narrative_reality_contract,
                        )
                        print(f"   ♻️  从已保存正文恢复 What-If 末态：topo_id={topo_id}")
                    except Exception as _resume_state_error:
                        print(
                            f"   ⚠️  已保存节点末态恢复失败：topo_id={topo_id}, "
                            f"error={_resume_state_error}"
                        )
                        _saved_states = {}
                if _saved_states:
                    real_id_to_terminal_states[real_id] = _saved_states
                if _plan_entry.get("summary"):
                    topo_node["summary"] = _plan_entry["summary"]
                _run_metrics["nodes_resumed"] += 1
                merge_soft_branch_metrics(
                    _run_metrics["soft_branches"],
                    _plan_entry.get("soft_branch_metrics", {}),
                )
                print(
                    f"   ♻️  恢复已完成节点：topo_id={topo_id}, real_id={real_id}, "
                    f"checkpoint={_plan_entry.get('status')}"
                )
                mark_node_self_heal_completed(_chapter_plan, topo_id)

        if _resume_md_content is None:
            update_plan_node(
                _chapter_plan,
                topo_id,
                status="running",
                real_id=real_id,
            )
            _save_chapter_branch_plan(task_id, _chapter_plan)
            start_runtime_heartbeat(
                task_id=task_id,
                run_id=_run_id,
                branch_type=branch_type,
                topo_id=topo_id,
                real_id=real_id,
                interval_seconds=RUNTIME_SETTINGS.heartbeat_interval_seconds,
                plan_updater=lambda payload: _record_chapter_branch_heartbeat(
                    task_id, payload
                ),
                lease_renewer=renew_stage_lease,
                logger=_runtime_logger,
            )
            structured_log(
                _runtime_logger,
                "node_started",
                task_id=task_id,
                run_id=_run_id,
                stage="story_planner",
                branch_type=branch_type,
                topo_id=topo_id,
                real_id=real_id,
                node_type=node_type,
                attempt=_chapter_plan.get("nodes", {}).get(topo_id, {}).get("attempts", 0),
            )
        else:
            structured_log(
                _runtime_logger,
                "node_resumed",
                task_id=task_id,
                run_id=_run_id,
                stage="story_planner",
                branch_type=branch_type,
                topo_id=topo_id,
                real_id=real_id,
            )

        # ---- 生成章节内容 ----
        if _resume_md_content is not None:
            md_content = _resume_md_content
        elif node_type == "ending" and is_original:
            # 原著末章：直接使用原文，不注入软分支（结局是终点，不需要积累数值）
            ch_text = chapters[ch_idx] if ch_idx is not None and ch_idx < len(chapters) else ""
            print(f"   📖 原著结局直接采用原文（跳过软分支注入）：{title}")
            md_content = f"# {title}\n\n{ch_text.strip()}\n"
            # 原著结局也写末态（即末章末态），便于前端展示与下游调试
            if ch_idx is not None and ch_idx in chapter_idx_to_states:
                real_id_to_terminal_states[real_id] = chapter_idx_to_states[ch_idx]
        elif node_type == "ending" and not is_original:
            # 平行结局：LLM 生成
            # pivot 必须是本结局的直接父节点（命运终点岔口/倒数第二章），
            # 不能反向遍历 topology 找 type=="chapter"——那样会跳过 branch_point，
            # 落到再上一章，结局正文就会重复倒数第二章的开场。
            pivot_text = ""
            pivot_label = "倒数第二章"
            _ending_pivot_index = None
            for pid in parent_topo_ids:
                ptopo = next((t for t in topology if t["node_id"] == pid), None)
                if ptopo and ptopo.get("is_original"):
                    pidx = ptopo.get("source_chapter_idx")
                    if pidx is not None and pidx < len(chapters):
                        pivot_text = chapters[pidx]
                        pivot_label = ptopo.get("title", f"父节点 {pid}")
                        _ending_pivot_index = pidx
                        break
            if not pivot_text and len(chapters) >= 2:
                pivot_text = chapters[-2]
                _ending_pivot_index = len(chapters) - 2
            print(f"\n✍️  生成结局节点：{title}（pivot={pivot_label}）")
            # 原著末章字数作为目标，平行结局长度对齐
            orig_ending_chars = len(chapters[-1]) if chapters else 8000
            # 找原著末章 summary 作为防撞车锚点
            orig_ending_summary = ""
            for t in topology:
                if t.get("is_original") and t.get("type") in ("ending", "chapter"):
                    sidx = t.get("source_chapter_idx")
                    if sidx is not None and sidx == len(chapters) - 1:
                        orig_ending_summary = t.get("summary", "")
                        break
            # 找本结局对应的触发变量信息（带完整 variable_defs 描述）
            ending_trigger = ""
            bv = topo_node.get("branch_variable", "")
            bt = topo_node.get("branch_threshold", "")
            if bv and bt:
                ending_trigger = f"{bv} >= {bt}"
                if variable_defs:
                    vdef = next((v for v in variable_defs if v.get("key") == bv), None)
                    if vdef:
                        ending_trigger += (
                            f"（{vdef.get('name', bv)}维度：{vdef.get('description', '')}；"
                            f"高分状态：{vdef.get('high_desc', '')}）"
                        )
            # 收集其他平行结局的 summary，供横向对比防撞车
            sibling_ending_summaries = [
                t.get("summary", "") for t in topology
                if t.get("type") == "ending" and not t.get("is_original")
                and t["node_id"] != topo_id and t.get("summary")
            ]
            # pivot 末角色状态：进入本结局时的事实基础，硬约束
            _pivot_states = {}
            for pid in parent_topo_ids:
                ptopo = next((t for t in topology if t["node_id"] == pid), None)
                if not ptopo:
                    continue
                if ptopo.get("is_original"):
                    pidx = ptopo.get("source_chapter_idx")
                    if pidx is not None and pidx in chapter_idx_to_states:
                        _pivot_states = chapter_idx_to_states[pidx]
                        break
                else:
                    # What-If 父节点：从 real_id_to_terminal_states 取
                    p_real = node_id_map.get(pid)
                    if p_real and p_real in real_id_to_terminal_states:
                        _pivot_states = real_id_to_terminal_states[p_real]
                        break
            def _generate_ending_candidate(candidate_node: dict) -> str:
                _effective_node = _node_with_consistency_directive(candidate_node)
                return generate_ending_content(
                    node=_effective_node,
                    pivot_chapter_text=pivot_text,
                    style_ref=style_ref,
                    provider=provider,
                    novel_context=novel_ctx,
                    target_chars=orig_ending_chars,
                    chapter_summaries=summaries,
                    original_sibling_summary=orig_ending_summary,
                    trigger_variable_info=ending_trigger,
                    output_language=output_language,
                    sibling_endings=sibling_ending_summaries,
                    pivot_terminal_states=_pivot_states,
                    pivot_chapter_index=_ending_pivot_index,
                    ui_mode=story_ui_mode,
                    narrative_pov_contract=_narrative_pov_contract,
                )

            # Parallel endings are optional generated branches.  Consistency
            # failures consume the same three-attempt local budget as What-If
            # generation and never trigger an outer chapter-branch restart.
            content, _ending_omit_reason = _run_optional_generator(
                lambda: _generate_ending_candidate(topo_node),
                label="结局 ",
                attempt_budget=_optional_attempt_budget,
                retain_quality_candidate=True,
            )
            if _ending_omit_reason:
                _omit_current_optional_branch(_ending_omit_reason)
                continue
            _ending_variable_def = next(
                (
                    item for item in (variable_defs or [])
                    if isinstance(item, dict) and item.get("key") == bv
                ),
                {},
            )
            _causality_ok, _causality_reason = validate_ending_variable_causality(
                _ending_variable_def,
                content,
                semantic_reviewer=_review_ending_causality,
                semantic_context={
                    "ending_id": real_id,
                    "topology_node_id": topo_id,
                    "title": title,
                    "summary": topo_node.get("summary", ""),
                    "plan_direction": topo_node.get("plan_direction", ""),
                },
            )
            if not _causality_ok:
                print(
                    "   🩹 结局变量因果不一致，携带硬约束重新生成一次："
                    + _causality_reason
                )
                _retry_node = dict(topo_node)
                _retry_node["summary"] = (
                    str(topo_node.get("summary") or "")
                    + "\n【因果修复硬约束】"
                    + _causality_reason
                    + "；结局核心转折必须由该高分人格轴对应的主角行动直接造成。"
                )
                if remaining_optional_branch_attempts(_optional_attempt_budget) > 0:
                    _causality_candidate, _causality_retry_reason = (
                        _run_optional_generator(
                            lambda: _generate_ending_candidate(_retry_node),
                            label="结局因果修复 ",
                            attempt_budget=_optional_attempt_budget,
                            retain_quality_candidate=True,
                        )
                    )
                    if _causality_retry_reason:
                        _pre_release_quality_warnings.append(
                            f"结局 {topo_id} 因果定向修复告警：{_causality_retry_reason}"
                        )
                    else:
                        content = _causality_candidate
                    _causality_ok, _causality_reason = validate_ending_variable_causality(
                        _ending_variable_def,
                        content,
                        semantic_reviewer=_review_ending_causality,
                        semantic_context={
                            "ending_id": real_id,
                            "topology_node_id": topo_id,
                            "title": title,
                            "summary": _retry_node.get("summary", ""),
                            "plan_direction": _retry_node.get("plan_direction", ""),
                        },
                    )
                else:
                    _pre_release_quality_warnings.append(
                        f"结局 {topo_id} 因果修复预算已耗尽：{_causality_reason}"
                    )
            # Causality retry can replace the complete ending, so run the POV
            # gate only after the final candidate is selected.  The helper
            # performs at most one targeted repair for this node.
            content, _ending_pov_issues, _ending_pov_repaired = _pov_gate_repair_text(
                content,
                title=title,
                attempt_budget=_optional_attempt_budget,
            )
            if _ending_pov_issues:
                _pov_reason = (
                    "结局正文叙事视角不一致且单次定向修复后仍未通过："
                    + "；".join(_ending_pov_issues[:4])
                )
                _record_optional_quality_warning(
                    _pov_reason,
                    content=content,
                )
                print(
                    f"   ⚠️  {title} {_pov_reason}，保留结局正文并记录 warning"
                )
            topo_node["ending_variable_causality"] = {
                "passed": _causality_ok,
                "reason": _causality_reason,
            }
            topo_node["legacy_flags"] = infer_ending_legacy_flags(
                str(topo_node.get("branch_variable") or ""),
                content,
                ui_mode=story_ui_mode,
            )
            md_content = f"# {title}\n\n{content}\n"
            # 结局正文摘要回写：覆盖 topo_node["summary"]（实摘要），原走向方向另存 plan_direction
            try:
                _ending_real_summary = summarize_ending_chapter(
                    ending_text=content,
                    ending_title=title,
                    provider=provider,
                    worldview=novel_ctx.worldview,
                    output_language=output_language,
                )
                if _ending_real_summary:
                    topo_node["plan_direction"] = topo_node.get("summary", "")
                    topo_node["summary"] = _ending_real_summary
            except Exception as _e:
                print(f"   ⚠️  结局摘要写回失败：{_e}")
            # 结局末态：抽取本结局正文末状态（虽然结局没下游，但便于调试 + 前端展示）
            try:
                _ending_states = extract_terminal_states(
                    chapter_text=content,
                    chapter_title=title,
                    provider=provider,
                    worldview=novel_ctx.worldview,
                    output_language=output_language,
                    inherited_states=_pivot_states,
                    narrative_contract=_narrative_reality_contract,
                )
            except Exception as _e:
                print(f"   ⚠️  结局末态抽取失败：{_e}")
                _ending_states = {}
            real_id_to_terminal_states[real_id] = _ending_states
        elif not is_original:
            # ── death_corridor Bad End 节点（is_bad_ending=True）──
            if topo_node.get("is_bad_ending"):
                print(f"\n💀  生成 Bad End 节点：{title}（death_corridor）...")
                _be_prev_text = ""
                _be_prev_summary = ""
                _be_pivot_index = None
                for pid in parent_topo_ids:
                    for t in topology:
                        if t["node_id"] == pid and t.get("is_original"):
                            pidx = t.get("source_chapter_idx")
                            if pidx is not None and pidx < len(chapters):
                                _be_prev_text = chapters[pidx]
                                _be_prev_summary = summaries[pidx] if pidx < len(summaries) else ""
                                _be_pivot_index = pidx
                            break
                _be_parent_states = {}
                for pid in parent_topo_ids:
                    ptopo = next((t for t in topology if t["node_id"] == pid), None)
                    if not ptopo:
                        continue
                    if ptopo.get("is_original"):
                        pidx = ptopo.get("source_chapter_idx")
                        if pidx is not None and pidx in chapter_idx_to_states:
                            _be_parent_states = chapter_idx_to_states[pidx]
                            break
                    else:
                        p_real = node_id_map.get(pid)
                        if p_real and p_real in real_id_to_terminal_states:
                            _be_parent_states = real_id_to_terminal_states[p_real]
                            break
                def _generate_bad_end_candidate():
                    return generate_whatif_chapter(
                        node=_node_with_consistency_directive(topo_node),
                        style_ref=style_ref,
                        pivot_chapter_text=_be_prev_text,
                        provider=provider,
                        pivot_summary=_be_prev_summary,
                        novel_context=novel_ctx,
                        output_language=output_language,
                        target_chars=min(avg_chapter_chars, 3500),
                        parent_terminal_states=_be_parent_states,
                        bad_ending_mode=True,
                        behavior_anchor_chapter_index=_be_pivot_index,
                        checkpoint_callback=_persist_whatif_scene,
                        resume_state=_plan_entry.get("whatif_scene_checkpoint"),
                        narrative_pov_contract=_narrative_pov_contract,
                    )
                content, _whatif_omit_reason = _run_optional_generator(
                    _generate_bad_end_candidate,
                    label="Bad End ",
                    attempt_budget=_optional_attempt_budget,
                    retain_quality_candidate=True,
                )
                if _whatif_omit_reason:
                    _omit_current_optional_branch(_whatif_omit_reason)
                    continue
                content, _pov_issues, _pov_repaired = _pov_gate_repair_text(
                    content,
                    title=title,
                    attempt_budget=_optional_attempt_budget,
                )
                if _pov_issues:
                    _bad_end_pov_reason = (
                        "POV 叙事视角不一致且单次定向修复后仍未通过："
                        + "；".join(_pov_issues[:4])
                    )
                    _record_optional_quality_warning(
                        _bad_end_pov_reason,
                        content=content,
                    )
                    print(
                        f"   ⚠️  {title} {_bad_end_pov_reason}，保留正文并记录 warning"
                    )
                real_id_to_terminal_states[real_id] = _be_parent_states
                md_content = f"# {title}\n\n{content}\n"
            else:
            # What-If 章节：LLM 全新生成
                print(f"\n✍️  生成 What-If 章节：{title}...")
            # 找直接父原著章节作为上文，同时带上该章摘要
                prev_original_text = ""
                prev_original_summary = ""
                for pid in parent_topo_ids:
                    for t in topology:
                        if t["node_id"] == pid and t.get("is_original"):
                            pidx = t.get("source_chapter_idx")
                            if pidx is not None and pidx < len(chapters):
                                prev_original_text = chapters[pidx]
                                prev_original_summary = summaries[pidx] if pidx < len(summaries) else ""
                            break
                # 找收束章节全文 + 摘要（What-If next 中的第一个原著节点 = 第 N+2 章）
                _merge_window = resolve_whatif_merge_window(topo_node, topology)
                convergence_opening = ""
                convergence_summary = ""
                for nxt_id in topo_node.get("next", []):
                    for t in topology:
                        if t["node_id"] == nxt_id and t.get("is_original"):
                            cidx = t.get("source_chapter_idx")
                            if cidx is not None and cidx < len(chapters):
                                convergence_opening = chapters[cidx]
                                convergence_summary = summaries[cidx] if cidx < len(summaries) else ""
                            break
                    if convergence_opening:
                        break

                # 找平级原著兄弟章节（What-If 实际"替代"的那一章 = 第 N+1 章）：
                # pivot 父节点 next 中除自己之外的 is_original 章节。
                # 这一章的全文 + 摘要必须给 LLM 看，否则它不知道在替换什么剧情，会乱写。
                sibling_chapter_text = ""
                sibling_chapter_summary = ""
                sibling_title = ""
                for pid in parent_topo_ids:
                    pivot_topo = next((t for t in topology if t["node_id"] == pid), None)
                    if not pivot_topo:
                        continue
                    for sib_id in pivot_topo.get("next", []):
                        if sib_id == topo_node["node_id"]:
                            continue
                        sib_topo = next((t for t in topology if t["node_id"] == sib_id), None)
                        if sib_topo and sib_topo.get("is_original"):
                            sidx = sib_topo.get("source_chapter_idx")
                            if sidx is not None and sidx < len(chapters):
                                sibling_chapter_text = chapters[sidx]
                                sibling_chapter_summary = summaries[sidx] if sidx < len(summaries) else ""
                                sibling_title = sib_topo.get("title", f"第{sidx+1}章")
                            break
                    if sibling_chapter_text:
                        break

                # 延迟收束时，What-If 替代的不止一个原著章节。把整个被替代窗口交给
                # 生成器，末态则对齐收束目标前一章，而不是只对齐最初兄弟章。
                _replaced_indices = _merge_window.get(
                    "replaced_source_chapter_indices", []
                )
                if _replaced_indices:
                    sibling_chapter_text = "\n\n".join(
                        f"【原著第{idx + 1}章】\n{chapters[idx]}"
                        for idx in _replaced_indices
                        if 0 <= idx < len(chapters)
                    )
                    sibling_chapter_summary = "\n".join(
                        f"第{idx + 1}章：{summaries[idx]}"
                        for idx in _replaced_indices
                        if 0 <= idx < len(summaries)
                    )
                    if len(_replaced_indices) > 1:
                        sibling_title = (
                            f"原著第{_replaced_indices[0] + 1}-"
                            f"{_replaced_indices[-1] + 1}章"
                        )

                # 防撞车锚点：原著兄弟章节摘要（What-If 必须与它走向截然不同）
                orig_sibling_summary = sibling_chapter_summary

                # 收束章节开头情绪锚点（主角状态词）
                convergence_tone = ""
                if convergence_opening:
                    for _para in convergence_opening.split('\n\n'):
                        if _para.strip():
                            convergence_tone = _para.strip()[:80]
                            break
                # 触发此 What-If 的变量条件：拼上变量完整描述，让 LLM 理解"积累了什么"
                whatif_trigger = ""
                bv = topo_node.get("branch_variable", "")
                bt = topo_node.get("branch_threshold", "")
                if bv and bt:
                    whatif_trigger = f"{bv} >= {bt}"
                    # 从 variable_defs 找完整描述
                    if variable_defs:
                        vdef = next((v for v in variable_defs if v.get("key") == bv), None)
                        if vdef:
                            whatif_trigger += (
                                f"（{vdef.get('name', bv)}维度：{vdef.get('description', '')}；"
                                f"高分状态：{vdef.get('high_desc', '')}）"
                            )
                # What-If 起点状态：岔路口章节末态。优先取 What-If 的直接父节点（pivot 章）。
                _whatif_parent_states = {}
                for pid in parent_topo_ids:
                    ptopo = next((t for t in topology if t["node_id"] == pid), None)
                    if not ptopo:
                        continue
                    if ptopo.get("is_original"):
                        pidx = ptopo.get("source_chapter_idx")
                        if pidx is not None and pidx in chapter_idx_to_states:
                            _whatif_parent_states = chapter_idx_to_states[pidx]
                            break
                    else:
                        p_real = node_id_map.get(pid)
                        if p_real and p_real in real_id_to_terminal_states:
                            _whatif_parent_states = real_id_to_terminal_states[p_real]
                            break
                # What-If 收束锚点末态：延迟收束时取目标章前一原著章末态。
                _whatif_sibling_states = {}
                _whatif_budget = plan_whatif_budget(avg_chapter_chars)
                _whatif_budget_min = min(8000, _whatif_budget["target_chars"])
                _whatif_accepted_min = (
                    _whatif_budget_min * 85 + 99
                ) // 100
                _state_anchor_idx = _merge_window.get(
                    "state_anchor_source_chapter_idx"
                )
                if _state_anchor_idx in chapter_idx_to_states:
                    _whatif_sibling_states = chapter_idx_to_states[_state_anchor_idx]

                def _generate_whatif_candidate():
                    candidate = generate_whatif_chapter(
                        node=_node_with_consistency_directive(topo_node),
                        style_ref=style_ref,
                        pivot_chapter_text=prev_original_text,
                        provider=provider,
                        pivot_summary=prev_original_summary,
                        novel_context=novel_ctx,
                        convergence_opening=convergence_opening,
                        convergence_summary=convergence_summary,
                        convergence_tone=convergence_tone,
                        original_sibling_summary=orig_sibling_summary,
                        sibling_chapter_text=sibling_chapter_text,
                        sibling_chapter_title=sibling_title,
                        trigger_variable_info=whatif_trigger,
                        chapter_summaries=summaries,
                        output_language=output_language,
                        target_chars=_whatif_budget["target_chars"],
                        target_chars_already_budgeted=True,
                        variable_defs=variable_defs,
                        parent_terminal_states=_whatif_parent_states,
                        sibling_terminal_states=_whatif_sibling_states,
                        behavior_anchor_chapter_index=(
                            next(
                                (
                                    t.get("source_chapter_idx")
                                    for t in topology
                                    if t.get("node_id") in parent_topo_ids
                                    and t.get("is_original")
                                    and t.get("source_chapter_idx") is not None
                                ),
                                None,
                            )
                        ),
                        checkpoint_callback=_persist_whatif_scene,
                        resume_state=_plan_entry.get("whatif_scene_checkpoint"),
                        narrative_pov_contract=_narrative_pov_contract,
                    )
                    return enforce_whatif_content_budget(
                        candidate,
                        min_chars=_whatif_budget_min,
                        max_chars=_whatif_budget["target_chars"],
                        preserve_ending=bool(convergence_opening.strip()),
                    )
                content, _whatif_omit_reason = _run_optional_generator(
                    _generate_whatif_candidate,
                    label="What-If ",
                    attempt_budget=_optional_attempt_budget,
                    retain_quality_candidate=True,
                )
                if _whatif_omit_reason:
                    _omit_current_optional_branch(_whatif_omit_reason)
                    continue
                # What-If 末态抽取：作为下游节点（如 W-I 链 / 收束章）的硬约束源
                try:
                    _whatif_states = extract_terminal_states(
                        chapter_text=content,
                        chapter_title=title,
                        provider=provider,
                        worldview=novel_ctx.worldview,
                        output_language=output_language,
                        inherited_states=_whatif_parent_states,
                        narrative_contract=_narrative_reality_contract,
                    )
                except Exception as _e:
                    print(f"   ⚠️  What-If 末态抽取失败：{_e}")
                    _whatif_states = {}
                real_id_to_terminal_states[real_id] = _whatif_states
                if _whatif_sibling_states:
                    _convergence_result = evaluate_convergence_states(
                        _whatif_states,
                        _whatif_sibling_states,
                    )
                    _can_converge = bool(_convergence_result["compatible"])
                    _convergence_hard_issues = list(
                        _convergence_result.get("hard_issues") or []
                    )
                    _convergence_warnings = list(
                        _convergence_result.get("warnings") or []
                    )
                    _convergence_transition_issues = list(
                        _convergence_result.get("bridge_requirements") or []
                    )
                    _convergence_issues = list(_convergence_hard_issues)
                    topo_node["convergence_hard_issues"] = list(
                        _convergence_hard_issues
                    )
                    if _convergence_warnings:
                        topo_node["convergence_warnings"] = list(_convergence_warnings)
                    if _convergence_transition_issues:
                        topo_node["convergence_transition_issues"] = list(
                            _convergence_transition_issues
                        )
                    if not _can_converge:
                        _base_repair_strategy = classify_convergence_repair(
                            _convergence_issues,
                            transition_issues=[],
                        )
                        _state_repair_history = []
                        _state_repair_limit = min(
                            2,
                            remaining_optional_branch_attempts(
                                _optional_attempt_budget
                            )
                            if _optional_attempt_budget is not None
                            else 2,
                        )
                        for _state_attempt in range(_state_repair_limit):
                            if _optional_attempt_budget is not None:
                                _budget_result = consume_optional_branch_attempt(
                                    _optional_attempt_budget,
                                    phase="state_repair",
                                )
                                if not _budget_result["allowed"]:
                                    break
                            _repair_strategy = escalate_state_repair_strategy(
                                _base_repair_strategy,
                                attempt=_state_attempt,
                            )
                            topo_node["convergence_repair_strategy"] = _repair_strategy
                            _state_repair_history.append({
                                "attempt": _state_attempt + 1,
                                "strategy": _repair_strategy,
                                "issues": list(_convergence_issues),
                            })
                            print(
                                "   🩹 What-If 存在硬收束冲突，按策略修复后复检："
                                f"{_repair_strategy}"
                            )
                            _state_repair_result = try_repair_whatif_convergence_segment(
                                content,
                                issues=_convergence_issues,
                                sibling_states=_whatif_sibling_states,
                                provider=provider,
                                output_language=output_language,
                                downstream_opening=convergence_opening,
                                strategy=_repair_strategy,
                                minimum_total_chars=_whatif_accepted_min,
                                narrative_pov_contract=_narrative_pov_contract,
                            )
                            content = _state_repair_result["content"]
                            if _state_repair_result["repair_failed"]:
                                _repair_error = (
                                    "收束修复 LLM 暂时不可用："
                                    f"{_state_repair_result['error_category']}"
                                )
                                topo_node["convergence_repair_error"] = {
                                    "category": _state_repair_result["error_category"],
                                    "message": _state_repair_result["error"],
                                }
                                _convergence_issues = list(_convergence_issues) + [
                                    _repair_error
                                ]
                                mark_incompatible_whatif_terminal(
                                    topo_node,
                                    _convergence_issues,
                                )
                                _whatif_omit_reason = _repair_error
                                print(
                                    "   ⚠️  What-If 收束修复调用失败，保留硬冲突并省略该可选分支："
                                    + _repair_error
                                )
                                break
                            try:
                                _whatif_states = extract_terminal_states(
                                    chapter_text=content,
                                    chapter_title=title,
                                    provider=provider,
                                    worldview=novel_ctx.worldview,
                                    output_language=output_language,
                                    inherited_states=_whatif_parent_states,
                                    narrative_contract=_narrative_reality_contract,
                                )
                            except Exception as _e:
                                print(f"   ⚠️  What-If 分级修复后末态抽取失败：{_e}")
                                _whatif_states = {}
                            real_id_to_terminal_states[real_id] = _whatif_states
                            _convergence_result = evaluate_convergence_states(
                                _whatif_states,
                                _whatif_sibling_states,
                            )
                            _can_converge = bool(_convergence_result["compatible"])
                            _convergence_hard_issues = list(
                                _convergence_result.get("hard_issues") or []
                            )
                            _convergence_warnings = list(
                                _convergence_result.get("warnings") or []
                            )
                            _convergence_transition_issues = list(
                                _convergence_result.get("bridge_requirements") or []
                            )
                            _convergence_issues = list(_convergence_hard_issues)
                            topo_node["convergence_hard_issues"] = list(
                                _convergence_hard_issues
                            )
                            if _convergence_warnings:
                                topo_node["convergence_warnings"] = list(
                                    _convergence_warnings
                                )
                            if _convergence_transition_issues:
                                topo_node["convergence_transition_issues"] = list(
                                    _convergence_transition_issues
                                )
                            if _can_converge:
                                break
                            _base_repair_strategy = classify_convergence_repair(
                                _convergence_issues,
                                transition_issues=[],
                            )
                        topo_node["state_repair_history"] = _state_repair_history
                        if not _can_converge:
                            _convergence_reason = "；".join(_convergence_issues[:6])
                            mark_incompatible_whatif_terminal(
                                topo_node,
                                _convergence_issues,
                            )
                            _whatif_omit_reason = _convergence_reason
                            print(
                                "   ⚠️  What-If 两轮完整末场修复后仍存在硬冲突，"
                                "省略该可选分支："
                                + _convergence_reason
                            )
                        elif _convergence_transition_issues:
                            topo_node["convergence_status"] = "bridge_required"
                            topo_node["convergence_transition_issues"] = list(
                                _convergence_transition_issues
                            )
                    elif _convergence_transition_issues:
                        topo_node["convergence_status"] = "bridge_required"
                        topo_node["convergence_transition_issues"] = list(
                            _convergence_transition_issues
                        )
                        print(
                            "   ↪️  What-If 与共同下游存在可过渡差异，保留收束并记录过渡要求："
                            + "；".join(_convergence_transition_issues[:6])
                        )
                if (
                    convergence_opening.strip()
                    and not topo_node.get("terminal_branch")
                    and topo_node.get("convergence_status") != "terminal"
                ):
                    _transition_budget_allowed = True
                    if _optional_attempt_budget is not None:
                        _transition_budget_allowed = consume_optional_branch_attempt(
                            _optional_attempt_budget,
                            phase="transition_repair",
                        )["allowed"]
                    if _transition_budget_allowed:
                        _repair_result = repair_whatif_until_compatible(
                            content=content,
                            downstream_opening=convergence_opening,
                            sibling_states=_whatif_sibling_states,
                            provider=provider,
                            output_language=output_language,
                            max_attempts=min(
                                int(
                                    os.getenv(
                                        "STORY_PLANNER_WHATIF_TRANSITION_REPAIR_ATTEMPTS",
                                        "1",
                                    )
                                ),
                                1,
                            ),
                            minimum_total_chars=_whatif_accepted_min,
                            narrative_pov_contract=_narrative_pov_contract,
                        )
                    else:
                        # No repair budget remains, but a final read-only
                        # transition review is still useful.  It can accept
                        # the candidate without another generation request.
                        _repair_result = repair_whatif_until_compatible(
                            content=content,
                            downstream_opening=convergence_opening,
                            sibling_states=_whatif_sibling_states,
                            provider=provider,
                            output_language=output_language,
                            max_attempts=0,
                            minimum_total_chars=_whatif_accepted_min,
                            narrative_pov_contract=_narrative_pov_contract,
                        )
                    if _repair_result.get("repair_error"):
                        topo_node["convergence_repair_error"] = dict(
                            _repair_result["repair_error"]
                        )
                        _repair_error_reason = (
                            "末场修复重试耗尽："
                            f"{_repair_result['repair_error'].get('category', 'unknown')}"
                        )
                        if _record_optional_quality_warning(
                            _repair_error_reason,
                            content=content,
                            category=_repair_result["repair_error"].get("category"),
                            review=_repair_result.get("review"),
                        ):
                            print(
                                "   ⚠️  What-If 多轮末场修复未完成，保留正文并记录 warning："
                                f"{_repair_result['repair_error'].get('category', 'unknown')}"
                            )
                        else:
                            _whatif_omit_reason = _repair_error_reason
                            print(
                                "   ⚠️  What-If 多轮末场修复存在硬冲突，将省略该可选分支："
                                f"{_repair_result['repair_error'].get('category', 'unknown')}"
                            )
                    if _whatif_omit_reason:
                        _omit_current_optional_branch(_whatif_omit_reason)
                        continue
                    try:
                        content = enforce_whatif_content_budget(
                            _repair_result["content"],
                            min_chars=_whatif_budget_min,
                            max_chars=_whatif_budget["target_chars"],
                            preserve_ending=True,
                        )
                    except WhatIfContentBudgetError as exc:
                        if not _record_optional_quality_warning(
                            str(exc),
                            content=getattr(exc, "content", "") or content,
                            category="underlong_content",
                        ):
                            _omit_current_optional_branch(str(exc))
                            continue
                        content = str(getattr(exc, "content", "") or content).strip()
                    _transition_review = _repair_result["review"]
                    if (
                        not _transition_review.get("hard_conflict")
                        and (
                            _transition_review.get("issues")
                            or int(_transition_review.get("score", 0) or 0)
                            < int(os.getenv("STORY_PLANNER_TRANSITION_MIN_SCORE", "5"))
                        )
                    ):
                        _record_optional_quality_warning(
                            "；".join(
                                str(issue)
                                for issue in (_transition_review.get("issues") or [])
                            )
                            or (
                                "实际衔接评分不足："
                                f"{_transition_review.get('score', 0)}/10"
                            ),
                            content=content,
                            review=_transition_review,
                        )
                    topo_node["transition_review"] = _transition_review
                    topo_node["convergence_repair_history"] = _repair_result[
                        "strategy_history"
                    ]
                    if _repair_result["attempts"]:
                        print(
                            "   🩹 What-If 完成多轮末场修复："
                            f"{_repair_result['attempts']} 轮，"
                            f"最终评分 {_transition_review.get('score', 0)}/10"
                        )
                        try:
                            _whatif_states = extract_terminal_states(
                                chapter_text=content,
                                chapter_title=title,
                                provider=provider,
                                worldview=novel_ctx.worldview,
                                output_language=output_language,
                                inherited_states=_whatif_parent_states,
                                narrative_contract=_narrative_reality_contract,
                            )
                            real_id_to_terminal_states[real_id] = _whatif_states
                        except Exception as _e:
                            print(f"   ⚠️  多轮末场修复后末态抽取失败：{_e}")
                            _whatif_states = {}
                    if not _repair_result["compatible"]:
                        _transition_issues = [
                            str(issue)
                            for issue in (_transition_review.get("issues") or [])[:6]
                            if str(issue).strip()
                        ]
                        try:
                            _transition_min_score = max(
                                0,
                                min(
                                    10,
                                    int(
                                        os.getenv(
                                            "STORY_PLANNER_TRANSITION_MIN_SCORE",
                                            "5",
                                        )
                                    ),
                                ),
                            )
                        except (TypeError, ValueError):
                            _transition_min_score = 5
                        try:
                            _transition_score = int(
                                _transition_review.get("score", 0) or 0
                            )
                        except (TypeError, ValueError):
                            _transition_score = 0
                        _transition_reason = "；".join(_transition_issues)
                        if _transition_score < _transition_min_score:
                            _score_reason = (
                                f"实际衔接评分不足：{_transition_score}/10"
                                f"（最低 {_transition_min_score} 分）"
                            )
                            _transition_reason = (
                                _score_reason
                                + (f"；{_transition_reason}" if _transition_reason else "")
                            )
                        if not _transition_reason:
                            _transition_reason = "最终正文与下游章节实际开头不兼容"
                        if _record_optional_quality_warning(
                            _transition_reason,
                            content=content,
                            review=_transition_review,
                        ):
                            print(
                                "   ⚠️  What-If 实际衔接评分不足，保留正文并记录 warning："
                                + _transition_reason
                            )
                        else:
                            mark_incompatible_whatif_terminal(
                                topo_node,
                                list(_transition_review.get("issues") or [
                                    "最终正文与下游章节实际开头不兼容"
                                ]),
                            )
                            _whatif_omit_reason = _transition_reason
                            print(
                                "   ⚠️  What-If 实际衔接存在硬冲突，将省略该可选分支："
                                + _transition_reason
                            )
                    else:
                        if _whatif_sibling_states:
                            _state_result = evaluate_convergence_states(
                                _whatif_states,
                                _whatif_sibling_states,
                            )
                            _state_hard_issues = list(
                                _state_result.get("hard_issues") or []
                            )
                            topo_node["convergence_hard_issues"] = list(
                                _state_hard_issues
                            )
                            if _state_result.get("warnings"):
                                topo_node["convergence_warnings"] = list(
                                    _state_result["warnings"]
                                )
                            if _state_result.get("bridge_requirements"):
                                topo_node["convergence_transition_issues"] = list(
                                    _state_result["bridge_requirements"]
                                )
                                topo_node["convergence_status"] = "bridge_required"
                            if _state_hard_issues:
                                mark_incompatible_whatif_terminal(
                                    topo_node,
                                    _state_hard_issues,
                                )
                                _whatif_omit_reason = "；".join(
                                    str(issue) for issue in _state_hard_issues[:6]
                                )
                if _whatif_omit_reason:
                    _omit_current_optional_branch(_whatif_omit_reason)
                    continue
                # 所有收束/末场修复完成后再做一次统一 POV 门禁，避免后续
                # 修复把人称重新改回“我”。失败时 What-If 作为可选节点省略，
                # 不影响主线继续发布。
                content, _pov_issues, _pov_repaired = _pov_gate_repair_text(
                    content,
                    title=title,
                    attempt_budget=_optional_attempt_budget,
                )
                if _pov_issues:
                    _pov_warning = (
                        "POV 叙事视角不一致且单次定向修复后仍未通过："
                        + "；".join(_pov_issues[:4])
                    )
                    # POV drift is a presentation-quality finding for an
                    # optional What-If.  Keep the already generated prose so
                    # the branch remains playable, and make the issue visible
                    # to release review instead of deleting a whole node.
                    _record_optional_quality_warning(
                        _pov_warning,
                        content=content,
                    )
                    print(
                        f"   ⚠️  {title} {_pov_warning}，保留正文并记录 warning"
                    )
                _chapter_plan["topology_result"] = topo_result
                _save_chapter_branch_plan(task_id, _chapter_plan)
                # fantasy: 生成 What-If 但不注入软分支
                if branch_type == "fantasy":
                    from services.story_planner.core.novel_reader import summarize_whatif_chapter as _swc
                    _fantasy_summary = _swc(
                        whatif_text=content, whatif_title=title, provider=provider,
                        worldview=novel_ctx.worldview, output_language=output_language,
                    )
                    if _fantasy_summary:
                        topo_node["plan_direction"] = topo_node.get("summary", "")
                        topo_node["summary"] = _fantasy_summary
                        refresh_whatif_choice_text(topo_node, _fantasy_summary)
                    md_content = f"# {title}\n\n{content}\n"
                else:
                    # 注入软分支（default，或任何有 What-If 的类型）
                    print(f"   💬 注入软分支：{title}（What-If）...")
                    whatif_scenes_count = _whatif_budget["scene_count"]
                    from services.story_planner.core.novel_reader import summarize_whatif_chapter
                    whatif_summary = summarize_whatif_chapter(
                        whatif_text=content,
                        whatif_title=title,
                        provider=provider,
                        worldview=novel_ctx.worldview,
                        output_language=output_language,
                    )
                    if whatif_summary:
                        topo_node["plan_direction"] = topo_node.get("summary", "")
                        topo_node["summary"] = whatif_summary
                        refresh_whatif_choice_text(topo_node, whatif_summary)
                    content = inject_soft_branches(
                        chapter_text=content,
                        chapter_idx=-1,
                        provider=provider,
                        novel_context=novel_ctx,
                        scenes_count=whatif_scenes_count,
                        variable_defs=variable_defs,
                        total_branches=_total_soft_branches,
                        output_language=output_language,
                        current_chapter_summary=whatif_summary,
                        branch_type=branch_type,
                        required_effect_keys=_effect_schedule_by_topo.get(topo_id, []),
                        choice_text_history=_choice_text_history,
                        metrics=_node_soft_branch_metrics,
                        resume_state=_plan_entry.get("soft_branch_gap_checkpoint"),
                        checkpoint_callback=_persist_soft_branch_gap_state,
                        narrative_pov_contract=_narrative_pov_contract,
                    )
                    # A What-If's optional soft theatres may be dropped
                    # independently; never discard the already-valid What-If
                    # body merely because an injected theatre drifted.
                    content = _check_original_injected_pov(content, title=title)
                    md_content = f"# {title}\n\n{content}\n"

        else:
            # 原著节点：切原文 + 注入软分支
            ch_text = chapters[ch_idx] if ch_idx is not None and ch_idx < len(chapters) else ""
            # death_corridor / fantasy: 原著章不注入软分支
            # death_corridor — 原文全压氛围；fantasy — 无数值系统，不需要软分支积累
            if branch_type in ("death_corridor", "fantasy"):
                print(f"   📖 {branch_type} 原著章直接采用原文（跳过软分支注入）：{title}")
                md_content = f"# {title}\n\n{ch_text.strip()}\n"
            else:
                print(f"   💬 注入软分支：{title}...")
                # 原著章节按字数与有效场景数自适应规划，普通章最多 3 组、岔口章最多 4 组。
                _sc = plan_original_chapter_scene_count(
                    topo_node.get("type", "chapter"),
                    chapter_text=ch_text,
                )
                ch_text = inject_soft_branches(
                    chapter_text=ch_text,
                    chapter_idx=ch_idx,
                    provider=provider,
                    novel_context=novel_ctx,
                    scenes_count=_sc,
                    variable_defs=variable_defs,
                    total_branches=_total_soft_branches,
                    output_language=output_language,
                    branch_type=branch_type,
                    required_effect_keys=_effect_schedule_by_topo.get(topo_id, []),
                    choice_text_history=_choice_text_history,
                    metrics=_node_soft_branch_metrics,
                    resume_state=_plan_entry.get("soft_branch_gap_checkpoint"),
                    checkpoint_callback=_persist_soft_branch_gap_state,
                    narrative_pov_contract=_narrative_pov_contract,
                )
                ch_text = _check_original_injected_pov(ch_text, title=title)
            md_content = f"# {title}\n\n{ch_text.strip()}\n"
            # 原著节点末态：直接复用预扫阶段的结果
            if ch_idx is not None and ch_idx in chapter_idx_to_states:
                real_id_to_terminal_states[real_id] = chapter_idx_to_states[ch_idx]

        # ---- 创建 StoryNode ----
        node = StoryNode(
            id=real_id,
            depth=depth,
            title=title,
            parent_id=parent_id_single,
            parent_ids=real_parent_ids,
            content_file=f"{real_id}.md",
            children=[],
            summary=topo_node.get("summary", title),
            plan_direction=topo_node.get("plan_direction", ""),
            terminal_states=real_id_to_terminal_states.get(real_id, {}),
            legacy_flags=topo_node.get("legacy_flags", {}),
            legacy_requirements=topo_node.get("legacy_requirements", []),
            legacy_evidence=topo_node.get("legacy_evidence", []),
            legacy_review=topo_node.get("legacy_review", {}),
            legacy_validation=topo_node.get("legacy_validation", {}),
            is_original=is_original,
            director_metadata={
                key: deepcopy(topo_node[key])
                for key in (
                    "convergence_status",
                    "convergence_hard_issues",
                    "convergence_warnings",
                    "convergence_transition_issues",
                    "convergence_repair_strategy",
                    "state_repair_history",
                    "transition_review",
                    "convergence_repair_history",
                    "convergence_repair_error",
                    "validation_status",
                    "validation_warnings",
                )
                if key in topo_node
            },
            generated_at=_dt.now().isoformat(),
        )
        tree.add_node(node)
        if not tree.root_id:
            tree.root_id = real_id

        # 落盘前清洗舞台指令/创作注释（原文可能自带“（场景转换：…）”这类标注）
        from services.story_planner.core.story_generator import _strip_stage_directions
        md_content = _strip_stage_directions(md_content, node_id=real_id)

        if not is_original:
            min_generated_chars = max(300, min(1200, avg_chapter_chars // 3))
            md_content, content_ok, content_reason, content_repaired = (
                prepare_generated_content_for_release(
                    md_content,
                    min_chars=min_generated_chars,
                )
            )
            if content_repaired:
                print(f"   🩹 节点 {topo_id} 正文末段缺失闭引号，已安全补齐并通过复检")
            if not content_ok:
                candidate_record = _save_owned_chapter_branch_md(
                    task_id=task_id,
                    name=f"{real_id}.candidate",
                    content=md_content,
                    overwrite=True,
                )
                if candidate_record is None:
                    print(f"   ⚠️  节点 {topo_id} 失败候选正文保存失败: real_id={real_id}")
                if not is_original and node_type == "chapter":
                    _generated_body = (
                        md_content.split("\n\n", 1)[1]
                        if "\n\n" in md_content
                        else md_content
                    )
                    if _record_optional_quality_warning(
                        content_reason,
                        content=_generated_body,
                        category="generated_content_quality",
                    ):
                        print(
                            f"   ⚠️  节点 {topo_id} 正文质量告警，保留可运行候选并继续："
                            f"{content_reason}"
                        )
                    else:
                        _optional_reason = _schedule_optional_branch_self_heal(
                            content_reason
                        )
                        _omit_current_optional_branch(_optional_reason)
                        continue
                # A generated ending is still a publishable candidate.  Keep
                # the artifact for diagnostics and let the score-only release
                # gate record the prose issue as a warning; never reopen all
                # previously completed chapters for a single ending defect.
                print(
                    f"   ⚠️  结局节点 {topo_id} 正文质量告警（保留候选，不触发全流程重跑）："
                    f"{content_reason}"
                )
                _pre_release_quality_warnings.append(
                    f"结局节点 {topo_id} 正文质量失败：{content_reason}"
                )

        _artifact_sha256 = content_sha256(md_content)
        if _resume_md_record is None:
            merge_soft_branch_metrics(
                _run_metrics["soft_branches"],
                _node_soft_branch_metrics,
            )
            _chapter_plan.setdefault("nodes", {}).setdefault(topo_id, {})[
                "soft_branch_metrics"
            ] = _node_soft_branch_metrics
        md_record = _resume_md_record
        if md_record is None:
            update_plan_node(
                _chapter_plan,
                topo_id,
                status="persisting",
                real_id=real_id,
                terminal_states=real_id_to_terminal_states.get(real_id, {}),
                summary=topo_node.get("summary", title),
                artifact_sha256=_artifact_sha256,
            )
            _save_chapter_branch_plan(task_id, _chapter_plan)
            md_record = _save_owned_chapter_branch_md(
                task_id=task_id,
                name=real_id,
                content=md_content,
                overwrite=True,
            )
        if md_record is None:
            raise RuntimeError(f"节点 '{topo_id}' 的 MD 保存失败：real_id={real_id}")
        if _resume_md_record is None:
            _run_metrics["nodes_generated"] += 1
        saved_md_ids.add(real_id)
        node_md_contents[real_id] = md_content
        update_plan_node(
            _chapter_plan,
            topo_id,
            status="completed",
            real_id=real_id,
            terminal_states=real_id_to_terminal_states.get(real_id, {}),
            summary=topo_node.get("summary", title),
            artifact_sha256=_artifact_sha256,
        )
        mark_node_self_heal_completed(_chapter_plan, topo_id)
        _save_chapter_branch_plan(task_id, _chapter_plan)
        stop_runtime_heartbeat(task_id, final_status="completed")
        structured_log(
            _runtime_logger,
            "node_completed",
            task_id=task_id,
            run_id=_run_id,
            stage="story_planner",
            branch_type=branch_type,
            topo_id=topo_id,
            real_id=real_id,
            resumed=_resume_md_record is not None,
        )

    # 按最终保存的真实 effects 一次性校准完整结局规则。
    # These are content-quality diagnostics, not infrastructure failures.  Keep
    # them for the release report but never restart the completed pipeline.
    _actual_effect_pairs = []
    _effect_parse_issues = []
    for _effect_node_id, _effect_content in node_md_contents.items():
        _pairs, _issues = extract_effect_pairs_from_markdown(_effect_content)
        _actual_effect_pairs.extend(_pairs)
        _effect_parse_issues.extend(
            f"节点 {_effect_node_id}: {_issue}" for _issue in _issues
        )
    if _effect_parse_issues:
        _pre_release_quality_warnings.extend(
            f"effects marker 解析失败：{issue}" for issue in _effect_parse_issues
        )
    _effect_caps = merge_effect_caps_with_observed(_effect_caps, _actual_effect_pairs)
    _chapter_plan["effect_caps"] = _effect_caps
    _threshold_calibration_issues = calibrate_ending_rules(
        variable_defs,
        topology,
        _actual_effect_pairs,
    )
    for _issue in _threshold_calibration_issues:
        print(f"   ⚖️  {_issue}")
    _route_simulation = simulate_ending_route_reachability(
        variable_defs,
        _actual_effect_pairs,
    )
    _still_unreachable = [
        f"{_key}: reason={_route.get('reason', 'unknown')}"
        for _key, _route in _route_simulation.items()
        if not _route.get("reachable")
        and next(
            (
                bool(_variable.get("route_enabled", True))
                for _variable in variable_defs
                if isinstance(_variable, dict)
                and str(_variable.get("key")) == str(_key)
            ),
            False,
        )
    ]
    if _still_unreachable:
        _pre_release_quality_warnings.append(
            "真实选择路径仍无法触发已启用结局：" + "；".join(_still_unreachable)
        )
    _chapter_plan["topology_result"] = topo_result

    # 预先建立 source_chapter_idx -> real_id 的映射，用于 DAG 强制收束兜底
    _orig_idx_to_real: dict = {}
    for t in topology:
        if t.get("is_original") and t.get("source_chapter_idx") is not None:
            _orig_idx_to_real[t["source_chapter_idx"]] = node_id_map.get(t["node_id"])

    # ---- 挂载 children（遍历拓扑 next 关系） ----
    for topo_node in topology:
        src_real_id = node_id_map.get(topo_node["node_id"])
        if not src_real_id:
            continue
        next_ids = topo_node.get("next", [])
        src_node = tree.get_node(src_real_id)
        if not src_node:
            continue
        choices = []
        for nxt in next_ids:
            dst_real_id = node_id_map.get(nxt)
            if not dst_real_id:
                # 地雷1兜底：LLM 幻觉了不存在的 node_id，强制收束到主线下一章
                src_ch_idx = topo_node.get("source_chapter_idx")
                if src_ch_idx is not None:
                    fallback_id = _orig_idx_to_real.get(src_ch_idx + 1)
                    if fallback_id:
                        print(f"   ⚠️  next='{nxt}' 不存在，强制收束到主线第{src_ch_idx+2}章")
                        dst_real_id = fallback_id
                    else:
                        print(f"   ⚠️  next='{nxt}' 不存在且找不到收束节点，跳过")
                        continue
                else:
                    print(f"   ⚠️  next='{nxt}' 不存在，跳过")
                    continue
            dst_topo = next((t for t in topology if t["node_id"] == nxt), {})
            # 硬分支用 choice_text；普通推进用「继续」
            if dst_topo.get("type") == "ending":
                choice_text = ""
            else:
                choice_text = dst_topo.get("choice_text") or "继续"
            # 结局节点额外携带 branch_variable / branch_threshold，供 converter 生成自动路由
            if dst_topo.get("type") == "ending":
                choices.append(StoryChoice(
                    choice_text=choice_text,
                    child_id=dst_real_id,
                    branch_variable=dst_topo.get("branch_variable") or None,
                    branch_threshold=dst_topo.get("branch_threshold") or None,
                ))
            else:
                choices.append(StoryChoice(choice_text=choice_text, child_id=dst_real_id))
        # What-If 节点无 children 时强制收束到主线下一章
        if (
            should_force_whatif_convergence(topo_node, choices)
            and not topo_node.get("is_original")
            and topo_node.get("type") != "ending"
        ):
            src_ch_idx = topo_node.get("source_chapter_idx")
            # What-If 节点无 source_chapter_idx，从父节点找
            for pt in topology:
                if topo_node["node_id"] in pt.get("next", []) and pt.get("is_original"):
                    fallback_id = _orig_idx_to_real.get((pt.get("source_chapter_idx") or 0) + 1)
                    if fallback_id:
                        print(f"   ⚠️  What-If 节点 '{topo_node['node_id']}' 无有效 next，强制收束")
                        choices.append(StoryChoice(choice_text="继续", child_id=fallback_id))
                    break
        src_node.children = choices

    # ---- 旧的 "What-If N-1 后处理" 已移除 ----
    # 新拓扑规则下（branch_designer），What-If 节点的 next 严格等于其原著兄弟章节的 next，
    # 即两者都收束到 pivot+2（菱形 DAG）。原著兄弟和 What-If 处于同一深度，
    # What-If 不可能直接以原著兄弟为 child，因此不再需要 children 替换补丁。

    print(f"\n✅ 故事树构建完成，共 {len(tree.nodes)} 个节点")
    if mark_regeneration_request_completed(_chapter_plan):
        print("   ✅ 定向重生成节点已全部完成，关闭本次 regeneration_request")
    _run_metrics["phase_seconds"]["node_generation"] = round(
        time.perf_counter() - _node_generation_started, 3
    )
    structured_log(
        _runtime_logger,
        "phase_completed",
        task_id=task_id,
        run_id=_run_id,
        stage="story_planner",
        branch_type=branch_type,
        phase="node_generation",
        duration_seconds=_run_metrics["phase_seconds"]["node_generation"],
    )
    update_plan_step(
        _chapter_plan,
        "node_generation",
        status="completed",
        output_digest=stable_payload_digest(
            {
                node_id: content_sha256(content)
                for node_id, content in sorted(node_md_contents.items())
            }
        ),
    )
    _run_metrics["duration_seconds"] = round(time.perf_counter() - _run_started, 3)
    _cache_metrics = _result_cache.get_metrics()
    _run_metrics.update(
        {
            "cache_namespace": _cache_metrics["namespace"],
            "cache_hits": _cache_metrics["hits"],
            "cache_misses": _cache_metrics["misses"],
            "cache_hit_rate": _cache_metrics["hit_rate"],
            "cache_writes": _cache_metrics["writes"],
            "cache_invalidations": _cache_metrics["invalidations"],
            "cache_read_errors": _cache_metrics["read_errors"],
            "cache_write_errors": _cache_metrics["write_errors"],
            "cache_miss_reasons": _cache_metrics["miss_reasons"],
            "cache": _cache_metrics,
        }
    )
    _metric_bundle = build_run_metrics(_previous_run_metrics, _run_metrics)
    _attempt_metrics = _metric_bundle["attempt_metrics"]
    _cumulative_metrics = _metric_bundle["cumulative_metrics"]
    # Keep the historical flat shape for existing consumers while exposing
    # explicit per-attempt and cumulative snapshots for resumed tasks.
    _cumulative_snapshot = deepcopy(_cumulative_metrics)
    _cumulative_metrics["attempt_metrics"] = _attempt_metrics
    _cumulative_metrics["cumulative_metrics"] = _cumulative_snapshot
    _run_metrics = _cumulative_metrics
    _chapter_plan["metrics"] = _run_metrics
    _save_chapter_branch_plan(task_id, _chapter_plan)

    # ==================== 6. 保存 story_tree.json ====================
    visual_style_context = build_visual_style_context(
        source_text=text,
        story_mode=StoryMode.CHAPTER_BRANCH,
        chapters=visual_style_source_chapters,
        worldview=novel_ctx.worldview,
        chapter_summaries=novel_ctx.chapter_summaries,
        narrative_arc=novel_ctx.narrative_arc,
        style_samples=novel_ctx.style_samples,
        world_rules=novel_ctx.world_rules,
        additional_context={"branch_type": branch_type},
    )
    print(
        "🎨 visual_style_context 已整理: "
        f"coverage={visual_style_context.get('coverage_strategy')} "
        f"source_chars={visual_style_context.get('source_char_count')} "
        f"sampled_chars={visual_style_context.get('sampled_char_count')}"
    )

    # 结局路由按 child_id 保存，支持同一变量对应多个不同阈值的结局。
    _player_variables, _branch_thresholds = build_route_metadata(
        topology,
        node_id_map,
        variable_defs,
    )
    json_data = {
        "root_id": tree.root_id,
        **_build_story_tree_book_metadata(
            config,
            novel_filename=config.get("novel_filename") or novel_name,
            task_id=task_id,
            narrative_arc=getattr(novel_ctx, "narrative_arc", ""),
            chapter_summaries=summaries,
        ),
        # 保留给 game_builder/UI 的主题兼容；生图链路不得读取此字段。
        "art_style": config.get("art_style", "ink_chinese"),
        "book_language": output_language,
        "UI_mode": infer_ui_mode(text=text, metadata=novel_ctx.worldview),
        "story_mode": "chapter_branch",
        "branch_type": branch_type,
        "visual_style_context": visual_style_context,
        "nodes": {nid: node.to_dict() for nid, node in tree.nodes.items()},
        "player_variables": _player_variables,
        "branch_thresholds": _branch_thresholds,
        "variable_defs": variable_defs,
        "effect_caps": _effect_caps,
        "metrics": _run_metrics,
        # Work-level content warnings are optional player-entry metadata.  Keep
        # the complete normalized object in every locale snapshot produced below.
        "content_warnings": _content_warnings,
    }
    if global_style:
        json_data["global_style"] = global_style
    release_ok, release_errors = validate_release_integrity(
        json_data,
        saved_md_ids=saved_md_ids,
    )
    if not release_ok:
        _pre_release_quality_warnings.extend(
            f"发布完整性告警：{error}" for error in release_errors
        )
        print(
            "   ⚠️  发布完整性存在告警，继续保存质量报告并完成流程："
            + "；".join(release_errors[:8])
        )

    def _review_ambiguous_legacy(payload: dict) -> dict:
        variable_key = str((payload or {}).get("variable") or "")
        variable_definition = next(
            (
                item for item in (variable_defs or [])
                if isinstance(item, dict) and item.get("key") == variable_key
            ),
            {},
        )
        print(
            "   🔎 可验证遗产关键词门禁已停用，执行大模型题材语义复核："
            f"variable={variable_key}, UI_mode={story_ui_mode}"
        )
        return review_variable_legacy_evidence(
            ending_text=str((payload or {}).get("content") or ""),
            variable_key=variable_key,
            ui_mode=story_ui_mode,
            provider=provider,
            variable_definition=variable_definition,
            worldview=getattr(novel_ctx, "worldview", None),
        )

    quality_report = validate_chapter_branch_release(
        json_data,
        node_md_contents,
        plan=_chapter_plan,
        worldview=getattr(novel_ctx, "worldview", None),
        legacy_semantic_reviewer=_review_ambiguous_legacy,
        ending_semantic_reviewer=_review_ending_causality,
        thresholds={
            "min_generated_chars": int(
                os.getenv("STORY_PLANNER_RELEASE_MIN_GENERATED_CHARS", "300")
            ),
            "min_endings": int(
                os.getenv("STORY_PLANNER_RELEASE_MIN_ENDINGS", "1")
            ),
            "min_score": int(
                os.getenv("STORY_PLANNER_RELEASE_MIN_SCORE", "30")
            ),
            "soft_branch_transition_drop_limit": float(
                os.getenv("STORY_PLANNER_SOFT_BRANCH_TRANSITION_DROP_LIMIT", "0.65")
            ),
            "whatif_transition_min_score": int(
                os.getenv("STORY_PLANNER_TRANSITION_MIN_SCORE", "5")
            ),
        },
        extra_warnings=_pre_release_quality_warnings,
    )
    json_data["quality_gate"] = quality_report
    _chapter_plan["quality_gate"] = quality_report
    _save_chapter_branch_plan(task_id, _chapter_plan)
    _save_chapter_branch_diagnostics(task_id, json_data, quality_report)
    if not quality_report["passed"]:
        # Quality findings are already represented as warnings by the gate.
        # A score below the configured threshold is the only release-level
        # blocker and must not reopen completed nodes or schedule a full
        # chapter_branch replay.  Operators can use the saved candidate and
        # warning report to decide whether to raise the threshold or repair a
        # specific node explicitly.
        structured_log(
            _runtime_logger,
            "release_gate_failed",
            level="error",
            task_id=task_id,
            run_id=_run_id,
            stage="story_planner",
            branch_type=branch_type,
            quality_score=quality_report["score"],
            error_count=len(quality_report["errors"]),
        )
        raise ReleaseGateError(quality_report)  # 仅分数门槛失败；候选与诊断产物已保存

    update_plan_step(
        _chapter_plan,
        "release_gate",
        status="completed",
        output_digest=stable_payload_digest(quality_report),
    )
    _save_chapter_branch_plan(task_id, _chapter_plan)

    json_data = wrap_story_tree_language_bundles(
        json_data,
        build_story_tree_language_bundle(
            json_data,
            selected_language=output_language,
            source_text=text,
            provider=provider,
        ),
        default_language=get_story_tree_player_language(),
    )
    from services.story_planner.core.chapter_branch_artifacts import (
        validate_story_tree_language_bundle,
    )

    _language_bundle_issues = validate_story_tree_language_bundle(json_data)
    if _language_bundle_issues:
        # Locale generation may fall back independently. Re-apply the same
        # canonical machine policy to every locale once before publication so
        # changing UI language can never bypass the acknowledgement gate.
        from services.story_planner.core.content_warnings import (
            localize_content_warnings,
        )

        print(
            "⚠️  story_tree 多语言内容提示不一致，执行一次确定性归一化："
            + "；".join(_language_bundle_issues)
        )
        for _locale, _locale_tree in json_data.items():
            if isinstance(_locale_tree, dict) and isinstance(
                _locale_tree.get("nodes"), dict
            ):
                _locale_tree["content_warnings"] = localize_content_warnings(
                    _content_warnings,
                    _locale,
                )
        _language_bundle_issues = validate_story_tree_language_bundle(json_data)
        if _language_bundle_issues:
            # Locale mismatches are publish diagnostics.  The canonical locale
            # remains available and the concrete issues are preserved in the
            # quality report for operators; do not restart the full novel.
            append_quality_warnings(
                quality_report,
                [f"多语言 story_tree 告警：{issue}" for issue in _language_bundle_issues],
            )
            json_data["quality_gate"] = quality_report
            _chapter_plan["quality_gate"] = quality_report
            _save_chapter_branch_plan(task_id, _chapter_plan)
            if not quality_report["passed"]:
                structured_log(
                    _runtime_logger,
                    "release_gate_failed",
                    level="error",
                    task_id=task_id,
                    run_id=_run_id,
                    stage="story_planner",
                    branch_type=branch_type,
                    quality_score=quality_report["score"],
                    error_count=len(quality_report["errors"]),
                )
                raise ReleaseGateError(quality_report)
    publish_result = _publish_chapter_branch_story_tree(
        task_id=task_id,
        md_contents=node_md_contents,
        json_data=json_data,
        expected_node_ids=saved_md_ids,
        run_id=_run_id,
        run_started_at=_run_started_at,
    )
    _run_metrics["publication_fingerprint"] = publish_result.publication_fingerprint
    _run_metrics["orphan_md_deleted"] = publish_result.orphan_md_deleted
    print(
        "✅ story_tree.json 与最终 MD 已事务发布到数据库"
        f"（清理孤儿 MD: {publish_result.orphan_md_deleted}）"
    )
    structured_log(
        _runtime_logger,
        "chapter_branch_published",
        task_id=task_id,
        run_id=_run_id,
        stage="story_planner",
        branch_type=branch_type,
        node_count=len(saved_md_ids),
        quality_score=quality_report["score"],
        orphan_md_deleted=publish_result.orphan_md_deleted,
        duration_seconds=_run_metrics["duration_seconds"],
    )
    update_plan_step(
        _chapter_plan,
        "story_publish",
        status="completed",
        output_digest=publish_result.publication_fingerprint,
    )
    _save_chapter_branch_plan(task_id, _chapter_plan)

    # 从持久化章节摘要 + 世界观拼出剧本摘要，不因数据库失败额外消耗 LLM。
    from services.story_planner.core.chapter_branch_resume import (
        build_summary_from_checkpoint,
    )

    summary_payload = build_summary_from_checkpoint(
        _chapter_plan,
        {
            "worldview": novel_ctx.worldview,
            "world_rules": novel_ctx.world_rules,
            "narrative_arc": novel_ctx.narrative_arc,
        },
    )
    _save_required_chapter_branch_summary(task_id=task_id, **summary_payload)
    print("✅ 剧本摘要已从 chapter_summaries + worldview 拼出（无额外 LLM 调用）")
    with Session(engine) as _summary_session:
        _summary_row = _summary_session.exec(
            select(StorySummary).where(StorySummary.task_id == task_id)
        ).first()
    _summary_data = getattr(_summary_row, "data", {}) if _summary_row else {}
    if (
        getattr(_summary_row, "status", None) != 1
        or not isinstance(_summary_data, dict)
        or not all(
            str(_summary_data.get(_field) or "").strip()
            for _field in ("outline", "background", "one_liner")
        )
    ):
        raise RuntimeError("chapter_branch 剧本摘要未完整落库")
    update_plan_step(
        _chapter_plan,
        "summary_publish",
        status="completed",
        output_digest=stable_payload_digest(_summary_data),
    )
    update_plan_step(
        _chapter_plan,
        "stage_complete",
        status="completed",
        output_digest="completion-request-validated",
    )
    _save_chapter_branch_plan(task_id, _chapter_plan)

    # ==================== 7. 触发后续服务 ====================
    print(f"\n🚀 触发后续服务...")
    _trigger_downstream(task_id, game_project)

    print(f"\n🎉 章节分支全流程完成!")
    if _usage_tracker:
        _usage_tracker.flush()
    return task_id


def _parse_chapter_branch_mode(
    novel_path,
    novel_name: str,
    game_project: str,
    global_style: str = None,
    content_warning_source_text: str | None = None,
):
    """Run chapter_branch with bounded checkpoint restarts for repairable failures."""
    from services.story_planner.core.chapter_branch_runtime import (
        NodeSelfHealRequired,
        ReleaseQualityRepairRequired,
    )

    while True:
        try:
            return _parse_chapter_branch_mode_once(
                novel_path,
                novel_name,
                game_project,
                global_style,
                content_warning_source_text=content_warning_source_text,
            )
        except NodeSelfHealRequired as exc:
            _stop_runtime_heartbeat(game_project, final_status="repairing")
            print(f"♻️  {exc}，从 chapter_branch 断点继续")
        except ReleaseQualityRepairRequired as exc:
            _stop_runtime_heartbeat(game_project, final_status="repairing")
            print(f"🩹 {exc}，从 chapter_branch 断点继续")


@celery_app.task(
    name="services.story_planner.tasks.regenerate_chapter_branch_nodes",
    soft_time_limit=RUNTIME_SETTINGS.task_soft_time_limit,
    time_limit=RUNTIME_SETTINGS.task_time_limit,
)
def regenerate_chapter_branch_nodes(
    task_id: str,
    topology_node_ids,
    include_non_original_descendants: bool = True,
    reason: str = "人工定向重生成",
):
    """重开指定 chapter_branch 节点，并复用其余已完成检查点重新执行规划。"""
    from services.story_planner.core.chapter_branch_runtime import (
        mark_running_nodes_failed,
        reopen_plan_nodes,
    )

    if isinstance(topology_node_ids, str):
        requested_node_ids = [
            node_id.strip()
            for node_id in re.split(r"[,，\s]+", topology_node_ids)
            if node_id.strip()
        ]
    else:
        requested_node_ids = [
            str(node_id).strip()
            for node_id in (topology_node_ids or [])
            if str(node_id).strip()
        ]
    requested_node_ids = list(dict.fromkeys(requested_node_ids))
    if not requested_node_ids:
        raise ValueError("定向重生成至少需要一个 topology_node_id")

    config = _get_task_config(task_id)
    if config.get("story_mode") != StoryMode.CHAPTER_BRANCH:
        raise ValueError(
            f"任务 {task_id} 不是 chapter_branch 模式，不能执行节点定向重生成"
        )

    plan = _load_chapter_branch_plan(task_id)
    if not isinstance(plan.get("nodes"), dict) or not plan["nodes"]:
        raise ValueError(f"任务 {task_id} 尚无可恢复的 chapter_branch_plan")

    novel_path = Path(RAW_NOVELS_PATH) / f"{task_id}.txt"
    if not novel_path.exists():
        raise FileNotFoundError(f"原始剧本文件不存在: {novel_path}")

    usage_run_id = None
    try:
        if _usage_tracker:
            usage_run_id = _usage_tracker.set_context(
                task_id,
                "story_planner",
                force_new_run=True,
            )
        start_stage(task_id, "story_planner", 18)
        reopened_node_ids = reopen_plan_nodes(
            plan,
            requested_node_ids,
            include_non_original_descendants=include_non_original_descendants,
            reason=reason,
        )
        _save_chapter_branch_plan(
            task_id,
            plan,
            require_stage_ownership=False,
        )
        print(
            f"♻️  chapter_branch 定向重生成已登记: task_id={task_id}, "
            f"nodes={reopened_node_ids}"
        )
        return _parse_chapter_branch_mode(
            novel_path,
            config["novel_filename"],
            task_id,
        )
    except Exception as exc:
        try:
            failed_plan = _load_chapter_branch_plan(task_id)
            if mark_running_nodes_failed(failed_plan, str(exc)):
                _save_chapter_branch_plan(
                    task_id,
                    failed_plan,
                    require_stage_ownership=False,
                )
        except Exception as checkpoint_error:
            print(f"⚠️  定向重生成失败状态落盘失败：{checkpoint_error}")
        fail_stage(task_id, "story_planner", 18, str(exc))
        raise
    finally:
        _stop_runtime_heartbeat(task_id, final_status="stopped")
        if _usage_tracker and usage_run_id:
            _usage_tracker.flush(run_id=usage_run_id)
            _usage_tracker.clear_context(usage_run_id)


@celery_app.task(
    name="services.story_planner.tasks.generate_summary_only",
    soft_time_limit=RUNTIME_SETTINGS.auxiliary_task_soft_time_limit,
    time_limit=RUNTIME_SETTINGS.auxiliary_task_time_limit,
)
def generate_summary_only(task_id: str):
    """
    仅生成剧本摘要（大纲/背景/一句话故事），不触发完整任务链。
    在文件上传后立即触发，供前端在输出约束页展示。

    - ORIGINAL / CHAPTER_BRANCH: 读原文前8000字，用 LLM 提取摘要
    - MULTI: 跑 WorldAnalyst 分析世界观，写 Worldview(DRAFT)，再提取摘要
    """
    try:
        with auxiliary_task_guard(
            task_id,
            "summary",
            ttl_seconds=RUNTIME_SETTINGS.auxiliary_task_time_limit + 60,
        ) as acquired:
            if not acquired:
                print(f"♻️  generate_summary_only 重复任务已跳过: task_id={task_id}")
                return
            if _summary_is_ready(task_id):
                print(f"♻️  generate_summary_only 已有完成结果: task_id={task_id}")
                return
            return _generate_summary_only_owned(task_id)
    except Exception as e:
        import traceback
        print(f"❌ generate_summary_only 失败: {e}\n{traceback.format_exc()}")
        _mark_summary_failed(task_id, "提炼失败")
        raise


def _generate_summary_only_owned(task_id: str):
    usage_run_id = None
    try:
        if _usage_tracker:
            usage_run_id = _usage_tracker.set_context(
                task_id, "story_planner", force_new_run=True
            )
        config = _get_task_config(task_id)
        story_mode = config["story_mode"]
        novel_path = Path(RAW_NOVELS_PATH) / f"{task_id}.txt"

        if not novel_path.exists():
            raise FileNotFoundError(f"原始剧本文件不存在: {novel_path}")

        if story_mode == StoryMode.MULTI:
            # MULTI 模式：跑世界观分析，写 Worldview(DRAFT)，再从 narrative_structure 提取摘要
            analyst = WorldAnalyst(provider=STORY_PLANNER_GATEWAY_PROVIDER)
            world_bible = analyst.analyze_novel_file(
                filepath=str(novel_path),
                project_name=task_id,
                output_dir="world_db",
                task_id=task_id,
            )
            ns = world_bible.narrative_structure or {}
            wv = world_bible.worldview or {}
            saved = _save_summary(
                task_id=task_id,
                outline="; ".join(ns.get("major_conflicts", [])) or ns.get("inciting_incident", ""),
                background=wv.get("history", "") or "",
                one_liner=ns.get("inciting_incident", ""),
            )
            if not saved:
                raise RuntimeError("剧本摘要写入数据库失败")
        else:
            # ORIGINAL / CHAPTER_BRANCH：LLM 从原文片段提取摘要
            text = _read_and_normalize_novel(novel_path)
            _generate_summary_from_text(task_id=task_id, text_sample=text)

        print(f"✅ generate_summary_only 完成: task_id={task_id}")
    finally:
        if _usage_tracker and usage_run_id:
            _usage_tracker.flush(run_id=usage_run_id)
            _usage_tracker.clear_context(usage_run_id)


def _summary_is_ready(task_id: str) -> bool:
    with Session(engine) as session:
        existing = session.exec(
            select(StorySummary).where(StorySummary.task_id == task_id)
        ).first()
    return bool(existing and existing.status == 1 and isinstance(existing.data, dict))


@celery_app.task(
    name="services.story_planner.tasks.generate_project_name_only",
    soft_time_limit=RUNTIME_SETTINGS.auxiliary_task_soft_time_limit,
    time_limit=RUNTIME_SETTINGS.auxiliary_task_time_limit,
)
def generate_project_name_only(task_id: str):
    """
    用 LLM 给作品起一个中文名，写回 novel_tasks.project_name / generated_project_name。
    单/批量上传都可独立触发，与 generate_summary_only 解耦。
    读前 8000 字，一次 LLM 调用，失败仅记录日志，不影响主流水线。
    """
    try:
        with auxiliary_task_guard(
            task_id,
            "project_name",
            ttl_seconds=RUNTIME_SETTINGS.auxiliary_task_time_limit + 60,
        ) as acquired:
            if not acquired:
                print(f"♻️  generate_project_name_only 重复任务已跳过: task_id={task_id}")
                return
            if _project_name_is_ready(task_id):
                print(f"♻️  generate_project_name_only 已有完成结果: task_id={task_id}")
                return
            return _generate_project_name_only_owned(task_id)
    except Exception as e:
        import traceback
        print(f"❌ generate_project_name_only 失败: {e}\n{traceback.format_exc()}")


def _generate_project_name_only_owned(task_id: str):
    usage_run_id = None
    try:
        if _usage_tracker:
            usage_run_id = _usage_tracker.set_context(
                task_id, "story_planner", force_new_run=True
            )
        novel_path = Path(RAW_NOVELS_PATH) / f"{task_id}.txt"
        if not novel_path.exists():
            print(f"❌ generate_project_name_only: 文件不存在 {novel_path}")
            return
        text = _read_and_normalize_novel(novel_path)[:8000]
        name = _generate_project_name_from_text(text)
        if name:
            _update_project_name(task_id, name)
        print(f"✅ generate_project_name_only 完成: task_id={task_id}, name={name!r}")
    finally:
        if _usage_tracker and usage_run_id:
            _usage_tracker.flush(run_id=usage_run_id)
            _usage_tracker.clear_context(usage_run_id)


def _project_name_is_ready(task_id: str) -> bool:
    from common.task_models import NovelTask

    with Session(platform_engine) as session:
        task = session.exec(
            select(NovelTask).where(NovelTask.task_id == task_id)
        ).first()
    return bool(task and str(task.generated_project_name or "").strip())


def _generate_project_name_from_text(text_sample: str, provider: str = None) -> str:
    """从原文前 8000 字调一次 LLM 起名，返回清洗后的中文作品名；失败返回空串。"""
    from services.story_planner.core.llm_client import LLMClient
    if provider is None:
        provider = STORY_PLANNER_GATEWAY_PROVIDER
    try:
        client = LLMClient(provider=provider, temperature=0.8, max_tokens=50)
        raw = client.chat_completion(
            system_prompt=(
                "你是小说命名专家。阅读小说片段后，输出一个不超过15个字的中文作品名，"
                "不加书名号，只输出名字本身。"
            ),
            user_prompt=text_sample[:8000],
            timeout=60,
        )
        return (raw or "").strip().strip("《》").splitlines()[0].strip()
    except Exception as e:
        print(f"⚠️  LLM 起名失败: {e}")
        return ""


def _update_project_name(task_id: str, name: str):
    """把 LLM 生成的作品名写回 novel_tasks 表。失败仅记录日志。"""
    from common.task_models import NovelTask
    from common.platform_db import platform_engine as task_engine  # novel_tasks 已迁平台库
    try:
        with Session(task_engine) as session:
            task = session.exec(select(NovelTask).where(NovelTask.task_id == task_id)).first()
            if not task:
                print(f"⚠️  novel_tasks 中找不到 task_id={task_id}，跳过更新作品名")
                return
            task.project_name = name
            task.generated_project_name = name
            session.add(task)
            session.commit()
        print(f"✅ 作品名已更新: task_id={task_id}, project_name={name!r}")
    except Exception as e:
        print(f"⚠️  作品名更新失败（不影响主流程）: {e}")


def _save_required_chapter_branch_summary(
    task_id: str,
    outline: str,
    background: str,
    one_liner: str,
) -> None:
    if not _save_summary(
        task_id,
        outline,
        background,
        one_liner,
        require_stage_ownership=True,
    ):
        raise RuntimeError("剧本摘要写入数据库失败")


def _save_summary(
    task_id: str,
    outline: str,
    background: str,
    one_liner: str,
    *,
    require_stage_ownership: bool = False,
) -> bool:
    """将剧本摘要写入 story_summary 表；返回是否保存成功。"""
    try:
        if require_stage_ownership:
            ensure_stage_lease_owned(task_id, "story_planner")
        with Session(engine) as session:
            existing = session.exec(select(StorySummary).where(StorySummary.task_id == task_id)).first()
            summary_data = {"outline": outline, "background": background, "one_liner": one_liner}
            if existing:
                existing.data = summary_data
                existing.status = 1
                session.add(existing)
            else:
                session.add(StorySummary(task_id=task_id, data=summary_data, status=1))
            session.commit()
        print(f"✅ 剧本摘要已保存: task_id={task_id}")
        return True
    except StageLeaseLostError:
        raise
    except Exception as e:
        print(f"⚠️  剧本摘要保存失败: {e}")
        return False


def _mark_summary_failed(task_id: str, error: str):
    """记录摘要生成失败状态，供 API 轮询返回。"""
    try:
        with Session(engine) as session:
            existing = session.exec(select(StorySummary).where(StorySummary.task_id == task_id)).first()
            if existing:
                existing.status = 2
                existing.data = {**dict(existing.data or {}), "error": error}
                session.add(existing)
            else:
                session.add(StorySummary(task_id=task_id, data={"error": error}, status=2))
            session.commit()
    except Exception as exc:
        print(f"⚠️  剧本摘要失败状态保存失败: {exc}")


def _generate_summary_from_text(task_id: str, text_sample: str, provider: str = None):
    """
    ORIGINAL/CHAPTER_BRANCH 模式：用 LLM 从原文片段提取摘要。
    提取三个字段：outline（大纲）、background（背景）、one_liner（一句话故事）。
    """
    import os
    from services.story_planner.core.llm_client import LLMClient
    if provider is None:
        provider = STORY_PLANNER_GATEWAY_PROVIDER

    system_prompt = (
        "You are a story analyst. Given a novel excerpt, extract three things in the same language as the input text:\n"
        "1. outline: A 2-4 sentence plot summary covering the main story arc.\n"
        "2. background: A 1-2 sentence description of the world/setting.\n"
        "3. one_liner: One sentence that captures the essence of the story.\n"
        "Return ONLY a JSON object with keys: outline, background, one_liner. No other text."
    )
    sample = text_sample[:8000]
    try:
        client = LLMClient(provider=provider, temperature=0.3, max_tokens=600)
        raw = client.chat_completion(system_prompt=system_prompt, user_prompt=sample, timeout=90).strip()
        import json as _json
        # 去掉可能的 markdown 代码块
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = _json.loads(raw)
        saved = _save_summary(
            task_id=task_id,
            outline=result.get("outline", ""),
            background=result.get("background", ""),
            one_liner=result.get("one_liner", ""),
        )
        if not saved:
            raise RuntimeError("剧本摘要写入数据库失败")
    except Exception as e:
        # 不能吞掉异常：外层 generate_summary_only 负责把 story_summary 标成 failed。
        # 吞掉后 Celery 会显示 SUCCESS，但数据库没有 ready/failed 记录，前端只能永久 pending。
        print(f"⚠️  LLM 摘要提取失败: {e}")
        raise RuntimeError("LLM 摘要提取失败") from e




def wait_for_worldview_confirmation(
    task_id: str,
    poll_interval: int = 10,
    timeout: int = RUNTIME_SETTINGS.confirmation_timeout,
) -> bool:
    """
    等待世界观状态变为CONFIRMED

    Args:
        task_id: 世界观task_id
        poll_interval: 轮询间隔（秒）
        timeout: 超时时间（秒）

    Returns:
        bool: 成功确认返回True，超时或错误返回False
    """
    print(f"   等待世界观状态变为CONFIRMED (1)...")
    start_time = time.time()
    poll_count = 0

    while True:
        poll_count += 1
        current_time = time.time()

        # 检查超时
        if timeout and (current_time - start_time) > timeout:
            print(f"⏰ 超时: 在{timeout}秒内未检测到世界观状态变化")
            return False

        try:
            with Session(engine) as session:
                worldview = session.exec(
                    select(Worldview).where(Worldview.task_id == task_id)
                ).first()

                if not worldview:
                    print(f"❌ 错误: 找不到世界观 task_id={task_id}")
                    return False

                status = worldview.status

                if poll_count % 6 == 0:  # 每60秒输出一次状态
                    print(f"   轮询 #{poll_count}: 世界观状态={status}")

                if status == WorldviewStatus.CONFIRMED:
                    print(f"✅ 世界观状态已变为CONFIRMED!")
                    return True

                # 如果世界观被归档，则失败
                if status == WorldviewStatus.ARCHIVED:
                    print(f"❌ 世界观已被归档，无法继续")
                    return False

        except Exception as e:
            print(f"⚠️  轮询世界观状态时出错: {e}")

        # 等待下一次轮询
        time.sleep(poll_interval)


def wait_for_outline_confirmation(
    task_id: str,
    poll_interval: int = 10,
    timeout: int = RUNTIME_SETTINGS.confirmation_timeout,
) -> bool:
    """
    等待大纲状态变为CONFIRMED

    Args:
        task_id: 大纲task_id（与世界观的task_id相同）
        poll_interval: 轮询间隔（秒）
        timeout: 超时时间（秒）

    Returns:
        bool: 成功确认返回True，超时或错误返回False
    """
    print(f"   等待大纲状态变为CONFIRMED (1)...")
    start_time = time.time()
    poll_count = 0

    while True:
        poll_count += 1
        current_time = time.time()

        # 检查超时
        if timeout and (current_time - start_time) > timeout:
            print(f"⏰ 超时: 在{timeout}秒内未检测到大纲状态变化")
            return False

        try:
            with Session(engine) as session:
                outline = session.exec(
                    select(Outline).where(Outline.task_id == task_id)
                ).first()

                if not outline:
                    # 大纲可能尚未生成，继续等待
                    if poll_count % 6 == 0:
                        print(f"   轮询 #{poll_count}: 大纲尚未生成，继续等待...")
                else:
                    status = outline.status

                    if poll_count % 6 == 0:
                        print(f"   轮询 #{poll_count}: 大纲状态={status}")

                    if status == OutlineStatus.CONFIRMED:
                        print(f"✅ 大纲状态已变为CONFIRMED!")
                        return True

                    # 如果大纲被归档，则失败
                    if status == OutlineStatus.ARCHIVED:
                        print(f"❌ 大纲已被归档，无法继续")
                        return False

        except Exception as e:
            print(f"⚠️  轮询大纲状态时出错: {e}")

        # 等待下一次轮询
        time.sleep(poll_interval)


def generate_story_tree(
    world_bible,
    width: int,
    depth: int,
    provider: str,
    task_id: str,
    global_style: str = None,
    visual_style_context: dict = None,
):
    """
    生成多分支故事树

    Args:
        world_bible: 世界圣经
        width: 分支宽度
        depth: 故事深度
        provider: LLM提供商
        task_id: 世界观/大纲的task_id

    Returns:
        tuple: (tree, session_dir)
    """
    import random

    # 使用 task_id 作为存档目录
    base_dir = Path("services/story_planner/storage/story_tree")
    session_dir = base_dir / str(task_id)
    print(f"📂 本次运行存档目录: {session_dir}")

    session_dir.mkdir(parents=True, exist_ok=True)

    generator = StoryGenerator(
        world_bible=world_bible,
        provider=provider,
        story_dir=str(session_dir),
        max_depth=depth,
        task_id=task_id
    )

    # 初始化或加载
    tree_file = generator.story_dir / "story_tree.json"
    if tree_file.exists():
        print("📂 加载现有剧情树...")
        tree = generator.load_existing_tree()
    else:
        print("🌱 初始化新剧情树...")
        tree = generator.initialize_story()

    root_id = tree.root_id

    # 开始循环生成
    for line_idx in range(width):
        print(f"\n📖 正在编写第 {line_idx + 1}/{width} 条世界线...")
        current_node_id = root_id
        current_node = tree.get_node(current_node_id)

        for step_idx in range(depth):
            print(f"  ⬇️  进度 {step_idx + 1}/{depth} (当前节点: {current_node_id})")

            if not current_node.children:
                if step_idx < depth - 1:
                    print("   ⚠️ 警告：中途断档，无后续选项")
                break

            # 智能选择策略
            unvisited_choices = []
            for i, child_choice in enumerate(current_node.children):
                if child_choice.child_id not in tree.nodes:
                    unvisited_choices.append(i)

            if unvisited_choices:
                if len(unvisited_choices) > 1:
                    choice_index = unvisited_choices[line_idx % len(unvisited_choices)]
                else:
                    choice_index = unvisited_choices[0]
                print(f"    🆕 发现 {len(unvisited_choices)} 个未生成选项，选择索引 {choice_index}")
            else:
                choice_index = random.randint(0, len(current_node.children) - 1)
                print(f"    🔄 所有 {len(current_node.children)} 个选项已生成，随机选择索引 {choice_index}")

            choice = current_node.children[choice_index]

            # 检查是否已存在
            if choice.child_id in tree.nodes:
                print(f"    🔗 剧情收束/复用: 跳转至现有节点 {choice.child_id}")
                current_node_id = choice.child_id
                current_node = tree.get_node(current_node_id)
            else:
                print(f"    ✍️  新剧情创作中...")
                try:
                    new_node = generator.continue_story(
                        current_node_id=current_node_id,
                        choice_index=choice_index
                    )
                    current_node_id = new_node.id
                    current_node = new_node
                    time.sleep(1)  # 避免 API 速率限制
                except Exception as e:
                    print(f"    ❌ 生成中断: {e}")
                    import traceback
                    traceback.print_exc()
                    break

    print("✅ 剧情树生成完毕。")
    # 剪枝无效链接
    tree.prune_invalid_links(max_depth=depth)
    # 保存剪枝后的剧情树到本地
    tree.save(str(session_dir / "story_tree.json"))
    print(f"📂 存档位置: {session_dir}")

    # 保存 JSON 到数据库
    try:
        from services.story_planner.table.story_tree_to_db import save_story_json_to_db
        from services.story_planner.core.ui_mode import infer_ui_mode
        import json

        json_path = session_dir / "story_tree.json"
        with open(json_path, 'r', encoding='utf-8') as f:
            json_data = json.load(f)

        config = _get_task_config(task_id)
        if global_style:
            json_data['global_style'] = global_style
        else:
            json_data.pop('global_style', None)
        if visual_style_context:
            json_data['visual_style_context'] = visual_style_context
        # game_builder 仍用 art_style 选择 UI 主题；图片提示词不读取它。
        json_data['art_style'] = config.get('art_style', 'ink_chinese')
        json_data['book_language'] = config.get('output_language')
        json_data.update(
            _build_story_tree_book_metadata(
                config,
                novel_filename=config.get("novel_filename"),
                task_id=task_id,
                narrative_arc=(
                    getattr(world_bible, "narrative_structure", {}) or {}
                ).get("inciting_incident", "")
                if isinstance(getattr(world_bible, "narrative_structure", {}), dict)
                else "",
            )
        )
        json_data['UI_mode'] = infer_ui_mode(
            existing=json_data.get('UI_mode'),
            metadata={"art_style": getattr(world_bible, "art_style", "")},
        )
        json_data = wrap_story_tree_language_bundles(
            json_data,
            build_story_tree_language_bundle(
                json_data,
                selected_language=config.get("output_language"),
                source_text="",
                provider=provider,
            ),
            default_language=get_story_tree_player_language(),
        )

        record = save_story_json_to_db(
            task_id=task_id,
            json_data=json_data,
            overwrite=True
        )
        if record:
            print(f"✅ 故事树JSON已保存到数据库: task_id={task_id}")
            # 自动删除本地文件
            try:
                import shutil
                if session_dir.exists():
                    shutil.rmtree(session_dir)
                    print(f"🗑️  已自动删除本地文件目录: {session_dir}")
            except Exception as e:
                print(f"⚠️  删除本地文件目录失败: {e}")
        else:
            print(f"⚠️  故事树JSON保存到数据库失败")
    except Exception as e:
        print(f"⚠️  保存故事树JSON到数据库失败: {e}")
        import traceback
        traceback.print_exc()

    return tree, session_dir
