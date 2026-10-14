"""Layered chapter context selection for bounded story-planner prompts."""

from __future__ import annotations

import re
from typing import Iterable


_DECISION_TERMS = (
    "决定", "选择", "拒绝", "坦白", "离开", "留下", "答应", "承认",
    "揭露", "隐瞒", "放弃", "背叛", "救", "杀", "公开", "治疗", "死亡",
)


def _tokens(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    tokens = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,4}", normalized))
    tokens.update(term for term in _DECISION_TERMS if term in normalized)
    return tokens


def select_relevant_chapter_summaries(
    summaries: Iterable[str],
    current_index: int,
    query: str = "",
    *,
    neighbor_radius: int = 2,
    remote_limit: int = 2,
) -> list[tuple[int, str]]:
    items = [str(item or "") for item in summaries]
    if not items:
        return []
    current = min(max(int(current_index), 0), len(items) - 1)
    local_indexes = set(
        range(max(0, current - neighbor_radius), min(len(items), current + neighbor_radius + 1))
    )
    query_tokens = _tokens(query or items[current])
    remote_candidates = []
    for index, summary in enumerate(items):
        if index in local_indexes:
            continue
        summary_tokens = _tokens(summary)
        overlap = len(query_tokens & summary_tokens)
        decision_score = sum(1 for term in _DECISION_TERMS if term in summary)
        score = overlap * 5 + decision_score
        if score > 0:
            remote_candidates.append((score, -abs(index - current), index))
    remote_candidates.sort(reverse=True)
    selected_remote = {item[2] for item in remote_candidates[: max(0, remote_limit)]}
    ordered = sorted(local_indexes) + sorted(selected_remote - local_indexes)
    return [(index, items[index]) for index in ordered]


def build_layered_summary_block(
    summaries: Iterable[str],
    current_index: int,
    query: str = "",
    *,
    neighbor_radius: int = 2,
    remote_limit: int = 2,
    max_chars: int = 2800,
) -> str:
    selected = select_relevant_chapter_summaries(
        summaries,
        current_index,
        query,
        neighbor_radius=neighbor_radius,
        remote_limit=remote_limit,
    )
    local_low = current_index - neighbor_radius
    local_high = current_index + neighbor_radius
    lines = []
    for index, summary in selected:
        label = "局部" if local_low <= index <= local_high else "相关历史决策召回"
        lines.append(f"【{label}·第{index + 1}章】{summary}")
    block = "## 分层章节上下文\n" + "\n".join(lines) + "\n\n"
    return block[:max_chars]
