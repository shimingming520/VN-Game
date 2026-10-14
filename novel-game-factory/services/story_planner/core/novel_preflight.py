"""Large-novel validation and workload estimation before expensive processing."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import os
from typing import Any, Mapping, Optional, Sequence
from services.story_planner.core.hierarchical_processing import HIERARCHICAL_SETTINGS


DEFAULT_MAX_NOVEL_CHARS = 1_500_000
DEFAULT_MAX_CHAPTERS = 3_000
DEFAULT_ESTIMATED_LLM_SECONDS_PER_CALL = 30
LARGE_NOVEL_WARNING_CHARS = 600_000
LONG_CHAPTER_WARNING_CHARS = 50_000
WORLDVIEW_SEGMENT_CHARS = HIERARCHICAL_SETTINGS.worldview_segment_chars
CHAPTER_SUMMARY_SEGMENT_CHARS = HIERARCHICAL_SETTINGS.analysis_segment_chars
MAX_REPLACEMENT_CHAR_RATIO = 0.01


class NovelPreflightError(ValueError):
    """Raised when input is unsafe or exceeds configured production limits."""


@dataclass(frozen=True)
class NovelPreflightLimits:
    max_novel_chars: int
    max_chapters: int
    estimated_llm_seconds_per_call: int


@dataclass(frozen=True)
class NovelPreflightReport:
    story_mode: str
    char_count: int
    utf8_bytes: int
    chapter_count: int
    average_chapter_chars: int
    max_chapter_chars: int
    worldview_segments: int
    chapter_summary_segments: int
    estimated_soft_branches: int
    estimated_output_nodes_min: int
    estimated_output_nodes_max: int
    estimated_llm_calls_min: int
    estimated_llm_calls_max: int
    estimated_runtime_minutes_min: int
    estimated_runtime_minutes_max: int
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["warnings"] = list(self.warnings)
        return data

    def format_text(self) -> str:
        lines = [
            "📊 小说处理预检完成",
            (
                f"   模式={self.story_mode}，字符={self.char_count}，"
                f"UTF-8={self.utf8_bytes} bytes，章节={self.chapter_count}"
            ),
            (
                f"   平均章长={self.average_chapter_chars}，最长章={self.max_chapter_chars}，"
                f"世界观分段={self.worldview_segments}，章节摘要分段={self.chapter_summary_segments}"
            ),
            (
                f"   预计输出节点={self.estimated_output_nodes_min}-"
                f"{self.estimated_output_nodes_max}，软分支≈{self.estimated_soft_branches}"
            ),
            (
                f"   预计 LLM 调用={self.estimated_llm_calls_min}-"
                f"{self.estimated_llm_calls_max} 次，预计耗时="
                f"{self.estimated_runtime_minutes_min}-"
                f"{self.estimated_runtime_minutes_max} 分钟"
            ),
        ]
        lines.extend(f"   ⚠️  {warning}" for warning in self.warnings)
        return "\n".join(lines)


def calibrate_chapter_branch_preflight(
    report: NovelPreflightReport,
    *,
    topology: Sequence[Mapping[str, Any]],
    branch_type: str,
    chapters: Optional[Sequence[str]] = None,
) -> NovelPreflightReport:
    """Recalculate workload after topology and branch type are known."""
    mode = str(branch_type or report.story_mode).lower()
    original_count = sum(1 for node in topology if node.get("is_original"))
    whatif_count = sum(
        1
        for node in topology
        if not node.get("is_original") and node.get("type") == "chapter"
    )
    if mode in {"fantasy", "death_corridor"}:
        soft_branches = 0
    elif chapters is not None:
        # Use the exact same chapter/scene policy as node generation once the
        # topology and source chapter texts are available.
        from services.story_planner.core.chapter_branch_runtime import (
            estimate_total_soft_branches,
        )

        soft_branches = estimate_total_soft_branches(
            topology,
            list(chapters),
            branch_type=mode,
        )
    else:
        # Backward-compatible approximation for callers that only have the
        # report and topology.  Production callers pass ``chapters``.
        soft_branches = max(0, original_count - 1) * 3 + whatif_count * 3
    output_nodes = len(list(topology))
    analysis_calls = report.estimated_llm_calls_min - report.estimated_soft_branches
    analysis_calls = max(1, analysis_calls)
    whatif_calls_min = whatif_count * 6
    whatif_calls_max = whatif_count * 10
    calls_min = analysis_calls + soft_branches + whatif_calls_min
    calls_max = (
        analysis_calls
        + soft_branches * 3
        + whatif_calls_max
        + max(6, report.chapter_count * 2)
    )
    seconds = resolve_preflight_limits().estimated_llm_seconds_per_call
    min_seconds = max(1, seconds // 2)
    max_seconds = max(1, math.ceil(seconds * 1.5))
    return NovelPreflightReport(
        story_mode=mode,
        char_count=report.char_count,
        utf8_bytes=report.utf8_bytes,
        chapter_count=report.chapter_count,
        average_chapter_chars=report.average_chapter_chars,
        max_chapter_chars=report.max_chapter_chars,
        worldview_segments=report.worldview_segments,
        chapter_summary_segments=report.chapter_summary_segments,
        estimated_soft_branches=soft_branches,
        estimated_output_nodes_min=output_nodes,
        estimated_output_nodes_max=output_nodes,
        estimated_llm_calls_min=calls_min,
        estimated_llm_calls_max=calls_max,
        estimated_runtime_minutes_min=max(1, math.ceil(calls_min * min_seconds / 60)),
        estimated_runtime_minutes_max=max(1, math.ceil(calls_max * max_seconds / 60)),
        warnings=report.warnings,
    )


def _positive_int(source: Mapping[str, str], key: str, default: int) -> int:
    try:
        value = int(source.get(key, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def resolve_preflight_limits(
    env: Optional[Mapping[str, str]] = None,
) -> NovelPreflightLimits:
    source = os.environ if env is None else env
    return NovelPreflightLimits(
        max_novel_chars=_positive_int(
            source,
            "STORY_PLANNER_MAX_NOVEL_CHARS",
            DEFAULT_MAX_NOVEL_CHARS,
        ),
        max_chapters=_positive_int(
            source,
            "STORY_PLANNER_MAX_CHAPTERS",
            DEFAULT_MAX_CHAPTERS,
        ),
        estimated_llm_seconds_per_call=_positive_int(
            source,
            "STORY_PLANNER_ESTIMATED_LLM_SECONDS_PER_CALL",
            DEFAULT_ESTIMATED_LLM_SECONDS_PER_CALL,
        ),
    )


def _segment_count(char_count: int, segment_chars: int) -> int:
    return max(1, math.ceil(char_count / segment_chars))


def _estimate_soft_branches(chapters: Sequence[str]) -> int:
    """Estimate initial workload with the chapter-level adaptive policy.

    The initial preflight runs before topology generation, so it cannot know
    the exact What-If pivots.  It budgets all non-ending source chapters using
    their own text and adds a conservative fallback-sized What-If allowance.
    The post-topology calibration replaces this estimate with exact node data.
    """
    chapter_texts = list(chapters or [])
    if len(chapter_texts) <= 1:
        return 0

    from services.story_planner.core.chapter_branch_runtime import (
        plan_original_chapter_scene_count,
        plan_whatif_budget,
    )

    original_branches = sum(
        max(
            plan_original_chapter_scene_count(
                "chapter",
                chapter_text=chapter_text,
            )
            - 1,
            0,
        )
        for chapter_text in chapter_texts[:-1]
    )
    average_chars = sum(len(chapter) for chapter in chapter_texts) // max(
        len(chapter_texts), 1
    )
    estimated_whatif_count = max(0, (len(chapter_texts) - 2) // 3)
    whatif_branches = estimated_whatif_count * plan_whatif_budget(
        average_chars
    )["soft_branch_count"]
    return original_branches + whatif_branches


def run_novel_preflight(
    text: str,
    chapters: Sequence[str],
    story_mode: str,
    limits: Optional[NovelPreflightLimits] = None,
) -> NovelPreflightReport:
    """Validate input and estimate processing volume without DB or LLM access."""
    limits = limits or resolve_preflight_limits()
    mode = str(getattr(story_mode, "value", story_mode) or "unknown").lower()
    raw_text = text or ""
    char_count = len(raw_text)
    if not raw_text.strip():
        raise NovelPreflightError("小说缺少有效正文，禁止进入处理流程")
    if char_count > limits.max_novel_chars:
        raise NovelPreflightError(
            f"小说字符数 {char_count} 超过上限 {limits.max_novel_chars}；"
            "请拆分输入或调高 STORY_PLANNER_MAX_NOVEL_CHARS"
        )

    chapter_lengths = [len(chapter or "") for chapter in chapters]
    chapter_count = len(chapter_lengths)
    if chapter_count == 0:
        raise NovelPreflightError("小说缺少有效章节，禁止进入处理流程")
    if chapter_count > limits.max_chapters:
        raise NovelPreflightError(
            f"小说章节数 {chapter_count} 超过上限 {limits.max_chapters}；"
            "请检查章节识别结果或调高 STORY_PLANNER_MAX_CHAPTERS"
        )

    replacement_count = raw_text.count("\ufffd")
    if replacement_count / max(char_count, 1) > MAX_REPLACEMENT_CHAR_RATIO:
        raise NovelPreflightError(
            f"文本包含过多解码替换字符：{replacement_count}/{char_count}，"
            "请确认原文件编码后重新上传"
        )

    average_chapter_chars = sum(chapter_lengths) // max(chapter_count, 1)
    max_chapter_chars = max(chapter_lengths, default=0)
    worldview_segments = _segment_count(char_count, WORLDVIEW_SEGMENT_CHARS)
    summary_segments_by_chapter = [
        _segment_count(length, CHAPTER_SUMMARY_SEGMENT_CHARS)
        for length in chapter_lengths
    ]
    chapter_summary_segments = sum(summary_segments_by_chapter)

    warnings = []
    if char_count >= LARGE_NOVEL_WARNING_CHARS:
        warnings.append("已达到 40–60 万字以上的大文本规模，将按长任务预算执行")
    if max_chapter_chars > LONG_CHAPTER_WARNING_CHARS:
        warnings.append(
            f"最长章节 {max_chapter_chars} 字，将在后续分层摘要阶段继续切分"
        )
    if chapter_count > 300:
        warnings.append(f"章节数较多（{chapter_count}），预计处理和落库时间显著增加")

    if mode == "original":
        estimated_soft_branches = 0
        output_nodes_min = chapter_count
        output_nodes_max = chapter_count
        llm_calls_min = 1
        llm_calls_max = 1
    else:
        estimated_soft_branches = _estimate_soft_branches(chapters)
        output_nodes_min = chapter_count + 2
        output_nodes_max = chapter_count + math.ceil(chapter_count * 0.25) + 6
        worldview_calls = worldview_segments + (1 if worldview_segments > 1 else 0)
        chapter_summary_calls = sum(
            segments + (1 if segments > 1 else 0)
            for segments in summary_segments_by_chapter
        )
        analysis_and_topology_calls = worldview_calls + chapter_summary_calls + 4
        llm_calls_min = analysis_and_topology_calls + estimated_soft_branches
        llm_calls_max = (
            analysis_and_topology_calls
            + estimated_soft_branches * 3
            + max(6, chapter_count * 2)
        )

    seconds_per_call = limits.estimated_llm_seconds_per_call
    min_seconds_per_call = max(1, seconds_per_call // 2)
    max_seconds_per_call = max(1, math.ceil(seconds_per_call * 1.5))
    runtime_min = max(1, math.ceil(llm_calls_min * min_seconds_per_call / 60))
    runtime_max = max(1, math.ceil(llm_calls_max * max_seconds_per_call / 60))

    return NovelPreflightReport(
        story_mode=mode,
        char_count=char_count,
        utf8_bytes=len(raw_text.encode("utf-8")),
        chapter_count=chapter_count,
        average_chapter_chars=average_chapter_chars,
        max_chapter_chars=max_chapter_chars,
        worldview_segments=worldview_segments,
        chapter_summary_segments=chapter_summary_segments,
        estimated_soft_branches=estimated_soft_branches,
        estimated_output_nodes_min=output_nodes_min,
        estimated_output_nodes_max=output_nodes_max,
        estimated_llm_calls_min=llm_calls_min,
        estimated_llm_calls_max=llm_calls_max,
        estimated_runtime_minutes_min=runtime_min,
        estimated_runtime_minutes_max=runtime_max,
        warnings=tuple(warnings),
    )
