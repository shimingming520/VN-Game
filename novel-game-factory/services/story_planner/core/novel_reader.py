"""novel_reader.py - 小说预分析服务（世界观 + 章节摘要 + 风格样本 + 角色卡）

供 chapter_branch 和 multi 模式共享使用。
"""
from __future__ import annotations
from dataclasses import dataclass, field
import json
import os
import re
from typing import List, Dict, Any, Sequence
from services.story_planner.core.llm_client import LLMClient, get_lang_instruction
from services.story_planner.core.hierarchical_processing import (
    DEFAULT_FALLBACK_MERGE_CHARS,
    HIERARCHICAL_SETTINGS,
    hierarchical_reduce,
    split_text_at_boundaries,
)
from services.story_planner.core.prompt_budget import (
    calculate_input_token_budget,
    estimate_prompt_tokens,
)


class ContentConsistencyRepairRequired(RuntimeError):
    """Signal that one generated node still needs checkpoint-level regeneration."""

    def __init__(self, message: str, *, content: str = "", issues: str = ""):
        super().__init__(message)
        self.content = str(content or "")
        self.issues = str(issues or "")


@dataclass
class NovelContext:
    """小说预分析结果，供所有生成函数使用"""
    worldview: Dict[str, Any] = field(default_factory=dict)
    chapter_summaries: List[str] = field(default_factory=list)
    style_samples: str = ""       # 从各章抽取的风格样本，约2000字
    char_cards: str = ""          # 格式化角色卡文本，直接嵌入 prompt
    relationship_map: str = ""    # 人物关系线文本
    world_rules: str = ""         # 世界观规则/设定（格式化文本）
    narrative_arc: str = ""       # 全书叙事弧线摘要
    persona_axes: List[Dict[str, Any]] = field(default_factory=list)  # 主角人格维度轴
    is_romance_oriented: bool = False                                  # 是否恋爱向
    factions_text: str = ""                                            # 势力/组织格式化文本
    hidden_threads_text: str = ""                                      # 隐线/伏笔格式化文本
    ending_dimensions: List[Dict[str, Any]] = field(default_factory=list)  # 结局矩阵骨架
    character_secrets_text: str = ""                                   # 所有角色秘密的可用弹药
    character_behavior_anchors: str = ""                               # 各章人物决策史 + 行为指纹，防穿帮用
    branch_type: str = "default"                                       # 自动检测的分支类型：default/death_corridor/romance/fantasy/modular
    branch_type_strategy_version: int = 0                               # 模式识别策略版本，用于刷新旧缓存
    narrative_pov_contract: Dict[str, Any] = field(default_factory=dict) # 正文生成与校验共用的叙事视角契约


# ---------------------------------------------------------------------------
# Narrative point-of-view contract
# ---------------------------------------------------------------------------
#
# POV is deliberately inferred without an additional LLM request.  It is a
# small, deterministic contract used by all prose writers and by their local
# quality gates.  Keeping this here (next to the rest of the novel analysis
# helpers) means cached analyses can carry the contract along with the
# worldview instead of asking every generator to rediscover it.
# Public so checkpoint/version builders can invalidate stale analysis outputs
# without duplicating a magic number in another module.
# Version 2 invalidates contracts created before the narrator-metadata and
# dialogue/转述 boundary fixes.  Keeping this version in the analysis-step
# identity also forces an in-flight/resumed run to recompute its POV contract.
NARRATIVE_POV_CONTRACT_VERSION = 2
_POV_CONTRACT_VERSION = NARRATIVE_POV_CONTRACT_VERSION
_POV_PERSPECTIVES = {
    "first_person",
    "third_person_limited",
    "third_person_omniscient",
    "mixed",
    "unknown",
}

_FIRST_PERSON_MARKERS = (
    "第一人称",
    "第一视角",
    "我视角",
    "first person",
    "first-person",
    "first_person",
    "1st person",
)
_THIRD_PERSON_MARKERS = (
    "第三人称",
    "第三视角",
    "third person",
    "third-person",
    "third_person",
)
_LIMITED_POV_MARKERS = (
    "限知",
    "有限视角",
    "贴近视角",
    "limited",
    "close third",
)
_OMNISCIENT_POV_MARKERS = (
    "全知",
    "上帝视角",
    "omniscient",
    "omnipresent",
)
_MIXED_POV_MARKERS = (
    "多视角",
    "混合视角",
    "视角切换",
    "切换视角",
    "multi-perspective",
    "multiple pov",
    "multiple perspectives",
    "mixed pov",
)

# Verbs that make a first-person pronoun a likely narrator subject.  A bare
# “我” can be a quoted/object reference; requiring a nearby action or mental
# verb greatly reduces false positives in third-person prose.
_POV_ACTION_WORDS = (
    "不知道",
    "不明白",
    "不记得",
    "想",
    "觉得",
    "感到",
    "意识到",
    "发现",
    "看见",
    "看到",
    "望见",
    "听见",
    "听到",
    "记得",
    "忘了",
    "相信",
    "希望",
    "害怕",
    "决定",
    "选择",
    "推开",
    "打开",
    "关上",
    "拿起",
    "放下",
    "抬头",
    "低头",
    "转身",
    "回头",
    "走",
    "跑",
    "站",
    "坐",
    "躺",
    "醒",
    "睡",
    "等",
    "来到",
    "离开",
    "进入",
    "回到",
    "问",
    "说",
    "告诉",
    "伸手",
    "盯着",
    "看着",
    "想着",
    "感觉",
    "明白",
    "承认",
    "接受",
    "拒绝",
    "继续",
    "开始",
    "准备",
    "能够",
    "无法",
    "要",
    "想要",
    "不想",
    "会",
    "能",
)
_POV_ACTION_PATTERN = "(?:" + "|".join(map(re.escape, _POV_ACTION_WORDS)) + ")"


def _pov_source_text(
    worldview: Dict[str, Any] | None,
    chapters: List[str] | None = None,
    style_samples: str = "",
    text: str | None = None,
) -> str:
    """Build a bounded text sample for deterministic POV inference."""

    parts: list[str] = []
    if isinstance(text, str) and text.strip():
        parts.append(text)
    if chapters:
        # Keep the beginning, middle and end of each chapter.  The contract is
        # a style-level property, so a bounded representative sample is enough
        # and avoids putting a whole long novel into memory twice.
        for chapter in chapters[:80]:
            value = str(chapter or "")
            if not value:
                continue
            if len(value) > 1800:
                value = value[:900] + "\n" + value[-900:]
            parts.append(value)
    if style_samples:
        parts.append(str(style_samples))
    if isinstance(worldview, dict):
        # Metadata is useful for explicit “叙述视角” declarations but should
        # not add the entire JSON character card to the prose statistics.
        for key in (
            "narrative_perspective",
            "narrative_pov",
            "pov",
            "style_notes",
            "narrative_style",
        ):
            value = worldview.get(key)
            if isinstance(value, (str, int, float)):
                parts.append(str(value))
    return "\n".join(parts)


def _pov_remove_dialogue_and_markup(value: str) -> str:
    """Remove dialogue/control labels before counting narrator pronouns."""

    source = str(value or "")
    # Chinese, Japanese and ASCII quote pairs.  Generated prose may contain a
    # multiline quotation, hence DOTALL is intentional.
    for pattern in (
        r"“[^”]*”",
        r"「[^」]*」",
        r"『[^』]*』",
        r"\"[^\"\n]*\"",
        r"‘[^’]*’",
        r"'[^'\n]*'",
    ):
        source = re.sub(pattern, " ", source, flags=re.DOTALL)
    # Unquoted letters/diary excerpts are common in imported novels.  They are
    # written in the character's own first person, but do not change the
    # surrounding narrator.  Treat the paragraph after an explicit written-
    # text marker as quoted material.  Requiring a marker plus a paragraph
    # boundary keeps ordinary prose such as ``他问我`` untouched, while the
    # bounded paragraph scope prevents a later real POV switch from being
    # silently hidden when the author separates it with a blank line.
    source = re.sub(
        r"(?:信中|信上|信纸上|来信中|书信中|日记中|日记里|日记本上|日记上)"
        r"(?:写着|写道|记着|记载|内容(?:是|为)|如下)?\s*[:：]"
        r".*?(?=\r?\n\s*\r?\n|$)",
        " ",
        source,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Headings and branch control markers are UI/control text rather than
    # narration.  Keep ordinary punctuation and line breaks for sentence
    # boundary detection.
    source = re.sub(r"^\s*#{1,6}[^\n]*$", " ", source, flags=re.MULTILINE)
    source = re.sub(r"【[^】]*(?:软分支|What[- ]?If|选择|选项|分支)[^】]*】", " ", source)
    source = re.sub(r"\[(?:soft[_ -]?branch|what[_ -]?if|choice)[^\]]*\]", " ", source, flags=re.IGNORECASE)
    # Markdown exports use explicit choice rows.  These are UI labels and may
    # intentionally be first-person even when the prose contract is third
    # person; never feed them into narrator statistics.
    source = re.sub(
        r"^\s*(?:▶\s*)?(?:选项|choice)\s*[A-D]?：?:?.*$",
        " ",
        source,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    source = re.sub(
        r"^\s*(?:▶\s*)?(?:选择|choice_[ab])\s*[:：].*$",
        " ",
        source,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    return source


def _pov_first_person_narration_hits(value: str) -> int:
    """Count likely first-person narrator subjects outside dialogue."""

    source = _pov_remove_dialogue_and_markup(value)
    hits = 0
    # Sentence/clause starts are stronger evidence than an object pronoun in
    # “他问我”。  Include comma boundaries because Chinese often chains two
    # independent clauses in one sentence.
    for match in re.finditer(
        r"(?:^|[。！？!?；;\n，,])\s*(我们|我)\s*([^。！？!?；;\n]{0,24})",
        source,
    ):
        tail = match.group(2) or ""
        if re.search(_POV_ACTION_PATTERN, tail):
            hits += 1
    return hits


def _pov_third_person_narration_hits(
    value: str,
    protagonist_name: str = "",
    protagonist_aliases: list[str] | None = None,
    *,
    include_pronouns: bool = True,
) -> int:
    """Count likely third-person references to the protagonist outside dialogue."""

    source = _pov_remove_dialogue_and_markup(value)
    hits = 0
    protagonist_terms = [
        str(term).strip()
        for term in [protagonist_name, *(protagonist_aliases or [])]
        if str(term).strip()
    ]
    if protagonist_terms:
        name_pattern = "(?:" + "|".join(re.escape(term) for term in protagonist_terms) + ")"
        # A named subject followed by an action/mental verb is strong evidence
        # that the prose is describing the protagonist in third person.
        hits += len(
            re.findall(
                rf"(?:^|[。！？!?；;\n])\s*{name_pattern}\s*(?:却|又|便|就|还|也|正在|已经|曾经)?\s*{_POV_ACTION_PATTERN}",
                source,
            )
        )
    # Pronouns are only counted at sentence/clause starts; object references
    # such as “他问我” must not make first-person prose fail the gate.
    if include_pronouns:
        hits += len(
            re.findall(
                rf"(?:^|[。！？!?；;\n，,])\s*(他|她|它)\s*(?:却|又|便|就|还|也|正在|已经|曾经)?\s*{_POV_ACTION_PATTERN}",
                source,
            )
        )
    return hits


def _pov_declared_metadata(worldview: Dict[str, Any]) -> list[tuple[str, str]]:
    """Return (source, value) pairs for explicit POV metadata."""

    pairs: list[tuple[str, str]] = []
    for key in (
        "narrative_perspective",
        "narrative_pov",
        "pov",
        "style_notes",
        "narrative_style",
    ):
        value = worldview.get(key)
        if isinstance(value, dict):
            for nested_key in ("perspective", "mode", "type", "description", "rule_text"):
                nested = value.get(nested_key)
                if nested:
                    pairs.append((f"{key}.{nested_key}", str(nested)))
        elif value:
            pairs.append((key, str(value)))
    # Do not treat a cached contract's rule text as fresh style evidence.
    # ``infer_narrative_perspective_contract`` handles a recognized, current
    # contract explicitly above; stale/unknown checkpoints must not be able to
    # override the current style metadata or chapter samples merely because
    # their old rule text mentions the opposite POV.
    return pairs


def _normalize_pov_label(value: str) -> str | None:
    """Normalize Chinese/English POV labels to the public enum."""

    text = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    if not text:
        return None
    def _marker_is_negated(marker: str) -> bool:
        """Whether a nearby negation says the marker is forbidden, not used."""
        marker_text = marker.lower()
        start = text.find(marker_text)
        if start < 0:
            return False
        prefix = text[max(0, start - 8):start]
        return any(
            negation in prefix
            for negation in ("不得", "禁止", "不要", "不能", "避免", "不可", "不允许", "不作", "不做")
        )

    if any(
        marker.lower() in text and not _marker_is_negated(marker)
        for marker in _MIXED_POV_MARKERS
    ):
        return "mixed"
    has_first = any(marker.lower() in text for marker in _FIRST_PERSON_MARKERS)
    has_third = any(marker.lower() in text for marker in _THIRD_PERSON_MARKERS)
    # Rule text often mentions the forbidden alternative (for example,
    # “第一人称……不得改用第三人称”).  In that case the positive declaration
    # must win instead of treating the sentence as an ambiguous mixed POV.
    if has_first and has_third:
        # A frequent style-note form is “第一人称叙述，角色对白偶尔使用
        # 第三人称转述”。  Both labels are present, but only the first one
        # describes the narrator.  Resolve this explicit local grammar before
        # falling back to the more general negation/position heuristics.
        first_narrative_decl = re.search(
            r"(?:第一人称|第一视角|我视角|first\s*person|first-person|first_person)"
            r"[^。！？!?；;\n]{0,12}(?:叙述|视角|旁白|narrat(?:ion|ive)|pov)",
            text,
            flags=re.IGNORECASE,
        )
        third_dialogue_decl = re.search(
            r"(?:对白|对话|台词|转述|引语|角色说|书信|日记)"
            r"[^。！？!?；;\n]{0,12}(?:第三人称|第三视角|third\s*person|third-person|third_person)"
            r"|(?:第三人称|第三视角|third\s*person|third-person|third_person)"
            r"[^。！？!?；;\n]{0,12}(?:对白|对话|台词|转述|引语|角色说|书信|日记)",
            text,
            flags=re.IGNORECASE,
        )
        third_narrative_decl = re.search(
            r"(?:第三人称|第三视角|third\s*person|third-person|third_person)"
            r"[^。！？!?；;\n]{0,12}(?:叙述|视角|旁白|narrat(?:ion|ive)|pov)",
            text,
            flags=re.IGNORECASE,
        )
        first_dialogue_decl = re.search(
            r"(?:对白|对话|台词|转述|引语|角色说|书信|日记)"
            r"[^。！？!?；;\n]{0,12}(?:第一人称|第一视角|我视角|first\s*person|first-person|first_person)"
            r"|(?:第一人称|第一视角|我视角|first\s*person|first-person|first_person)"
            r"[^。！？!?；;\n]{0,12}(?:对白|对话|台词|转述|引语|角色说|书信|日记)",
            text,
            flags=re.IGNORECASE,
        )
        if first_narrative_decl and third_dialogue_decl and not third_narrative_decl:
            return "first_person"
        if third_narrative_decl and first_dialogue_decl and not first_narrative_decl:
            return "third_person_limited"

        def _positions(markers: tuple[str, ...]) -> list[int]:
            positions: list[int] = []
            for marker in markers:
                start = 0
                needle = marker.lower()
                while True:
                    found = text.find(needle, start)
                    if found < 0:
                        break
                    positions.append(found)
                    start = found + max(len(needle), 1)
            return positions

        def _dialogue_context(position: int, marker: str) -> bool:
            # Style metadata often says e.g. “第一人称叙述，角色对白偶尔
            # 使用第三人称转述”。  The second marker describes quoted
            # dialogue, not the narrator.  Keep a small local window so a
            # later real declaration is not hidden by unrelated text.
            window = text[max(0, position - 20): position + len(marker) + 24]
            return any(
                token in window
                for token in ("对白", "对话", "台词", "转述", "引语", "角色说", "书信", "日记")
            )

        first_positions_all = _positions(_FIRST_PERSON_MARKERS)
        third_positions_all = _positions(_THIRD_PERSON_MARKERS)
        first_dialogue = any(
            _dialogue_context(pos, next(
                marker for marker in _FIRST_PERSON_MARKERS
                if text.find(marker.lower(), max(0, pos - len(marker))) == pos
            ))
            for pos in first_positions_all
        ) if first_positions_all else False
        third_dialogue = any(
            _dialogue_context(pos, next(
                marker for marker in _THIRD_PERSON_MARKERS
                if text.find(marker.lower(), max(0, pos - len(marker))) == pos
            ))
            for pos in third_positions_all
        ) if third_positions_all else False
        if first_positions_all and third_positions_all and first_dialogue != third_dialogue:
            # The marker that is not scoped to dialogue is the narrator
            # declaration.  This resolves common metadata phrasing without
            # weakening the explicit-negation handling below.
            if not first_dialogue and third_dialogue:
                return "first_person"
            if not third_dialogue and first_dialogue:
                return "third_person_limited"
        first_positions = [text.find(marker.lower()) for marker in _FIRST_PERSON_MARKERS if marker.lower() in text]
        third_positions = [text.find(marker.lower()) for marker in _THIRD_PERSON_MARKERS if marker.lower() in text]
        first_pos = min(first_positions) if first_positions else -1
        third_pos = min(third_positions) if third_positions else -1
        if first_pos >= 0 and third_pos > first_pos:
            between = text[first_pos:third_pos]
            if any(
                neg in between
                for neg in ("不得", "禁止", "不要", "不能", "避免", "不可", "不使用", "不采用", "不切换", "不允许")
            ):
                return "first_person"
        if third_pos >= 0 and first_pos > third_pos:
            between = text[third_pos:first_pos]
            if any(
                neg in between
                for neg in ("不得", "禁止", "不要", "不能", "避免", "不可", "不使用", "不采用", "不切换", "不允许")
            ):
                return "third_person_limited"
    if has_first and not has_third:
        return "first_person"
    if has_third:
        if any(marker.lower() in text for marker in _OMNISCIENT_POV_MARKERS):
            return "third_person_omniscient"
        if any(marker.lower() in text for marker in _LIMITED_POV_MARKERS):
            return "third_person_limited"
        # Generic “第三人称” defaults to limited: it is the safer contract
        # for generated prose because it prevents unearned mind-reading.
        return "third_person_limited"
    # Direct enum values are accepted even when no human-readable marker is
    # present (e.g. a checkpoint generated by a newer service version).
    enum_aliases = {
        "first person": "first_person",
        "third person limited": "third_person_limited",
        "third person omniscient": "third_person_omniscient",
        "mixed": "mixed",
        "unknown": "unknown",
    }
    return enum_aliases.get(text)


def _pov_protagonist(worldview: Dict[str, Any], metadata_text: str = "") -> tuple[str, list[str]]:
    """Find the canonical protagonist name and aliases from worldview data."""

    characters = worldview.get("characters") if isinstance(worldview, dict) else []
    candidates: list[dict[str, Any]] = []
    for character in characters or []:
        if not isinstance(character, dict) or not str(character.get("name") or "").strip():
            continue
        role = str(character.get("role") or "").lower()
        if (
            character.get("is_protagonist")
            or any(marker in role for marker in ("主角", "主人公", "男主", "女主", "protagonist", "hero"))
        ):
            candidates.append(character)

    # If an explicit “跟随/以 X 视角” declaration names a known character,
    # prefer it over list order.  This also handles multiple protagonists.
    names = [str(item.get("name") or "").strip() for item in candidates]
    for name in names:
        if name and re.search(rf"{re.escape(name)}\s*(?:的)?\s*(?:视角|叙述|所见|内心)", metadata_text):
            chosen = next(item for item in candidates if str(item.get("name")) == name)
            aliases = [str(alias).strip() for alias in (chosen.get("aliases") or []) if str(alias).strip()]
            return name, aliases
    if candidates:
        chosen = candidates[0]
        name = str(chosen.get("name") or "").strip()
        aliases = [str(alias).strip() for alias in (chosen.get("aliases") or []) if str(alias).strip()]
        return name, aliases

    # Legacy caches sometimes store the protagonist outside characters.
    protagonist = worldview.get("protagonist") if isinstance(worldview, dict) else None
    if isinstance(protagonist, dict):
        name = str(protagonist.get("name") or "").strip()
        aliases = [str(alias).strip() for alias in (protagonist.get("aliases") or []) if str(alias).strip()]
        if name:
            return name, aliases
    return "", []


def _pov_rule_text(perspective: str, protagonist_name: str, allow_switch: bool) -> str:
    """Build a concise Chinese rule block suitable for generation prompts."""

    name = protagonist_name or "主角"
    if perspective == "first_person":
        text = (
            f"保持{ name }的第一人称叙事；正文中的“我”只能指叙事主角，"
            "不得把主角长期改写为“他/她”或主角姓名。对白、书信和日记中的人称按原文保留。"
        )
    elif perspective == "third_person_omniscient":
        text = (
            f"保持围绕{ name }的第三人称全知叙事；使用角色姓名、他/她等第三人称表达，"
            "不得无铺垫切换成第一人称旁白。对白、书信和日记中的“我”可以保留。"
        )
    elif perspective == "third_person_limited":
        text = (
            f"保持围绕{ name }的第三人称限知叙事；正文叙述使用角色姓名、他/她等第三人称表达，"
            "不得把叙述者改成“我”。对白、书信和日记中的“我”可以保留。"
        )
    elif perspective == "mixed":
        text = (
            "原著允许有限的多视角/视角切换；每个场景必须明确跟随一个视角人物，"
            "不得在同一场景无标记切换第一、第三人称。对白、书信和日记中的人称按原文保留。"
        )
    else:
        text = "原著叙事视角尚未确定；优先模仿相邻原文，禁止在同一场景无理由切换人称。"
    if allow_switch and perspective not in {"mixed", "unknown"}:
        text += "原著存在章节级视角切换时，只能在明确的场景/章节边界切换。"
    return text


def infer_narrative_perspective_contract(
    worldview: Dict[str, Any] | None,
    chapters: List[str] | None = None,
    style_samples: str = "",
    text: str | None = None,
) -> Dict[str, Any]:
    """Infer a deterministic narrative POV contract from analysis data.

    The function intentionally accepts optional chapter/text samples so callers
    can use it both during fresh analysis and when refreshing a cached
    worldview.  Existing valid contracts are normalized and reused; no LLM
    request is made here.
    """

    source_worldview = worldview if isinstance(worldview, dict) else {}
    metadata_pairs = _pov_declared_metadata(source_worldview)
    metadata_text = "\n".join(value for _key, value in metadata_pairs)
    protagonist_name, aliases = _pov_protagonist(source_worldview, metadata_text)

    # Existing checkpoint contract is authoritative when it has a recognized
    # perspective.  This prevents a resume from changing behavior just because
    # a later heuristic sees a different sample window.
    existing = source_worldview.get("narrative_pov_contract")
    existing_perspective = (
        _normalize_pov_label(str(existing.get("perspective") or existing.get("mode") or ""))
        if isinstance(existing, dict)
        else None
    )
    perspective = existing_perspective
    evidence: list[str] = []
    confidence = 0.0
    try:
        existing_version = (
            int(existing.get("contract_version", 0) or 0)
            if isinstance(existing, dict)
            else 0
        )
    except (TypeError, ValueError):
        existing_version = 0
    if perspective and perspective != "unknown" and existing_version == _POV_CONTRACT_VERSION:
        try:
            confidence = float(existing.get("confidence") or 0.95) if isinstance(existing, dict) else 0.95
        except (TypeError, ValueError):
            confidence = 0.5
        if isinstance(existing, dict):
            evidence.extend(
                str(item) for item in (existing.get("evidence") or [])
                if str(item).strip()
            )
        # Do not append a resume/audit marker here.  The POV contract is part of
        # node-generation identity; mutating ``evidence`` on every resume would
        # change its digest and reopen every generated node for a local repair.
        evidence = list(dict.fromkeys(evidence))
    else:
        # Unknown/obsolete checkpoints must not outrank fresh metadata or text
        # evidence.  This is important when the POV inference rules evolve.
        perspective = None
        # Explicit metadata wins over statistical inference.  Prefer the first
        # field in the order above, but recognize an explicit mixed marker even
        # when style notes mention both first and third person as prohibitions.
        for key, value in metadata_pairs:
            normalized = _normalize_pov_label(value)
            # ``unknown`` is an explicit absence of evidence, not a usable
            # contract.  Do not let an old checkpoint's placeholder mask a
            # newer style declaration or chapter-sample inference.
            if normalized and normalized != "unknown":
                perspective = normalized
                confidence = 0.93 if key.startswith("narrative_pov_contract") else 0.88
                evidence.append(f"{key} 声明：{value[:160]}")
                break

    prose_source = _pov_remove_dialogue_and_markup("\n".join(
        [str(item) for item in (chapters or [])] if chapters else [text or style_samples]
    ))
    first_hits = _pov_first_person_narration_hits(prose_source)
    third_hits = _pov_third_person_narration_hits(
        prose_source,
        protagonist_name,
        aliases,
        include_pronouns=not bool(protagonist_name),
    )
    if not perspective or perspective == "unknown":
        if first_hits >= 2 and first_hits >= third_hits + 1:
            perspective = "first_person"
            confidence = min(0.82, 0.55 + first_hits * 0.04)
            evidence.append(f"正文样本检测到 {first_hits} 处第一人称叙述动作")
        elif third_hits >= 2 and third_hits >= first_hits + 1:
            perspective = "third_person_limited"
            confidence = min(0.82, 0.55 + third_hits * 0.04)
            evidence.append(f"正文样本检测到 {third_hits} 处第三人称叙述动作")
        elif first_hits or third_hits:
            perspective = "mixed"
            confidence = 0.55
            evidence.append(f"正文样本人称信号混合（第一人称={first_hits}，第三人称={third_hits}）")
        else:
            perspective = "unknown"
            confidence = 0.25
            evidence.append("没有足够的旁白人称证据")

    allow_switch = perspective == "mixed" or any(
        marker.lower() in metadata_text.lower() for marker in _MIXED_POV_MARKERS
    )
    narrator_pronouns = (
        ["我", "我们"]
        if perspective == "first_person"
        else ["他", "她", "它"]
        if perspective in {"third_person_limited", "third_person_omniscient"}
        else ["视角人物"]
    )
    rule_text = _pov_rule_text(perspective, protagonist_name, allow_switch)
    return {
        "contract_version": _POV_CONTRACT_VERSION,
        "perspective": perspective if perspective in _POV_PERSPECTIVES else "unknown",
        # ``mode`` and ``pov_character`` are compatibility aliases for prompt
        # adapters owned by other modules.
        "mode": perspective if perspective in _POV_PERSPECTIVES else "unknown",
        "protagonist_name": protagonist_name,
        "pov_character": protagonist_name,
        "pov_character_aliases": aliases,
        "narrator_pronouns": narrator_pronouns,
        "allow_switch": bool(allow_switch),
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "evidence": evidence[:8],
        "rule_text": rule_text,
        "stats": {"first_person_narration_hits": first_hits, "third_person_narration_hits": third_hits},
    }


def detect_narrative_pov_drift(
    text: str,
    contract: Dict[str, Any] | None,
) -> list[str]:
    """Return deterministic POV drift issues for generated prose.

    Dialogue, headings and branch controls are excluded.  A single strong
    first-person narrator subject is enough to flag a third-person contract;
    third-person references must persist (two or more) before a first-person
    contract is rejected, avoiding false positives from an occasional name in
    a first-person paragraph.
    """

    if not isinstance(contract, dict):
        return []
    perspective = _normalize_pov_label(
        str(contract.get("perspective") or contract.get("mode") or contract.get("pov") or "")
    ) or "unknown"
    if perspective in {"unknown", "mixed"}:
        return []
    source = str(text or "")
    first_hits = _pov_first_person_narration_hits(source)
    protagonist_name = str(
        contract.get("protagonist_name") or contract.get("pov_character") or ""
    ).strip()
    aliases = contract.get("pov_character_aliases") or contract.get("protagonist_aliases") or []
    if not isinstance(aliases, list):
        aliases = []
    third_hits = _pov_third_person_narration_hits(
        source,
        protagonist_name,
        [str(item) for item in aliases],
        include_pronouns=not bool(protagonist_name),
    )
    issues: list[str] = []
    if perspective in {"third_person_limited", "third_person_omniscient"} and first_hits:
        label = "第三人称限知" if perspective == "third_person_limited" else "第三人称全知"
        issues.append(
            f"pov_drift：契约为{label}，正文检测到 {first_hits} 处第一人称旁白动作（“我/我们”）；"
            "对白、书信和日记除外"
        )
    elif perspective == "first_person" and third_hits >= 2:
        issues.append(
            f"pov_drift：契约为第一人称，正文检测到 {third_hits} 处持续第三人称主角叙述；"
            "请保持“我”作为叙事主体"
        )
    return issues


def infer_narrative_reality_contract(worldview: Dict[str, Any]) -> Dict[str, Any]:
    """Infer one canonical outer reality contract for all chapter state scans."""
    worldview = worldview if isinstance(worldview, dict) else {}
    existing = worldview.get("narrative_reality_contract")

    import json

    source = json.dumps(worldview, ensure_ascii=False).lower()
    if any(marker in source for marker in ("治疗重构", "治疗重建", "therapy_reconstruction")):
        domain = "reconstructed_world"
        reconstruction_type = "therapy_reconstruction"
    elif any(marker in source for marker in ("梦境", "梦中", "dream")):
        domain = "reconstructed_world"
        reconstruction_type = "dream"
    elif any(marker in source for marker in ("重生", "回到过去", "rebirth")):
        domain = "reconstructed_world"
        reconstruction_type = "rebirth"
    else:
        domain = "physical_reality"
        reconstruction_type = "none"

    compatible = [reconstruction_type]
    if domain == "reconstructed_world":
        compatible = ["dream", "therapy_reconstruction", "rebirth", "unknown"]
    resurrection_markers = [
        marker
        for marker in (
            "死而复生",
            "起死回生",
            "还魂",
            "复活",
            "重塑肉身",
            "招魂归体",
        )
        if marker in source
    ]
    resurrection_forbidden = any(
        marker in source
        for marker in (
            "死亡不可逆",
            "死者不可复生",
            "不能复活",
            "无法复活",
            "禁止复活",
        )
    )
    inferred = {
        "primary_reality_domain": domain,
        "primary_reconstruction_type": reconstruction_type,
        "compatible_reconstruction_types": compatible,
        "allow_physical_resurrection": bool(resurrection_markers)
        and not resurrection_forbidden,
        "resurrection_rule_markers": resurrection_markers,
    }
    if isinstance(existing, dict) and existing.get("primary_reality_domain"):
        inferred.update(existing)
        inferred.setdefault(
            "allow_physical_resurrection",
            bool(resurrection_markers) and not resurrection_forbidden,
        )
        inferred.setdefault("resurrection_rule_markers", resurrection_markers)
    return inferred


def _extract_style_samples(chapters: list) -> str:
    """
    从各章各抽一个含对话的典型段落，拼成风格样本，总长度约2000字。
    """
    samples = []
    budget = 2000
    per_chapter = max(100, budget // max(len(chapters), 1))

    for ch in chapters:
        paras = [p.strip() for p in ch.split('\n\n') if p.strip()]
        chosen = ""
        for p in paras:
            if any(c in p for c in ['「', '」', '\"', '\u201c', '\u201d', '：', ':']):
                chosen = p[:per_chapter]
                break
        if not chosen and paras:
            chosen = paras[0][:per_chapter]
        if chosen:
            samples.append(chosen)

    return "\n\n---\n\n".join(samples)[:2000]


def _extract_style_samples_llm(chapters: list, provider: str, output_language: str = None) -> str:
    """
    LLM 精选 3-5 段最典型的风格代表段落（情感戏 + 动作戏 + 对话戏 各一），
    总长度约 2000-3500 字。失败时回退到 _extract_style_samples。
    """
    if not chapters:
        return ""
    sampling = []
    per_ch_budget = 4000
    for i, ch in enumerate(chapters):
        if len(ch) <= per_ch_budget:
            sampling.append(f"【第{i+1}章节选】\n{ch.strip()}")
        else:
            head = ch[:1300].strip()
            mid_start = (len(ch) - 1300) // 2
            mid = ch[mid_start:mid_start + 1300].strip()
            tail = ch[-1300:].strip()
            sampling.append(f"【第{i+1}章首段】\n{head}\n\n【第{i+1}章中段】\n{mid}\n\n【第{i+1}章末段】\n{tail}")
    full_sampling = "\n\n========\n\n".join(sampling)
    if len(full_sampling) > 60000:
        full_sampling = full_sampling[:60000] + "\n\n[内容过长，已截断]"

    lang_instr = get_lang_instruction(output_language)
    system_prompt = (
        "你是文学风格分析师。从原作中精选 3-5 段最能代表本作风格的『风格样本』，"
        "供后续 LLM 模仿写作时作风格锚定使用。\n"
        "选择标准：\n"
        "  ① 情感戏 1 段（人物内心独白或情绪爆发，体现心理描写习惯）\n"
        "  ② 动作戏/场景戏 1 段（紧张段落，体现节奏和画面切换）\n"
        "  ③ 对话戏 1 段（多角色对话，体现说话方式和潜台词运用）\n"
        "  ④（可选）特色段落 1-2 段（环境描写/超自然/打斗等本作独有的风格段）\n"
        "每段必须是原文的精确截取，不得修改一字。\n"
        "输出严格 JSON：{\"samples\": [{\"type\": \"情感/动作/对话/特色\", \"excerpt\": \"原文精确截取，400-600字\"}, ...]}"
        + ("\n" + lang_instr if lang_instr else "")
    )
    user_prompt = (
        f"以下是原作各章节选（按章节排列）：\n\n{full_sampling}\n\n"
        "请精选 3-5 段最典型的风格代表段落，按上述标准输出 JSON。"
    )
    try:
        client = LLMClient(provider=provider)
        result = client.chat_completion_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=4000,
        )
        samples = result.get("samples", [])
        if not isinstance(samples, list) or not samples:
            print("   ⚠️  LLM 风格精选返回空，使用 fallback")
            return _extract_style_samples(chapters)
        lines = []
        for s in samples:
            if not isinstance(s, dict):
                continue
            t = s.get("type", "样本")
            ex = s.get("excerpt", "").strip()
            if ex:
                lines.append(f"【{t}】\n{ex}")
        joined = "\n\n---\n\n".join(lines)
        if len(joined) < 500:
            print("   ⚠️  LLM 风格精选过短，使用 fallback")
            return _extract_style_samples(chapters)
        return joined[:3500]
    except Exception as e:
        print(f"   ⚠️  LLM 风格精选失败（{e}），使用 fallback")
        return _extract_style_samples(chapters)


# 单次送给 LLM 的最大字数（留足 prompt+输出余量）
_WORLDVIEW_SEG_SIZE = HIERARCHICAL_SETTINGS.worldview_segment_chars

_WORLDVIEW_FORMAT = (
    '{\n'
    '  "title": "小说标题（从内容推断）",\n'
    '  "genre": "类型（仙侠/现代/科幻/悬疑等）",\n'
    '  "is_romance_oriented": "布尔值。true 当且仅当同一主角在同一可选择阶段有≥2位明确的恋爱对象，且偏向谁会形成贯穿主要章节的不同角色路线；不同主角、前世今生、已错过或已死亡人物不得合并计数；其余情况一律 false",\n'
    '  "romance_targets": [\n'
    '    "（仅当 is_romance_oriented=true 时填，否则置 []）",\n'
    '    {\n'
    '      "name": "恋爱对象名（含中文/原名）",\n'
    '      "role_label": "女1/女2/男1 等顺位标签",\n'
    '      "affection_arc": "与主角情感发展轨迹（30字以内）",\n'
    '      "key_scenes": ["关键互动场景，3-5条，每条 ≤25 字"],\n'
    '      "fatal_choice_moment": "主角对此人最关键的一次抉择时刻"\n'
    '    }\n'
    '  ],\n'
    '  "world_rules": [\n'
    '    "（数组形式，3-6 条，每条对应一项世界级规则/体系/限制）",\n'
    '    {\n'
    '      "rule_name": "规则名称（如：修炼境界体系/赛博义体规则/时空法则）",\n'
    '      "description": "完整描述（不少于30字）",\n'
    '      "implications": "此规则对人物行为的限制/可能性（不少于20字）"\n'
    '    }\n'
    '  ],\n'
    '  "factions": [\n'
    '    "（势力/组织/门派/帮会等，2-5 条；若原作无明显势力则填 []）",\n'
    '    {\n'
    '      "name": "势力名",\n'
    '      "ideology": "核心理念/目标",\n'
    '      "key_members": ["主要成员名"],\n'
    '      "conflicts_with": ["敌对势力名"],\n'
    '      "leverage_on_protagonist": "此势力对主角的具体压力/吸引力"\n'
    '    }\n'
    '  ],\n'
    '  "narrative_arc": "全书叙事弧线（200-300字，必须含起/承/转/合 四阶段，每阶段一句）",\n'
    '  "characters": [\n'
    '    "（提取所有有姓名/称谓的角色，主角必有，配角至少3-8人）",\n'
    '    {\n'
    '      "name": "角色名",\n'
    '      "aliases": ["同一角色使用过的其他姓名、化名、称号；没有则 []，不得把不同人物合并"],\n'
    '      "role": "主角/重要配角/反派/路人甲等",\n'
    '      "appearance": "外貌特征（衣着/标志性身体特征，30字以内）",\n'
    '      "background": "出身/过往关键经历/伤痛记忆（50字以内）",\n'
    '      "personality": "性格关键词 3-5 个",\n'
    '      "speech_style": "说话方式（如：简短直接/文绉绉/爱用反问/带方言）",\n'
    '      "core_motivation": "核心驱动力（这个角色在全书的最终目标）",\n'
    '      "fatal_flaw": "致命弱点/软肋（最容易被攻破或牺牲的东西）",\n'
    '      "secrets": ["隐藏的秘密 0-3 条，每条≤30字（若无则 []）"],\n'
    '      "arc": "角色成长弧线（1-2 句话）",\n'
    '      "key_relations": ["与XX的关系：简述"]\n'
    '    }\n'
    '  ],\n'
    '  "relationship_map": [\n'
    '    "（人物关系网，覆盖主角与所有重要配角的关系）",\n'
    '    {\n'
    '      "from": "角色A",\n'
    '      "to": "角色B",\n'
    '      "type": "关系类型（夫妻/师徒/血仇/羁绊/利益等）",\n'
    '      "evolution": "关系变化轨迹（开局→结局）",\n'
    '      "tension_source": "关系张力的核心来源（一句具体描述）",\n'
    '      "leverage": "一方对另一方的把柄/筹码/依赖（若无则填 \\"无\\"）"\n'
    '    }\n'
    '  ],\n'
    '  "key_locations": [\n'
    '    {"name": "地点名", "description": "简介（10-30 字）", "significance": "对剧情的意义"}\n'
    '  ],\n'
    '  "key_events": [\n'
    '    "（按时序排列的关键事件，至少 6-12 条，每条 30-50 字，必须涵盖：起因/转折/高潮/收束）"\n'
    '  ],\n'
    '  "hidden_threads": [\n'
    '    "（隐线/伏笔，3-6 条；若原作无伏笔则 []）",\n'
    '    {\n'
    '      "thread": "伏笔内容（如：神秘玉佩的来历）",\n'
    '      "introduced_at": "首次出现位置（如：第1章/序章）",\n'
    '      "resolved_at": "回收位置（如：第7章/未回收）",\n'
    '      "implication": "若不揭穿会怎样，揭穿后改变了什么"\n'
    '    }\n'
    '  ],\n'
    '  "tone": "整体基调（不少于 20 字，如：压抑悲壮中带有对人性微光的执着）",\n'
    '  "style_notes": "语言风格要点（句式特点/常用修辞/叙述视角/对话密度，不少于 30 字）",\n'
    '  "persona_axes": [\n'
    '    "（主角人格维度轴，2-3 条；用哪种模板由 is_romance_oriented 决定）",\n'
    '    "若 is_romance_oriented=true：用 affection_<对象拼音> 格式，每个 romance_targets 对应一条",\n'
    '    "若 is_romance_oriented=false：用人格英文 key（如 courage/calmness/humanity/clarity/obsession 等），从原作主角真实面对的内心张力中选 2-3 条",\n'
    '    {\n'
    '      "key": "snake_case 变量名",\n'
    '      "name": "维度中文名（2-4 字）",\n'
    '      "low_desc": "低分时主角的具象状态（10 字以内）",\n'
    '      "high_desc": "高分时主角的具象状态（10 字以内）",\n'
    '      "ending_hint": "此维度高分时主角会走向哪种结局（1 句话，必须具体）"\n'
    '    }\n'
    '  ],\n'
    '  "ending_dimensions": [\n'
    '    "（结局可拆分的命运维度，至少 2-3 条，用于设计结局矩阵）",\n'
    '    {"dimension": "维度名（如：主角生死/复仇成败/关键配角生死/与某人结局）", "options": ["可能选项 1", "可能选项 2", ...]}\n'
    '  ]\n'
    '}'
)

# The full schema is useful when there is ample context, but it is too large
# to repeat verbatim for every merge batch.  Small-budget callers use this
# compact contract instead of cutting the prompt in the middle of JSON.
_WORLDVIEW_COMPACT_FORMAT = (
    '{"title":"...","genre":"...","is_romance_oriented":false,'
    '"romance_targets":[],"world_rules":[],"factions":[],"narrative_arc":"...",'
    '"characters":[],"relationship_map":[],"key_locations":[],"key_events":[],'
    '"hidden_threads":[],"tone":"...","style_notes":"...",'
    '"persona_axes":[],"ending_dimensions":[]}'
)

_WORLDVIEW_FIELD_PRIORITY = (
    "source_segment",
    "title",
    "genre",
    "is_romance_oriented",
    "characters",
    "romance_targets",
    "world_rules",
    "factions",
    "narrative_arc",
    "relationship_map",
    "key_locations",
    "key_events",
    "hidden_threads",
    "tone",
    "style_notes",
    "persona_axes",
    "ending_dimensions",
)


def _worldview_payload_dict(item: Any) -> dict[str, Any]:
    """Parse one merge item without ever slicing its serialized JSON."""
    if isinstance(item, dict):
        return dict(item)
    try:
        parsed = json.loads(str(item or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"source_text": str(item or "")}
    return dict(parsed) if isinstance(parsed, dict) else {"source_text": str(item or "")}


def _compact_worldview_value(value: Any, char_budget: int, depth: int = 0) -> Any:
    """Compact a JSON value at object/list boundaries, preserving its shape."""
    if char_budget <= 0:
        return ""
    if isinstance(value, str):
        if len(value) <= char_budget:
            return value
        # Keep both ends: introductions often contain definitions while the
        # end commonly contains consequences, resolutions, or source notes.
        head = max(1, int(char_budget * 0.62))
        tail = max(1, char_budget - head - 1)
        return value[:head] + "…" + (value[-tail:] if tail else "")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 4:
        text = str(value)
        return text if len(text) <= char_budget else text[: max(1, char_budget - 1)] + "…"
    if isinstance(value, list):
        if not value:
            return []
        # Preserve first and last entries so later events/resolutions are not
        # lost when a segment contains a long chronological list.
        limit = min(len(value), 12)
        indexes = list(range(limit))
        if len(value) > limit:
            tail_count = max(1, limit // 3)
            indexes = list(range(limit - tail_count)) + list(range(len(value) - tail_count, len(value)))
        per_item = max(16, char_budget // max(1, len(indexes)))
        result = [_compact_worldview_value(value[i], per_item, depth + 1) for i in indexes]
        if len(value) > len(indexes):
            result.insert(len(result) // 2, f"…其余 {len(value) - len(indexes)} 项已压缩…")
        return result
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        keys = [key for key in _WORLDVIEW_FIELD_PRIORITY if key in value]
        keys.extend(key for key in value if key not in keys)
        per_field = max(24, char_budget // max(1, min(len(keys), 18)))
        for key in keys:
            result[str(key)] = _compact_worldview_value(value[key], per_field, depth + 1)
        return result
    return _compact_worldview_value(str(value), char_budget, depth + 1)


def _compact_worldview_payload(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Return a bounded, valid worldview object while retaining source traceability."""
    source = _worldview_payload_dict(payload)
    if max_chars <= 0:
        return {"source_segment": source.get("source_segment", "")}
    # Reserve a little room for braces/quotes and iteratively tighten the
    # field-level budget until the serialized object is inside the limit.
    candidate = source
    budget = max(64, int(max_chars))
    for _ in range(8):
        candidate = _compact_worldview_value(source, budget)
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= max_chars:
            return candidate
        budget = max(32, int(budget * max_chars / max(len(encoded), 1) * 0.92))
    # Last resort is still valid JSON and keeps the segment identifier for
    # diagnostics and cross-segment deduplication.
    minimal: dict[str, Any] = {"source_segment": str(source.get("source_segment") or "")}
    for key in ("title", "genre"):
        value = str(source.get(key) or "")
        if not value:
            continue
        trial = dict(minimal)
        trial[key] = value[: max(1, max_chars // 4)]
        if len(json.dumps(trial, ensure_ascii=False, separators=(",", ":"))) <= max_chars:
            minimal = trial
    encoded = json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= max_chars:
        return minimal
    # A valid JSON object has a non-zero structural overhead.  Retain the
    # source marker even when the caller supplied an unrealistically tiny
    # budget; normal production budgets are comfortably above this floor.
    return {"source_segment": str(source.get("source_segment") or "")[: max(0, max_chars - 22)]}


def _fallback_merge_worldview_payloads(items: Sequence[Any]) -> dict[str, Any]:
    """Merge structured worldview payloads deterministically when LLM is unavailable."""
    payloads = [_worldview_payload_dict(item) for item in items]
    payloads = [payload for payload in payloads if payload]
    if not payloads:
        return {}
    merged: dict[str, Any] = {}
    source_segments = [
        str(payload.get("source_segment") or "").strip()
        for payload in payloads
        if str(payload.get("source_segment") or "").strip()
    ]
    if source_segments:
        merged["source_segments"] = list(dict.fromkeys(source_segments))
    list_fields = {
        "characters",
        "romance_targets",
        "world_rules",
        "factions",
        "relationship_map",
        "key_locations",
        "key_events",
        "hidden_threads",
        "persona_axes",
        "ending_dimensions",
    }
    for key in _WORLDVIEW_FIELD_PRIORITY:
        if key == "source_segment":
            continue
        values = [payload.get(key) for payload in payloads if key in payload]
        if not values:
            continue
        if key in list_fields:
            combined: list[Any] = []
            seen: set[str] = set()
            for value in values:
                candidates = value if isinstance(value, list) else [value]
                for candidate in candidates:
                    marker = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
                    if marker not in seen:
                        seen.add(marker)
                        combined.append(candidate)
            merged[key] = combined
        else:
            merged[key] = next((value for value in values if value not in (None, "")), values[0])
    return merged


def _worldview_merge_prompt_budget(max_output_tokens: int = 6000) -> int:
    return calculate_input_token_budget(max_output_tokens)


def _build_worldview_merge_prompt(
    group: Sequence[Any],
    level: int,
    batch_index: int,
    max_prompt_tokens: int,
) -> str:
    """Build a bounded merge prompt using structured, field-aware compaction."""
    values = [_worldview_payload_dict(item) for item in group if item is not None]
    values = values or [{}]
    prefix = (
        f"以下是同一部小说的世界观提取结果，第{level + 1}层第{batch_index + 1}批，"
        f"本批共{len(values)}份。请综合合并为一份完整、去重、无矛盾的世界观档案。\n"
        "只依据输入事实；同一角色的本名、化名、称号合并为一个角色；保留 source_segment 便于追溯。\n"
    )
    # Avoid spending the whole budget on the schema itself for small test or
    # deployment budgets; production-sized budgets retain the detailed schema.
    detailed_format = estimate_prompt_tokens(_WORLDVIEW_FORMAT) <= max_prompt_tokens * 0.38
    format_text = _WORLDVIEW_FORMAT if detailed_format else _WORLDVIEW_COMPACT_FORMAT
    suffix = f"\n\n输出格式：\n{format_text}"
    fixed_tokens = estimate_prompt_tokens(prefix) + estimate_prompt_tokens(suffix)
    available_tokens = max(32, max_prompt_tokens - fixed_tokens)
    per_item_tokens = max(24, available_tokens // len(values))
    per_item_chars = max(48, int(per_item_tokens * 0.72))
    compacted = [_compact_worldview_payload(value, per_item_chars) for value in values]

    def render(items: Sequence[dict[str, Any]]) -> str:
        return prefix + "\n---\n".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in items
        ) + suffix

    prompt = render(compacted)
    # Tighten only complete payloads; never invoke the generic text trimmer,
    # which can cut a JSON object or schema instruction in half.
    for _ in range(10):
        if estimate_prompt_tokens(prompt) <= max_prompt_tokens:
            return prompt
        per_item_chars = max(32, int(per_item_chars * 0.82))
        compacted = [_compact_worldview_payload(value, per_item_chars) for value in values]
        prompt = render(compacted)
    return prompt


def _group_worldview_merge_items(
    items: Sequence[Any],
    max_batch_size: int,
    max_prompt_tokens: int,
) -> list[list[Any]]:
    """Group merge inputs by both item count and the rendered prompt budget."""
    if max_batch_size < 1:
        raise ValueError("max_batch_size must be positive")
    groups: list[list[Any]] = []
    current: list[Any] = []
    for item in items:
        candidate = current + [item]
        too_many = len(candidate) > max_batch_size
        too_large = False
        if not too_many:
            # Keep a fidelity reserve for the raw facts.  The prompt builder
            # can compact fields to stay within the hard limit, but grouping
            # solely on the compacted result would allow five large payloads
            # to collapse into a tiny, low-information prompt.  Splitting
            # around 60% of the budget preserves room for the merge schema and
            # makes the grouping decision stable across retries.
            raw_tokens = sum(
                estimate_prompt_tokens(
                    json.dumps(_worldview_payload_dict(value), ensure_ascii=False)
                )
                for value in candidate
            )
            too_large = raw_tokens > max(64, int(max_prompt_tokens * 0.60))
        if not too_many and not too_large:
            too_large = estimate_prompt_tokens(
                _build_worldview_merge_prompt(candidate, 0, len(groups), max_prompt_tokens)
            ) > max_prompt_tokens
        if current and (too_many or too_large):
            groups.append(current)
            current = [item]
        else:
            current = candidate
    if current:
        groups.append(current)
    return groups


def _analyze_worldview(full_text: str, provider: str, output_language: str = None) -> Dict[str, Any]:
    """
    全量通读全文提取世界观、角色、关系网络。
    超过 _WORLDVIEW_SEG_SIZE 字时，分段逐段提取，再合并成最终档案。
    确保每个字都被 LLM 读到，不跳过任何段落。
    """
    import json as _json
    client = LLMClient(provider=provider)
    total = len(full_text)
    lang_instr = get_lang_instruction(output_language)
    system_prompt = (
        "你是顶级文学分析师 + 命运编织者，任务是从小说原文中榨取一份**完整无遗漏**的世界观档案，"
        "供视觉小说改编使用。你的产出会直接驱动 What-If 分支、软分支选项、平行结局——"
        "因此你必须像剥茧抽丝一样把原作的『弹药库』全部摆上桌：人物软肋、关系张力、势力压制、隐线伏笔、命运维度。\n\n"
        "【绝对铁律】\n"
        "1. 严格基于原文事实，禁止虚构任何未出现的内容；信息不足的字段宁可填 \"未明示\" 也不要编造。\n"
        "2. 不放过任何关键细节——尤其是：①每个角色的『软肋』和『秘密』；②人物之间的『张力来源』和『把柄』；"
        "   ③推动剧情但未点明的『隐线』；④可拆出多结局的『命运维度』。这四项是后续生成的命脉。\n"
        "3. 主动判定 is_romance_oriented：原作必须有≥2位明确恋爱对象、且情感发展是核心驱动之一时，才记为 true。\n"
        "   仅有暧昧/单恋/CP 副线 ≠ 恋爱向；恋爱向意味着主角的最终结局会因『偏心谁』而显著不同。\n"
        "4. persona_axes 必须严格按 is_romance_oriented 选模板：\n"
        "   - true → 每个 romance_targets 对应一条 affection_<对象拼音> 轴；\n"
        "   - false → 选 2-3 条人格轴（courage/calmness/humanity/clarity/obsession/justice/cruelty 等英文 key），"
        "     必须是原作主角真正面对过的内心张力，不要套通用模板。\n"
        "5. ending_dimensions 是结局矩阵的骨架：必须从原作真实存在的命运张力出发，至少 2-3 条不重叠维度。\n"
        "6. 输出严格 JSON，不要任何额外文字、注释、markdown 代码块标记。\n\n"
        "【字段硬下限 — 严格遵守，否则视为不合格】\n"
        "  - characters：必须列出所有有姓名的角色，主角必有，重要配角 ≥3。\n"
        "  - 主角的 fatal_flaw 字段绝对不能为空字符串，必须从原文中找出主角最致命的软肋（情感软肋/能力短板/记忆伤痕，三选一）。\n"
        "  - characters[*].secrets：主角和至少 2 名重要配角必须有 secrets 字段（≥1 条秘密），"
        "    没有明显秘密的角色可以填 []，但全书 secrets 总数必须 ≥3 条。\n"
        "  - hidden_threads：必须 ≥3 条；如果你只想到 1-2 条，请回头通读原文找出更多『未明说但在推动剧情的暗线』。\n"
        "  - relationship_map[*].tension_source 和 leverage 必须填具体内容，不准全部填 \"无\"。\n"
        "  - factions：除非完全没有势力概念（极罕见），否则必须 ≥1 条。\n"
        "  - ending_dimensions：必须 ≥2 条且互不重叠。\n"
        "  - 主角 core_motivation 必须是一句具体目标（如『为母报仇』『查清父亲死因』），不准写『追寻命运』这种空话。\n"
        + ("\n" + lang_instr if lang_instr else "")
    )

    def _call_worldview_json(user_prompt: str, max_tokens: int, label: str) -> dict:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                payload = client.chat_completion_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.2,
                    max_tokens=max_tokens,
                )
                if not isinstance(payload, dict) or not payload:
                    raise ValueError("世界观响应为空或不是 JSON 对象")
                return payload
            except Exception as error:
                last_error = error
                if attempt < 3:
                    print(
                        f"   ⚠️  {label}返回无效结果（attempt={attempt}，{error}），"
                        "仅重试当前批次"
                    )
        raise last_error or ValueError(f"{label}失败")

    if total <= _WORLDVIEW_SEG_SIZE:
        user_prompt = (
            f"请通读以下小说全文，输出严格 JSON 世界观档案：\n\n"
            f"{full_text}\n\n"
            f"输出格式：\n{_WORLDVIEW_FORMAT}"
        )
        try:
            return _call_worldview_json(user_prompt, 6000, "世界观提取")
        except Exception as e:
            print(f"   ⚠️  世界观提取失败（{e}），使用空档案")
            return {}

    # 分段逐段提取，再合并
    seg_results = []
    seg_count = (total + _WORLDVIEW_SEG_SIZE - 1) // _WORLDVIEW_SEG_SIZE
    print(f"   📄 全文 {total} 字，分 {seg_count} 段提取世界观（每段≤{_WORLDVIEW_SEG_SIZE}字）...")
    for i in range(seg_count):
        seg_start = i * _WORLDVIEW_SEG_SIZE
        seg_end = min(seg_start + _WORLDVIEW_SEG_SIZE, total)
        seg_text = full_text[seg_start:seg_end]
        seg_user = (
            f"以下是小说第{i+1}/{seg_count}段原文（字符{seg_start}-{seg_end}），"
            f"请提取本段出现的角色、事件、地点、世界规则，输出严格 JSON：\n\n"
            f"{seg_text}\n\n"
            f"输出格式：\n{_WORLDVIEW_FORMAT}"
        )
        try:
            seg_result = _call_worldview_json(
                seg_user,
                4000,
                f"第{i+1}/{seg_count}段世界观提取",
            )
            # Carry a stable source marker into every structured payload so
            # later merges and diagnostics can trace facts back to the source
            # segment even when individual fields are compacted.
            seg_result.setdefault(
                "source_segment",
                f"segment-{i + 1}/{seg_count}:{seg_start}-{seg_end}",
            )
            seg_results.append(seg_result)
            print(f"   ✅ 第{i+1}/{seg_count}段世界观提取完成")
        except Exception as e:
            print(f"   ⚠️  第{i+1}段世界观提取失败（{e}），跳过")

    if not seg_results:
        return {}
    if len(seg_results) == 1:
        return seg_results[0]

    serialized_results = [
        _json.dumps(result, ensure_ascii=False)
        for result in seg_results
    ]

    def _merge_worldview_group(group: list[str], level: int, batch_index: int) -> str:
        merge_prompt = _build_worldview_merge_prompt(
            group,
            level=level,
            batch_index=batch_index,
            max_prompt_tokens=_worldview_merge_prompt_budget(6000),
        )
        merged = _call_worldview_json(
            merge_prompt,
            6000,
            f"世界观第{level + 1}层第{batch_index + 1}批合并",
        )
        print(
            f"   ✅ 世界观第{level + 1}层第{batch_index + 1}批合并完成"
            f"（{len(group)}份）"
        )
        return _json.dumps(merged, ensure_ascii=False)

    # Worldview payloads are structured JSON. Reduce them with a prompt-aware
    # grouping strategy instead of hierarchical_reduce's character-prefix
    # fallback, which could leave malformed or semantically incomplete JSON.
    current = list(serialized_results)
    level = 0
    merge_budget = _worldview_merge_prompt_budget(6000)
    while len(current) > 1:
        groups = _group_worldview_merge_items(
            current,
            max_batch_size=HIERARCHICAL_SETTINGS.worldview_merge_batch_size,
            max_prompt_tokens=merge_budget,
        )
        next_level: list[str] = []
        for batch_index, group in enumerate(groups):
            if len(group) == 1:
                next_level.append(str(group[0]))
                continue
            try:
                next_level.append(_merge_worldview_group(group, level, batch_index))
            except Exception as error:
                print(
                    f"   ⚠️  世界观第{level + 1}层第{batch_index + 1}批合并失败（{error}），"
                    "使用结构化确定性兜底"
                )
                next_level.append(
                    _json.dumps(
                        _fallback_merge_worldview_payloads(group),
                        ensure_ascii=False,
                    )
                )
        if len(next_level) >= len(current):
            # A tiny budget can otherwise produce singleton groups forever.
            forced = current[:2]
            try:
                next_level = [_merge_worldview_group(forced, level, 0)] + current[2:]
            except Exception:
                combined = _fallback_merge_worldview_payloads(forced)
                next_level = [_json.dumps(combined, ensure_ascii=False)] + current[2:]
        current = next_level
        level += 1
    merged_text = current[0] if current else ""
    try:
        return _json.loads(merged_text)
    except (TypeError, ValueError, _json.JSONDecodeError):
        print("   ⚠️  世界观分层合并结果解析失败，返回首段结果")
        return seg_results[0]


def _summarize_one_chapter(ch: str, idx: int, worldview: Dict[str, Any], client: LLMClient, output_language: str = None) -> str:
    """
    通读单章全文生成结构化精细摘要（350-500字）。
    输出统一使用 10 个标签段，便于下游 What-If/软分支/结局生成直接引用。
    """
    relevant_names: list[str] = []
    for character in worldview.get("characters", []) or []:
        if not isinstance(character, dict):
            continue
        name = str(character.get("name") or "").strip()
        aliases = [
            str(alias or "").strip()
            for alias in (character.get("aliases") or [])
            if str(alias or "").strip()
        ]
        mentioned = any(candidate in ch for candidate in [name, *aliases] if candidate)
        if mentioned or str(character.get("role") or "").startswith("主角"):
            if name and name not in relevant_names:
                relevant_names.append(name)
        if len(relevant_names) >= 8:
            break
    char_names = ", ".join(relevant_names)
    world_rules_raw = worldview.get("world_rules", "")
    if isinstance(world_rules_raw, list):
        world_rules = "; ".join(
            (r.get("rule_name", "") + "：" + r.get("description", "")) if isinstance(r, dict) else str(r)
            for r in world_rules_raw
        )
    else:
        world_rules = str(world_rules_raw)
    persona_axes = worldview.get("persona_axes", []) or []
    axes_keys = ", ".join(a.get("key", "") for a in persona_axes if isinstance(a, dict))
    ending_dimensions = worldview.get("ending_dimensions", []) or []
    ending_dims_text = "; ".join(
        d.get("dimension", "") for d in ending_dimensions if isinstance(d, dict)
    )
    lang_instr = get_lang_instruction(output_language)

    system_prompt = (
        "你是专业文学编辑 + 视觉小说叙事架构师，对章节进行**结构化深度分析**。"
        "你的产出会同时驱动 What-If 分支、软分支选项、结局路由——必须把章节里所有可被复用的「弹药」摆上桌。\n"
        "严格基于原文事实，禁止虚构。每个标签段都必须填，信息不足时填 \"未明示\" 也不要省略标签。"
        + ("\n" + lang_instr if lang_instr else "")
    )

    segments = [
        segment.strip()
        for segment in split_text_at_boundaries(
            ch,
            max_chars=HIERARCHICAL_SETTINGS.analysis_segment_chars,
        )
        if segment.strip()
    ]

    structured_template = (
        "请按以下 10 个标签段输出（顺序固定，每段独立成行）：\n"
        "【章节定位】本章在全书中的功能（开局/铺垫/转折/高潮/收束/结局），≤25字\n"
        "【时间地点】本章发生的时间与主要地点\n"
        "【出场人物】列出所有出场角色，必要时标注状态变化（如 'XX（从信任→怀疑）'）\n"
        "【核心事件】3-5 条关键事件，每条 ≤30 字，按发生顺序\n"
        "【人物决策与后果】主角及其他重要角色的关键决策 → 直接后果 → 长远影响（每条一行，≤50字）\n"
        "【情感基调】章首基调 → 章末基调（含主角心境的具体词汇）\n"
        "【世界观信息增量】本章新揭示的世界规则/势力动向/伏笔（若无新信息则填 \"无新增\"）\n"
        "【可用作 What-If 的岔路】1-2 个真实可改的命运岔口，格式：\n"
        "  - 岔口名：主角面临的具体抉择（不是被动事件）\n"
        "  - 若选 X：会引发什么不同走向（1 句话）\n"
        f"【数值钩子】本章里主角面临的人格/情感张力点（必须明确指出对应哪个 persona_axes key：{axes_keys or '待定'}），\n"
        "  格式：'轴 key' → 主角在本章的哪个具体场景体现了这条轴的张力（≤40字/条，1-3 条）\n"
        f"【与结局的关联】本章铺垫了哪个结局维度（{ending_dims_text or '待定'}），1-2 句话\n"
    )

    seg_summaries = []
    for seg_idx, seg in enumerate(segments):
        seg_label = f"第{idx+1}章" if len(segments) == 1 else f"第{idx+1}章第{seg_idx+1}段"
        user_prompt = (
            f"已知角色：{char_names}\n"
            f"世界规则：{world_rules}\n"
            f"persona_axes：{axes_keys or '（世界观未输出）'}\n"
            f"结局维度：{ending_dims_text or '（世界观未输出）'}\n\n"
            f"请深度分析{seg_label}，目标 350-500 字。\n"
            f"{structured_template}\n"
            f"{seg_label}全文：\n{seg}"
        )
        try:
            s = client.chat_completion(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.3,
                max_tokens=1050,
            ).strip()
            seg_summaries.append(s)
        except Exception as e:
            print(f"   ⚠️  {seg_label}摘要失败（{e}）")
            seg_summaries.append(f"{seg_label}（{len(seg)}字，分析失败）")

    if len(seg_summaries) == 1:
        return seg_summaries[0]

    def _merge_summary_group(group: list[str], level: int, batch_index: int) -> str:
        merge_prompt = (
            f"以下是第{idx+1}章结构化分析的第{level + 1}层第{batch_index + 1}批，"
            f"本批共{len(group)}份，请合并为一份完整章节摘要（350-500 字），"
            f"保持 10 个标签段的统一结构，不得删减任何标签：\n\n"
            + "\n\n---\n\n".join(group)
            + f"\n\n{structured_template}"
        )
        return client.chat_completion(
            system_prompt="你是专业摘要师，擅长按结构化标签合并多段分析为完整章节摘要，保留所有 10 个标签段。",
            user_prompt=merge_prompt,
            temperature=0.2,
            max_tokens=1800,
        ).strip()

    return hierarchical_reduce(
        seg_summaries,
        merge_batch_size=HIERARCHICAL_SETTINGS.summary_merge_batch_size,
        merge_group=_merge_summary_group,
        fallback_max_chars=DEFAULT_FALLBACK_MERGE_CHARS,
    )


def _format_char_cards(worldview: Dict[str, Any]) -> str:
    """将世界观里的角色信息格式化为 prompt 友好的文本"""
    chars = worldview.get("characters", [])
    if not chars:
        return ""
    lines = ["【角色卡（含背景/动机/软肋/秘密）】"]
    for c in chars:
        aliases = [str(alias) for alias in (c.get("aliases") or []) if str(alias).strip()]
        alias_text = f"；别名：{'、'.join(aliases)}" if aliases else ""
        line = (
            f"▪ {c.get('name','')}（{c.get('role','')}）{alias_text}"
            f"\n   外貌：{c.get('appearance','未明示')}"
            f"\n   背景：{c.get('background','未明示')}"
            f"\n   性格：{c.get('personality','')}；说话方式：{c.get('speech_style','')}"
            f"\n   核心驱动：{c.get('core_motivation','未明示')}"
            f"\n   致命软肋：{c.get('fatal_flaw','未明示')}"
        )
        secrets = c.get("secrets", [])
        if secrets:
            line += f"\n   秘密：" + "；".join(s for s in secrets if s)
        line += f"\n   成长弧：{c.get('arc','')}"
        lines.append(line)
    return "\n".join(lines)


def _format_relationship_map(worldview: Dict[str, Any]) -> str:
    """格式化人物关系线（含张力来源/筹码）"""
    rels = worldview.get("relationship_map", [])
    if not rels:
        return ""
    lines = ["【人物关系线（含张力与筹码）】"]
    for r in rels:
        line = (
            f"▪ {r.get('from','')} ↔ {r.get('to','')}"
            f"：{r.get('type','')}，{r.get('evolution','')}"
        )
        tension = r.get("tension_source", "")
        leverage = r.get("leverage", "")
        if tension:
            line += f"\n   张力来源：{tension}"
        if leverage and leverage != "无":
            line += f"\n   筹码/把柄：{leverage}"
        lines.append(line)
    return "\n".join(lines)


def _format_world_rules(worldview: Dict[str, Any]) -> str:
    """格式化世界观规则数组（兼容旧版字符串）。"""
    rules = worldview.get("world_rules", "")
    if isinstance(rules, str):
        return rules
    if not isinstance(rules, list) or not rules:
        return ""
    lines = ["【世界规则/体系（必须严格遵守）】"]
    for r in rules:
        if isinstance(r, dict):
            lines.append(
                f"▪ {r.get('rule_name','规则')}：{r.get('description','')}"
                f"\n   含义：{r.get('implications','')}"
            )
        else:
            lines.append(f"▪ {r}")
    return "\n".join(lines)


def _format_factions(worldview: Dict[str, Any]) -> str:
    """格式化势力/组织"""
    factions = worldview.get("factions", [])
    if not factions:
        return ""
    lines = ["【势力/组织（角色行为受其约束）】"]
    for f in factions:
        if not isinstance(f, dict):
            continue
        line = (
            f"▪ {f.get('name','')}：{f.get('ideology','')}"
            f"\n   主要成员：{', '.join(f.get('key_members', []))}"
            f"\n   敌对：{', '.join(f.get('conflicts_with', []))}"
            f"\n   对主角的压力/吸引：{f.get('leverage_on_protagonist','')}"
        )
        lines.append(line)
    return "\n".join(lines)


def _format_hidden_threads(worldview: Dict[str, Any]) -> str:
    """格式化隐线/伏笔"""
    threads = worldview.get("hidden_threads", [])
    if not threads:
        return ""
    lines = ["【隐线/伏笔（What-If 与结局可调用的弹药）】"]
    for t in threads:
        if not isinstance(t, dict):
            continue
        lines.append(
            f"▪ {t.get('thread','')}"
            f"\n   首现：{t.get('introduced_at','')}；回收：{t.get('resolved_at','')}"
            f"\n   含义：{t.get('implication','')}"
        )
    return "\n".join(lines)


def _format_character_secrets(worldview: Dict[str, Any]) -> str:
    """提取所有角色 secrets，格式化为 What-If / 结局可调用的弹药块。"""
    chars = worldview.get("characters", [])
    if not chars:
        return ""
    lines = []
    for c in chars:
        if not isinstance(c, dict):
            continue
        secrets = [s for s in (c.get("secrets") or []) if s]
        if not secrets:
            continue
        name = c.get("name", "")
        role = c.get("role", "")
        for s in secrets:
            lines.append(f"  ▪ 【{name}（{role}）】{s}")
    if not lines:
        return ""
    return (
        "【可调用的角色秘密（What-If / 结局可揭穿这些秘密制造原著没有的戏剧冲突）】\n"
        + "\n".join(lines)
        + "\n   使用方式：在 What-If/结局合适时机让某个角色的秘密被揭穿，"
        "可以让另一个角色的决定/主角的认知/势力关系发生原著中不可能的逆转。\n"
    )


def summarize_whatif_chapter(
    whatif_text: str,
    whatif_title: str,
    provider: str,
    worldview: Dict[str, Any] = None,
    output_language: str = None,
) -> str:
    """
    给 What-If 章节生成简版结构化摘要（200-300 字），供软分支注入用作 cur_summary。
    与 _summarize_one_chapter 的 10 标签结构对齐，但精简到关键信息。
    """
    client = LLMClient(provider=provider)
    worldview = worldview or {}
    char_names = ", ".join(
        c.get("name", "") for c in worldview.get("characters", [])[:8]
        if isinstance(c, dict)
    )
    persona_axes = worldview.get("persona_axes", []) or []
    axes_keys = ", ".join(a.get("key", "") for a in persona_axes if isinstance(a, dict))
    lang_instr = get_lang_instruction(output_language)

    system_prompt = (
        "你是视觉小说叙事分析师，对一段 What-If 平行线章节做精炼结构化摘要，"
        "输出供软分支生成器作 prompt 锚定使用。"
        + ("\n" + lang_instr if lang_instr else "")
    )
    user_prompt = (
        f"已知角色：{char_names}\n"
        f"persona_axes：{axes_keys or '（未提供）'}\n\n"
        f"以下是《{whatif_title}》（What-If 平行章节）的全文，请输出 200-300 字的精简结构化摘要，"
        "按以下 6 个标签段输出（顺序固定，每段独立成行，信息不足时填 \"未明示\"）：\n"
        "【章节定位】这条 What-If 在原著哪个岔路口分叉，承担什么叙事功能（≤30 字）\n"
        "【时间地点】\n"
        "【出场人物】\n"
        "【核心事件】3-5 条，按发生顺序，每条 ≤30 字\n"
        f"【数值钩子】本章触发的人格/情感张力点（必须明确指出对应的 persona_axes key：{axes_keys or '待定'}），1-3 条\n"
        "【与结局的关联】这条 What-If 推动主角走向哪个结局走向（1 句话）\n\n"
        f"What-If 全文：\n{whatif_text}"
    )
    try:
        return client.chat_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.3,
            max_tokens=600,
        ).strip()
    except Exception as e:
        print(f"   ⚠️  What-If 简版摘要生成失败（{e}），返回空摘要")
        return ""


def summarize_ending_chapter(
    ending_text: str,
    ending_title: str,
    provider: str,
    worldview: Dict[str, Any] = None,
    output_language: str = None,
) -> str:
    """
    给结局章节生成精简结构化摘要（200-300 字），写回 story_tree.json.summary。
    与章节摘要风格对齐，让下游/前端列表/调试一眼能看出这条结局到底发生了什么。
    """
    client = LLMClient(provider=provider)
    worldview = worldview or {}
    char_names = ", ".join(
        c.get("name", "") for c in worldview.get("characters", [])[:8]
        if isinstance(c, dict)
    )
    lang_instr = get_lang_instruction(output_language)
    system_prompt = (
        "你是视觉小说叙事分析师，负责对一条平行结局做精炼结构化摘要，"
        "供前端列表展示与下游链路调试使用。"
        + ("\n" + lang_instr if lang_instr else "")
    )
    user_prompt = (
        f"已知核心角色：{char_names}\n\n"
        f"以下是《{ending_title}》（一条平行结局）的全文，请输出 200-300 字的结构化摘要，"
        "按以下 5 个标签段输出（顺序固定，每段独立成行，信息不足时填 \"未明示\"）：\n"
        "【结局基调】≤20 字，比如 \"苦涩团圆\"/\"血腥孤王\"/\"双双殒命\"\n"
        "【时间地点】\n"
        "【关键事件】3-5 条，按发生顺序，每条 ≤30 字\n"
        "【核心人物归宿】列出主要角色（含主角）的最终去向：生/死/隐退/疯癫等\n"
        "【主角内心】1 句话，主角在结局落幕时的核心心境\n\n"
        f"结局全文：\n{ending_text}"
    )
    try:
        return client.chat_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.3,
            max_tokens=600,
        ).strip()
    except Exception as e:
        print(f"   ⚠️  结局摘要生成失败（{e}），返回空摘要")
        return ""


_UNSTABLE_GENERIC_CHARACTER_NAMES = {
    "班长",
    "副班长",
    "主任",
    "班主任",
    "教导主任",
    "老师",
    "校长",
    "医生",
    "护士",
    "警察",
    "同学",
    "店员",
    "服务员",
    "司机",
    "保安",
    "经理",
    "领导",
}

_TERMINAL_MEMORY_MARKERS = (
    "想起",
    "回想",
    "回忆起",
    "记起",
    "记得",
    "忆及",
    "梦见",
    "梦到",
    "脑海里",
    "记忆里",
)

_TERMINAL_OFFSCREEN_MARKERS = (
    "电话里",
    "电话中",
    "听筒里",
    "通话中",
    "来电",
    "语音里",
    "录音里",
    "短信里",
    "微信里",
    "信里",
    "来信中",
    "广播里",
    "屏幕里",
    "视频里",
    "听说",
    "据说",
    "传闻",
    "转述",
)

_TERMINAL_ACTIVE_VERBS = (
    "说",
    "问",
    "答",
    "喊",
    "叫住",
    "开口",
    "回答",
    "解释",
    "坦白",
    "拒绝",
    "决定",
    "推",
    "拉",
    "拿",
    "放",
    "递",
    "抓",
    "抱",
    "打",
    "写",
    "查",
    "追",
    "走",
    "跑",
    "进",
    "出",
    "来",
    "去",
    "回",
    "离开",
    "出现",
    "坐",
    "站",
    "看",
    "笑",
    "哭",
    "点头",
    "摇头",
    "抬手",
    "转身",
    "继续",
    "想起",
    "回想",
    "回忆",
    "记起",
    "记得",
    "梦见",
    "梦到",
)

# A character/alias followed by a perfective or explicitly retrospective
# speech/action phrase is an attribution about the past, not an action taking
# place in the current chapter.  This matters especially for descriptive
# aliases such as ``世界第一强者``: ``世界第一强者就说过……`` should be a
# lightweight mention, while ``世界第一强者说……`` remains an active
# appearance.  Keep the cue deliberately narrow so ordinary present-tense
# alias actions continue to be recognized.
_TERMINAL_HISTORICAL_ATTRIBUTION_RE = re.compile(
    r"^\s*(?:(?:就|便|也|还)\s*)?(?:(?:曾经?|过去|此前|早年|当年|从前|生前)\s*"
    r"(?:[^，,。！？!?；;：:]{0,16}?)(?:说|讲|谈|提|道|表示|强调|写|留下|告诉)(?:过|着)?|"
    r"(?:说|讲|谈|提|道|表示|强调|写|留下|告诉)过)"
    r"(?:的)?"
)

_TERMINAL_QUOTE_PAIRS = {
    "‘": "’",
    "“": "”",
    "「": "」",
    "『": "』",
    "《": "》",
    "〈": "〉",
    "【": "】",
    '"': '"',
    "'": "'",
    "（": "）",
    "(": ")",
}


def _is_stable_terminal_character(character: Dict[str, Any]) -> bool:
    name = str(character.get("name") or "").strip()
    if not name:
        return False
    if name not in _UNSTABLE_GENERIC_CHARACTER_NAMES:
        return True
    if bool(character.get("stable_identity")) or character.get("identity_id"):
        return True
    aliases = [
        str(alias or "").strip()
        for alias in (character.get("aliases") or [])
        if str(alias or "").strip()
    ]
    return any(alias not in _UNSTABLE_GENERIC_CHARACTER_NAMES for alias in aliases)


def _terminal_character_candidates(character: Dict[str, Any]) -> list[str]:
    name = str(character.get("name") or "").strip()
    candidates = [name]
    candidates.extend(
        str(alias or "").strip()
        for alias in (character.get("aliases") or [])
        if str(alias or "").strip()
        and str(alias or "").strip() not in _UNSTABLE_GENERIC_CHARACTER_NAMES
    )
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _terminal_single_name_is_independent(
    text: str,
    start: int,
    end: int,
) -> bool:
    """Reject a one-character name when it is only part of a longer word.

    Chinese single-character names cannot use ``\b`` as a reliable boundary:
    both a real name (``洪看向罗峰``) and a place name (``洪泽湖``) are made of
    adjacent CJK characters.  Keep only occurrences with concrete name grammar
    such as quotation marks, an action immediately after the name, a possessive
    marker, or an interaction/title immediately before it.
    """
    import re as _re

    left = text[max(0, start - 16):start]
    right = text[end:min(len(text), end + 16)]
    left_char = text[start - 1] if start > 0 else ""
    right_char = text[end] if end < len(text) else ""
    quote_chars = "'\"‘’“”「」『』《》〈〉【】"

    # A quoted single-character name is valid only when the quoted token is
    # exactly that character.  This prevents ``‘洪泽湖’`` from being treated
    # as the character ``洪`` while retaining ``‘洪’抬手``.
    quote_pairs = (("‘", "’"), ("“", "”"), ("「", "」"), ("『", "』"), ("《", "》"), ("〈", "〉"), ("【", "】"), ('"', '"'), ("'", "'"))
    for quote_open, quote_close in quote_pairs:
        quote_start = text.rfind(quote_open, 0, start + 1)
        quote_end = text.find(quote_close, end)
        if quote_start >= 0 and quote_end >= 0:
            quoted_value = text[quote_start + len(quote_open) : quote_end].strip()
            if quoted_value != text[start:end]:
                return False
            return True

    if (left_char and left_char in quote_chars) or (
        right_char and right_char in quote_chars
    ):
        return True
    if _re.match(r"\s*[：:]", right):
        return True

    verb_pattern = "|".join(
        sorted(
            (_re.escape(verb) for verb in _TERMINAL_ACTIVE_VERBS),
            key=len,
            reverse=True,
        )
    )
    if _re.match(
        rf"\s*(?:{verb_pattern}|是|为|曾|也|则|便|就|并|与|和|同|及)",
        right,
    ):
        return True
    if _re.match(
        r"\s*(?:在)?(?:电话里|电话中|听筒里|通话中|来电|语音里|录音里|短信里|微信里|信里|来信中|广播里|屏幕里|梦里|梦中|回忆中|记忆中)",
        right,
    ):
        return True
    if _re.match(r"\s*(?:的|本人|先生|女士|老师|师父|前辈|队长|馆主|校长|主任)", right):
        return True
    if _re.search(
        r"(?:问|找|叫|喊|见|遇见|看向|望向|走向|跟|与|和|同|对|向|由|据|听说|提到|提及|想起|回忆起|传来|听见|听到|收到|读到|写着)\s*$",
        left,
    ):
        return True
    if _re.search(
        r"(?:世界第[一二三四五六七八九十]+强者|第一强者|第二强者|强者|馆主|宗主|掌门|先生|女士|老师|师父|前辈)\s*$",
        left,
    ):
        return True

    is_cjk = lambda value: bool(
        value and _re.match(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", value)
    )
    # At a sentence boundary, one missing neighbour is not enough to prove a
    # name: ``洪泽湖`` starts at the boundary but the following CJK character
    # shows that it is part of a longer lexical item.  A punctuation boundary
    # on either side, however, is a valid standalone mention.
    if right_char and not is_cjk(right_char) and (
        not left_char or not is_cjk(left_char)
    ):
        return True
    if left_char and not is_cjk(left_char) and (
        not right_char or not is_cjk(right_char)
    ):
        return True
    return False


def _terminal_candidate_spans(
    text: str,
    candidates: list[str],
    all_candidates: list[str] | None = None,
) -> list[tuple[int, int, str]]:
    """Return non-overlapping entity spans, preferring the longest name.

    For example, when both ``洪`` and ``罗洪国`` are known characters, the
    occurrence in ``罗洪国笑着说道`` belongs only to ``罗洪国``.  Mention,
    action and memory/offscreen classification all consume these same spans so
    they cannot disagree about which entity was present.
    """
    import re as _re

    targets = {str(candidate or "").strip() for candidate in candidates}
    known = {
        str(candidate or "").strip()
        for candidate in (all_candidates or candidates)
        if str(candidate or "").strip()
    }
    occupied: list[tuple[int, int]] = []
    accepted: list[tuple[int, int, str]] = []
    for candidate in sorted(known, key=lambda value: (-len(value), value)):
        for match in _re.finditer(_re.escape(candidate), str(text or "")):
            start, end = match.span()
            if any(start < used_end and end > used_start for used_start, used_end in occupied):
                continue
            if len(candidate) == 1 and not _terminal_single_name_is_independent(
                str(text or ""), start, end
            ):
                continue
            occupied.append((start, end))
            if candidate in targets:
                accepted.append((start, end, candidate))
    return sorted(accepted, key=lambda item: (item[0], item[1]))


def _terminal_mention_sentences(
    text: str,
    candidates: list[str],
    all_candidates: list[str] | None = None,
) -> list[str]:
    import re as _re

    sentences = [
        segment.strip()
        for segment in _re.split(r"(?<=[。！？!?；;\n])", str(text or ""))
        if segment.strip()
    ]
    return [
        sentence
        for sentence in sentences
        if _terminal_candidate_spans(sentence, candidates, all_candidates)
    ]


def _terminal_span_is_historical_attribution(
    clause: str,
    start: int,
    end: int,
) -> bool:
    """Whether an entity occurrence is only a retrospective attribution.

    The terminal classifier is intentionally conservative: a name should be
    sent to the detailed LLM extractor only when the current chapter actually
    makes it act or speak.  Chinese prose commonly uses a historical quote
    pattern (``某人曾说过``/``某人就说过``), and the generic active verb ``说``
    would otherwise classify that occurrence as active.  Inspect only a small
    local window around the matched entity so a separate action later in the
    same clause is not suppressed.
    """
    value = str(clause or "")
    following = value[end : min(len(value), end + 48)]
    # Character names are often written as an apposition, e.g.
    # ``世界第一强者‘洪’就说过``.  The alias span ends before the opening quote,
    # while the canonical-name span ends before the closing quote.  Normalize
    # both forms before checking the historical cue; only strip a short quoted
    # token that cannot contain sentence punctuation, so a real dialogue block
    # is never swallowed as metadata.
    following = following.lstrip(" \t\r\n")
    if following and following[0] in _TERMINAL_QUOTE_PAIRS:
        quote_close = _TERMINAL_QUOTE_PAIRS[following[0]]
        quote_end = following.find(quote_close, 1)
        if 1 <= quote_end <= 16:
            quoted_token = following[1:quote_end]
            if quoted_token and not re.search(r"[。！？!?；;\n]", quoted_token):
                following = following[quote_end + 1 :]
    following = following.lstrip(" \t\r\n，,、；;‘’“”\"'「」『』《》〈〉【】（）()")
    if _TERMINAL_HISTORICAL_ATTRIBUTION_RE.match(following):
        return True

    # Retrospective cues can also precede the name (for example, “早年洪曾
    # 说过……”).  Do not classify generic ``说`` as historical solely because
    # it appears after a title; require an explicit temporal/attribution cue.
    preceding = value[max(0, start - 20) : start]
    return bool(
        re.search(
            r"(?:曾经?|过去|此前|早年|当年|从前|生前|历史上|据记载|记载中)\s*$",
            preceding,
        )
    )


def _terminal_sentence_is_active(
    sentence: str,
    candidates: list[str],
    all_candidates: list[str] | None = None,
) -> bool:
    import re as _re

    verb_pattern = "|".join(
        sorted((_re.escape(verb) for verb in _TERMINAL_ACTIVE_VERBS), key=len, reverse=True)
    )
    # 只在同一分句内判断“角色名 + 行动/发言”。如果跨过逗号继续匹配，
    # “同桌叫谭凯，她过去常借谭凯的作业”会把被提及的谭凯误判为行动者。
    clauses = [clause.strip() for clause in _re.split(r"[，,。！？!?；;\n]", sentence)]
    for clause in clauses:
        for _start, end, _candidate in _terminal_candidate_spans(
            clause, candidates, all_candidates
        ):
            if _terminal_span_is_historical_attribution(clause, _start, end):
                continue
            following = clause[end:]
            if _re.match(r"\s*[：:]", following):
                return True
            if _re.match(rf"[^：:]{{0,12}}(?:{verb_pattern})", following):
                return True
    return False


def _terminal_marker_targets_character(
    sentence: str,
    candidates: list[str],
    markers: tuple[str, ...],
    *,
    allow_marker_after_name: bool = False,
    all_candidates: list[str] | None = None,
) -> bool:
    """Return whether a memory/offscreen marker describes this character."""
    import re as _re

    clauses = [clause.strip() for clause in _re.split(r"[，,。！？!?；;\n]", sentence)]
    for clause in clauses:
        for candidate_index, candidate_end, _candidate in _terminal_candidate_spans(
            clause, candidates, all_candidates
        ):
            for marker in markers:
                marker_index = clause.find(marker)
                if marker_index < 0:
                    continue
                # “想起顾沉”“电话里传来顾沉的声音”：marker 指向后面的人物。
                if marker_index < candidate_index and candidate_index - marker_index <= 30:
                    return True
                # 场外媒介也常写作“顾沉在电话里说”。回忆动词不能使用这一条，
                # 否则“顾沉想起旧案”会把实际行动者误判成 memory。
                if (
                    allow_marker_after_name
                    and candidate_index < marker_index
                    and marker_index - candidate_end <= 8
                ):
                    return True
            following = clause[candidate_end:]
            if _re.match(r"(?:的)?(?:回忆|记忆)(?:中|里)?", following):
                return True
    return False


def classify_terminal_state_characters(
    chapter_text: str,
    worldview: Dict[str, Any] = None,
) -> Dict[str, str]:
    """Classify stable characters before terminal-state LLM extraction.

    active characters need a detailed LLM state.  mentioned characters receive
    a deterministic lightweight record.  memory/offscreen characters inherit
    their previous state and therefore do not participate in confidence retry.
    """
    worldview = worldview if isinstance(worldview, dict) else {}
    classified: Dict[str, str] = {}
    tracked_characters: list[tuple[str, list[str]]] = []
    for character in worldview.get("characters", []) or []:
        if not isinstance(character, dict) or not _is_stable_terminal_character(character):
            continue
        name = str(character.get("name") or "").strip()
        candidates = _terminal_character_candidates(character)
        tracked_characters.append((name, candidates))
    all_candidates = list(
        dict.fromkeys(
            candidate
            for _name, candidates in tracked_characters
            for candidate in candidates
        )
    )
    for name, candidates in tracked_characters:
        mention_sentences = _terminal_mention_sentences(
            chapter_text,
            candidates,
            all_candidates,
        )
        if not mention_sentences:
            continue
        observed_modes: set[str] = set()
        for sentence in mention_sentences:
            if _terminal_marker_targets_character(
                sentence,
                candidates,
                _TERMINAL_MEMORY_MARKERS,
                all_candidates=all_candidates,
            ):
                observed_modes.add("memory")
            elif _terminal_marker_targets_character(
                sentence,
                candidates,
                _TERMINAL_OFFSCREEN_MARKERS,
                allow_marker_after_name=True,
                all_candidates=all_candidates,
            ):
                observed_modes.add("offscreen")
            elif _terminal_sentence_is_active(
                sentence,
                candidates,
                all_candidates,
            ):
                observed_modes.add("active")
            else:
                observed_modes.add("mentioned")
        if "active" in observed_modes:
            classified[name] = "active"
        elif "memory" in observed_modes:
            classified[name] = "memory"
        elif "offscreen" in observed_modes:
            classified[name] = "offscreen"
        else:
            classified[name] = "mentioned"
    return classified


def _first_person_narrator_is_unique_protagonist(
    worldview: Dict[str, Any],
    chapter_text: str,
) -> bool:
    """Conservatively infer that ``我`` refers to the sole declared protagonist.

    This is deliberately stricter than simply finding the pronoun: a chapter may
    contain dialogue from several characters or an explicit POV hand-off.  Only
    a single declared protagonist, a first-person worldview note, and enough
    narration outside quotes qualify for the deterministic upgrade to ``active``.
    """
    if not isinstance(worldview, dict):
        return False
    protagonists = [
        character
        for character in (worldview.get("characters", []) or [])
        if isinstance(character, dict)
        and (
            str(character.get("role") or "").startswith("主角")
            or bool(character.get("is_protagonist"))
            or str(character.get("role") or "").strip().lower()
            in {"主人公", "protagonist"}
        )
        and str(character.get("name") or "").strip()
    ]
    if len(protagonists) != 1:
        return False
    # Reuse the canonical POV inference so a legitimate note such as
    # “第一人称叙述，角色对白偶尔使用第三人称转述” remains first-person.
    # The older substring check treated any mention of “第三人称” as a hard
    # contradiction and consequently omitted the protagonist from active
    # terminal-state extraction.
    inferred_contract = infer_narrative_perspective_contract(
        worldview,
        text=chapter_text,
    )
    inferred_perspective = _normalize_pov_label(
        str(
            inferred_contract.get("perspective")
            or inferred_contract.get("mode")
            or ""
        )
    )
    if inferred_perspective not in {"first_person", "mixed"}:
        return False

    text = str(chapter_text or "")
    protagonist_name = str(protagonists[0].get("name") or "").strip()
    # Explicit POV labels take precedence over the global style note.  Do not
    # map a supporting character's first-person chapter back to the protagonist.
    switch_patterns = (
        rf"(?:以|从|切换至|转到|进入)?\s*{re.escape(protagonist_name)}\s*视角",
        r"(?:以|从|切换至|转到|进入)?\s*[^\s，。！？：:【】]{1,12}\s*视角",
    )
    explicit_pov = re.findall(r"(?:视角|POV)", text, flags=re.IGNORECASE)
    if explicit_pov:
        protagonist_marker = any(
            re.search(pattern, text, flags=re.IGNORECASE)
            for pattern in switch_patterns[:1]
        )
        # A labeled POV belonging to another character is a hard opt-out.  A
        # plain chapter title mentioning “视角” without a name remains neutral.
        other_pov = re.search(
            r"(?:以|从|切换至|转到|进入)?\s*([^\s，。！？：:【】]{1,12})\s*视角",
            text,
            flags=re.IGNORECASE,
        )
        if other_pov and not protagonist_marker:
            return False

    # Remove common quote forms so a supporting character saying “我” cannot
    # make the narrator appear active.
    narrator_text = _pov_remove_dialogue_and_markup(text)
    # A single strong narrator clause is enough when the contract explicitly
    # identifies first-person POV.  Keep the two-pronoun fallback for Chinese
    # sentences whose verb is not in the bounded action vocabulary.
    first_person_hits = _pov_first_person_narration_hits(text)
    raw_pronoun_hits = len(re.findall(r"(?<![自忘无唯舍])我(?:们)?", narrator_text))
    return first_person_hits >= 1 or raw_pronoun_hits >= 2


def _inherited_character_is_long_term_required(
    name: str,
    state: Dict[str, Any],
    worldview_character: Dict[str, Any] | None,
) -> bool:
    """Return whether an inherited state is a continuity invariant.

    Core characters and explicit life/death facts are retained indefinitely.
    Ordinary one-off characters are allowed to leave the rolling terminal-state
    set after two consecutive offscreen observations; this keeps prompts small
    without weakening resurrection prevention.
    """
    if not isinstance(state, dict):
        return False
    life_status = str(state.get("life_status") or "").strip().lower()
    if (
        life_status in {
            "death_confirmed",
            "death_reported",
            "death_suspected",
            "fake_death_revealed",
            "revived_confirmed",
        }
        or state.get("outer_alive") is False
    ):
        return True
    character = worldview_character if isinstance(worldview_character, dict) else {}
    role = str(character.get("role") or "")
    if bool(character.get("is_protagonist")) or role.startswith("主角"):
        return True
    if bool(character.get("romance_target")) or any(
        marker in role for marker in ("核心", "重要", "恋爱", "攻略", "反派", "主要", "女主", "男主")
    ):
        return True
    try:
        offscreen_streak = int(state.get("offscreen_streak", 0) or 0)
    except (TypeError, ValueError):
        offscreen_streak = 0
    return offscreen_streak < 2


def _passive_terminal_state(
    name: str,
    mode: str,
    inherited_states: Dict[str, Any],
) -> Dict[str, Any]:
    inherited = inherited_states.get(name)
    state = dict(inherited) if isinstance(inherited, dict) else {
        "alive": True,
        "outer_alive": True,
        "current_layer_alive": True,
        "life_status": "unknown",
        "life_status_confidence": 0.25,
        "death_evidence_level": "unknown",
        "death_sources": [],
        "previous_death_resolution": "",
        "resurrection_rule_id": "",
        "reality_layer": "unknown",
        "reality_domain": "unknown",
        "reconstruction_type": "unknown",
        "local_frame": "present",
    }
    state["state_source"] = (
        "inherited_passive" if isinstance(inherited, dict) else "deterministic_passive"
    )
    if mode == "mentioned":
        state.update(
            {
                "participation": "mentioned",
                "presence_type": "absent",
                "condition": "",
                "timeline": "",
                "location": "",
                "identity_status": "",
                "note": "本章仅被提及",
                "evidence": f"本章仅提及{name}",
                "offscreen_streak": 0,
            }
        )
        if not isinstance(inherited, dict):
            state["state_confidence"] = 0.25
    else:
        state["participation"] = "offscreen"
        try:
            previous_streak = int(state.get("offscreen_streak", 0) or 0)
        except (TypeError, ValueError):
            previous_streak = 0
        state["offscreen_streak"] = previous_streak + 1
        state["note"] = "本章回忆提及，继承上一节点状态" if mode == "memory" else "本章场外出现，继承上一节点状态"
        state["evidence"] = f"{name}仅以{'回忆' if mode == 'memory' else '场外方式'}出现"
        if mode == "memory":
            state["presence_type"] = "memory"
            state["local_frame"] = "memory"
        elif not isinstance(inherited, dict):
            state["presence_type"] = "absent"
    return state


def extract_terminal_states(
    chapter_text: str,
    chapter_title: str,
    provider: str,
    worldview: Dict[str, Any] = None,
    output_language: str = None,
    inherited_states: Dict[str, Any] = None,
    narrative_contract: Dict[str, Any] = None,
    lightweight: bool = False,
) -> Dict[str, Any]:
    """
    从一段已写完的章节正文里抽取末态——核心角色的生死、叙事层级、时间线、位置与身份。

    返回结构：
        {
          "<角色名>": {
            "alive": True/False,
            "presence_type": "physical/hallucination/memory/dream/corpse/absent",
            "reality_layer": "reality/dream/therapy_reconstruction/memory/other",
            "timeline": "<角色当前所处时间线>",
            "condition": "<伤情/精神状态简述，≤25字>",
            "location": "<位置或处境，≤20字>",
            "identity_status": "<当前身份，如在校学生/已故/伪装身份>",
            "note": "<本章关键变化，≤30字>"
          },
          ...
        }

    inherited_states：父节点的末态。如本章未提及某角色，沿用父节点状态，避免 LLM 漏写导致下游误判。
    """
    client = LLMClient(provider=provider)
    worldview = worldview or {}
    narrative_contract = (
        dict(narrative_contract)
        if isinstance(narrative_contract, dict) and narrative_contract
        else infer_narrative_reality_contract(worldview)
    )
    chars = worldview.get("characters", []) or []
    stable_worldview_names = {
        str(character.get("name") or "").strip()
        for character in chars
        if isinstance(character, dict)
        and _is_stable_terminal_character(character)
        and str(character.get("name") or "").strip()
    }
    worldview_names = {
        str(character.get("name") or "").strip()
        for character in chars
        if isinstance(character, dict) and str(character.get("name") or "").strip()
    }
    worldview_character_by_name = {
        str(character.get("name") or "").strip(): character
        for character in chars
        if isinstance(character, dict) and str(character.get("name") or "").strip()
    }
    effective_inherited_states = {
        str(name): state
        for name, state in (inherited_states or {}).items()
        if str(name).strip()
        and (
            str(name).strip() not in _UNSTABLE_GENERIC_CHARACTER_NAMES
            or str(name).strip() in stable_worldview_names
        )
        and (
            not stable_worldview_names
            or str(name) in stable_worldview_names
            or str(name) not in worldview_names
        )
        and _inherited_character_is_long_term_required(
            str(name), state, worldview_character_by_name.get(str(name))
        )
    }
    tracking_modes = classify_terminal_state_characters(chapter_text, worldview)
    protagonist_names: list[str] = []
    for character in chars:
        if not isinstance(character, dict):
            continue
        name = str(character.get("name") or "").strip()
        if not name or not _is_stable_terminal_character(character):
            continue
        if (
            "主角" in str(character.get("role") or "")
            or bool(character.get("is_protagonist"))
            or name
            and str(character.get("role") or "").strip() in {"主人公", "protagonist"}
        ):
            protagonist_names.append(name)
    if _first_person_narrator_is_unique_protagonist(worldview, chapter_text):
        fallback_name = next((name for name in protagonist_names if name), "")
        if fallback_name:
            tracking_modes[fallback_name] = "active"
    for inherited_name in effective_inherited_states:
        tracking_modes.setdefault(inherited_name, "offscreen")
    active_names = [name for name, mode in tracking_modes.items() if mode == "active"]
    passive_modes = {
        name: mode
        for name, mode in tracking_modes.items()
        if name not in active_names and mode != "active"
    }
    if not active_names and not passive_modes:
        return {}
    from services.story_planner.core.chapter_branch_runtime import normalize_terminal_states

    if not active_names:
        passive_states = {
            name: _passive_terminal_state(name, mode, effective_inherited_states)
            for name, mode in passive_modes.items()
        }
        normalized, issues = normalize_terminal_states(
            passive_states,
            effective_inherited_states,
            narrative_contract=narrative_contract,
        )
        if issues:
            print("   ⚠️  末态规范化：" + "；".join(issues))
        return normalized
    lang_instr = get_lang_instruction(output_language)

    inherited_block = ""
    if inherited_states:
        lines = []
        for name in active_names:
            st = effective_inherited_states.get(name)
            if not isinstance(st, dict):
                continue
            lines.append(
                f"  - {name}: alive={st.get('alive')}, life_status={st.get('life_status','unknown')}, "
                f"life_status_confidence={st.get('life_status_confidence', st.get('state_confidence', 0.0))}, "
                f"death_evidence_level={st.get('death_evidence_level','unknown')}, "
                f"presence_type={st.get('presence_type','')}, "
                f"reality_layer={st.get('reality_layer','')}, timeline={st.get('timeline','')}, "
                f"condition={st.get('condition','')}, location={st.get('location','')}, "
                f"identity_status={st.get('identity_status','')}"
            )
        if lines:
            inherited_block = (
                "## 进入本章节前的角色状态（上一章末态，可作为参考；本章正文未提及的角色请沿用此状态）\n"
                + "\n".join(lines) + "\n\n"
            )

    system_prompt = (
        "你是视觉小说叙事分析师，从章节正文里精确抽取核心角色的末态（生死/出场方式/现实层/时间线/伤情/位置/身份），"
        "用于防止下游章节出现穿帮（让已死角色复活、活着的角色凭空消失等）。\n"
        "硬规则：\n"
        "  1. 只能基于本章正文 + 上一章末态推断；不得凭空想象。\n"
        "  2. 若本章正文里某角色未出场也无任何提及，alive 沿用上一章末态，condition/location/note 标 \"本章未提及\"。\n"
        "  3. 必须拆分双层生死：outer_alive 表示外层现实/原时间线是否仍活着；"
        "current_layer_alive 表示角色在当前梦境、重生或治疗重构层内是否以活跃生命状态行动。"
        "alive 是兼容字段，必须始终等于 outer_alive。\n"
        "  4. 角色外层现实已死亡但在治疗重构中行动时，应返回 outer_alive=false、"
        "current_layer_alive=true、alive=false；这不是复活。presence_type 必须区分 "
        "physical/hallucination/memory/dream/corpse/absent。\n"
        "  5. reality_layer 保留正文直接呈现的旧分类；同时必须返回 reality_domain、reconstruction_type、local_frame，"
        "分别表示外层现实域、重构类型和局部叙事框架。梦中的回忆应是 reality_domain=reconstructed_world、"
        "reconstruction_type=dream、local_frame=memory。\n"
        "  6. participation 必须区分 active/mentioned/absent/offscreen/unknown；仅在日记、电话、回忆中被提及的角色不得标 active。\n"
        "  7. state_confidence 为0-1，evidence 必须引用正文中的简短依据；低置信度不得伪装成确定事实。\n"
        "  8. life_status 只能是 alive_confirmed/death_reported/death_suspected/death_confirmed/"
        "fake_death_revealed/revived_confirmed/unknown。听说、传闻、失踪、未找到遗体只能标 death_reported 或 death_suspected，"
        "不得标 death_confirmed；单一角色转述不能确认死亡。\n"
        "  9. death_confirmed 必须同时给出 death_evidence_level（direct/witnessed/body_verified/official_record/explicit_scene）"
        "和正文直接证据；death_sources 记录消息来源。假死揭示必须填 previous_death_resolution=fake_death。\n"
        "  10. revived_confirmed 只允许在全书统一契约 allow_physical_resurrection=true 且正文明确触发世界规则时使用，"
        "并填写 resurrection_rule_id；梦境、鬼魂、回忆不算复活。\n"
        "  11. romance 模式不得为路线差异凭空新增死亡；death_corridor 对确认死亡采用最严格证据门槛。\n"
        "  12. condition/timeline/location/identity_status/note 各自 ≤30 字，简洁但具体（避免 \"状态未知\" 这种空话）。\n"
        + ("\n" + lang_instr if lang_instr else "")
    )
    user_prompt = (
        "## 全书统一叙事层基准（所有章节必须复用，不得自行改名）\n"
        f"{narrative_contract}\n\n"
        f"## 详细末态角色（active，必须完整返回）\n{', '.join(active_names) or '无'}\n\n"
        f"## 仅简略记录角色（mentioned，由程序记录，不要返回）\n"
        f"{', '.join(name for name, mode in passive_modes.items() if mode == 'mentioned') or '无'}\n\n"
        f"## 继承角色（memory/offscreen，由程序继承，不要返回）\n"
        f"{', '.join(name for name, mode in passive_modes.items() if mode in {'memory', 'offscreen'}) or '无'}\n\n"
        f"{inherited_block}"
        + ("## 轻量末态模式\n只抽取影响下一章衔接的必要字段，避免复述人物卡。\n\n" if lightweight else "")
        + f"## 本章节《{chapter_title}》正文\n{chapter_text}\n\n"
        "## 任务\n"
        "请输出严格 JSON，只返回【详细末态角色】的完整末态；"
        "mentioned、memory、offscreen 由程序记录或继承，不得擅自补写位置、伤情。\n"
        "JSON 格式：\n"
        "{\n"
        '  "states": {\n'
        '    "<角色名>": {"alive": true/false, "outer_alive": true/false, "current_layer_alive": true/false, "life_status": "alive_confirmed/death_reported/death_suspected/death_confirmed/fake_death_revealed/revived_confirmed/unknown", "life_status_confidence": 0.0, "death_evidence_level": "none/hearsay/indirect/direct/witnessed/body_verified/official_record/explicit_scene/explicit_reveal/unknown", "death_sources": [{"speaker": "消息来源", "claim": "死亡说法"}], "previous_death_resolution": "fake_death/误报原因/空字符串", "resurrection_rule_id": "触发的世界规则或空字符串", "presence_type": "physical/hallucination/memory/dream/corpse/absent", "reality_layer": "reality/dream/therapy_reconstruction/memory/other", "reality_domain": "physical_reality/reconstructed_world/unknown", "reconstruction_type": "none/dream/therapy_reconstruction/rebirth/unknown", "local_frame": "present/memory/hallucination", "participation": "active/mentioned/absent/offscreen/unknown", "state_confidence": 0.0, "evidence": "正文依据", "timeline": "...", "condition": "...", "location": "...", "identity_status": "...", "note": "..."},\n'
        "    ...\n"
        "  }\n"
        "}"
    )
    try:
        try:
            terminal_state_timeout = int(
                os.getenv("STORY_PLANNER_TERMINAL_STATE_TIMEOUT_SECONDS", "1200")
            )
        except ValueError:
            terminal_state_timeout = 1200
        terminal_state_timeout = max(900, min(terminal_state_timeout, 3600))
        result = None
        last_extract_error: Exception | None = None
        low_confidence_names: list[str] = []
        max_attempts = 2 if lightweight else 3
        for attempt in range(1, max_attempts + 1):
            try:
                current_user_prompt = user_prompt
                if low_confidence_names:
                    current_user_prompt += (
                        "\n\n## 上次抽取的定向复核要求\n"
                        "以下在正文中明确出现的角色置信度低于0.6："
                        + "、".join(low_confidence_names)
                        + "。请重新逐句定位其末态证据并填写 evidence；"
                        "不得用‘依据不足/状态未知’敷衍。"
                    )
                result = client.chat_completion_json(
                    system_prompt=system_prompt,
                    user_prompt=current_user_prompt,
                    temperature=0.2,
                    max_tokens=(
                        max(750, min(1600, len(active_names) * 180))
                        if lightweight
                        else max(1200, min(2600, len(active_names) * 240))
                    ),
                    timeout=terminal_state_timeout,
                )
                states = result.get("states", {}) if isinstance(result, dict) else {}
                if not isinstance(states, dict) or not states:
                    raise ValueError("末态响应为空或缺少 states 对象")
                states = {
                    str(name): state
                    for name, state in states.items()
                    if str(name) in active_names
                }
                if not states:
                    raise ValueError("末态响应未包含当前章节跟踪角色")
                low_confidence_names = []
                for name in active_names:
                    state = states.get(name)
                    if not isinstance(state, dict):
                        low_confidence_names.append(name)
                        continue
                    try:
                        confidence = float(state.get("state_confidence", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        confidence = 0.0
                    evidence = str(state.get("evidence") or "").strip()
                    if confidence < 0.6 or not evidence or evidence in {"依据不足", "状态未知"}:
                        low_confidence_names.append(name)
                if low_confidence_names and attempt < max_attempts:
                    print(
                        f"   ⚠️  末态抽取置信度不足（attempt={attempt}，"
                        + "、".join(low_confidence_names)
                        + "），仅定向重试当前章节"
                    )
                    continue
                break
            except Exception as extract_error:
                last_extract_error = extract_error
                if attempt < max_attempts:
                    print(
                        f"   ⚠️  末态抽取返回无效结果（attempt={attempt}，{extract_error}），"
                        "仅重试当前章节"
                    )
        else:
            raise last_extract_error or ValueError("末态抽取失败")
        # active 必须有详细 entry；模型最终仍漏写时使用显式低置信度兜底。
        for name in active_names:
            if name not in states:
                inherited = effective_inherited_states.get(name)
                if isinstance(inherited, dict):
                    states[name] = dict(inherited)
                    states[name].update(
                        {
                            "participation": "active",
                            "state_source": "inherited_fallback",
                            "state_confidence": min(
                                float(states[name].get("state_confidence", 0.25) or 0.25),
                                0.25,
                            ),
                            "note": "本章实际出场，但详细末态抽取失败",
                            "evidence": "模型未返回该 active 角色",
                        }
                    )
                else:
                    states[name] = {
                        "alive": True,
                        "outer_alive": True,
                        "current_layer_alive": True,
                        "life_status": "unknown",
                        "life_status_confidence": 0.25,
                        "death_evidence_level": "none",
                        "death_sources": [],
                        "previous_death_resolution": "",
                        "resurrection_rule_id": "",
                        "presence_type": "unknown",
                        "reality_layer": "reality",
                        "timeline": "本章时间线",
                        "condition": "末态抽取失败",
                        "location": "未知",
                        "identity_status": "未知",
                        "participation": "active",
                        "state_source": "deterministic_fallback",
                        "state_confidence": 0.25,
                        "evidence": "模型未返回该 active 角色",
                        "note": "本章实际出场，但详细末态抽取失败",
                    }
        for name in active_names:
            state = states.get(name)
            if isinstance(state, dict):
                state["participation"] = "active"
                state["offscreen_streak"] = 0
                state["state_source"] = "llm"
        for name, mode in passive_modes.items():
            states[name] = _passive_terminal_state(
                name,
                mode,
                effective_inherited_states,
            )
        normalized, issues = normalize_terminal_states(
            states,
            effective_inherited_states,
            narrative_contract=narrative_contract,
        )
        if issues:
            print("   ⚠️  末态规范化：" + "；".join(issues))
        return normalized
    except Exception as e:
        print(f"   ⚠️  末态抽取失败（{e}），使用低置信度继承末态")
        fallback_states: Dict[str, Any] = {}
        for name in active_names:
            inherited = effective_inherited_states.get(name)
            if isinstance(inherited, dict):
                state = dict(inherited)
                source = "inherited_fallback"
            else:
                state = {
                    "alive": True,
                    "outer_alive": True,
                    "current_layer_alive": True,
                    "life_status": "unknown",
                    "life_status_confidence": 0.25,
                    "death_evidence_level": "unknown",
                    "death_sources": [],
                    "previous_death_resolution": "",
                    "resurrection_rule_id": "",
                    "presence_type": "unknown",
                    "reality_layer": "unknown",
                    "timeline": "本章末态抽取失败",
                    "condition": "未知",
                    "location": "未知",
                    "identity_status": "未知",
                    "note": "末态抽取失败，禁止作为确定事实",
                }
                source = "deterministic_fallback"
            state["state_source"] = source
            state["participation"] = "active"
            state["state_confidence"] = min(
                float(state.get("state_confidence", 0.25) or 0.25),
                0.25,
            )
            state["evidence"] = f"末态抽取失败：{type(e).__name__}"
            fallback_states[name] = state
        for name, mode in passive_modes.items():
            fallback_states[name] = _passive_terminal_state(
                name,
                mode,
                effective_inherited_states,
            )
        normalized, _issues = normalize_terminal_states(
            fallback_states,
            effective_inherited_states,
            narrative_contract=narrative_contract,
        )
        return normalized


def terminal_states_are_cacheable(states: Dict[str, Any]) -> bool:
    """Persist states when every active character has a reliable LLM result."""
    if not isinstance(states, dict) or not states:
        return False
    relevant_state_found = False
    for state in states.values():
        if not isinstance(state, dict):
            return False
        participation = str(state.get("participation") or "").strip().lower()
        if participation in {"mentioned", "absent", "offscreen"}:
            continue
        relevant_state_found = True
        if state.get("state_source") != "llm":
            return False
        try:
            confidence = float(state.get("state_confidence", 0.0))
        except (TypeError, ValueError):
            return False
        if confidence < 0.6:
            return False
    return relevant_state_found


def terminal_states_have_life_schema(states: Dict[str, Any]) -> bool:
    """Return whether a checkpoint carries the evidence-based life-state fields."""
    if not isinstance(states, dict) or not states:
        return False
    required = {
        "life_status",
        "life_status_confidence",
        "death_evidence_level",
    }
    return all(
        isinstance(state, dict) and required.issubset(state)
        for state in states.values()
    )


def format_terminal_states_for_prompt(
    states: Dict[str, Any],
    label: str = "前序节点末态（不可违反，否则即穿帮）",
) -> str:
    """把末态字典格式化成可塞进 prompt 的多行文本。供 What-If/结局生成调用。"""
    if not states:
        return ""
    lines = [f"【{label}】"]
    for name, st in states.items():
        if not isinstance(st, dict):
            continue
        alive = st.get("alive")
        life_status = str(st.get("life_status") or "").strip().lower()
        if not life_status:
            life_status = "alive_confirmed" if alive else "death_confirmed"
        life_labels = {
            "alive_confirmed": "确认存活",
            "death_reported": "传闻死亡（未确认）",
            "death_suspected": "疑似死亡（未确认）",
            "death_confirmed": "确认死亡",
            "fake_death_revealed": "假死已揭示、确认存活",
            "revived_confirmed": "依世界规则确认复活",
            "unknown": "生死未知",
        }
        alive_str = life_labels.get(life_status, "确认存活" if alive else "确认死亡")
        life_confidence = st.get(
            "life_status_confidence", st.get("state_confidence", 0.0)
        )
        death_evidence = st.get("death_evidence_level", "unknown") or "unknown"
        cond = st.get("condition", "") or "未明示"
        loc = st.get("location", "") or "未明示"
        presence = st.get("presence_type", "") or ("physical" if alive else "absent")
        layer = st.get("reality_layer", "") or "reality"
        domain = st.get("reality_domain", "") or "unknown"
        reconstruction_type = st.get("reconstruction_type", "") or "unknown"
        local_frame = st.get("local_frame", "") or "present"
        participation = st.get("participation", "") or "unknown"
        timeline = st.get("timeline", "") or "未明示"
        identity = st.get("identity_status", "") or "未明示"
        lines.append(
            f"  - {name}：{alive_str}；生命状态码：{life_status}；置信度：{life_confidence}；"
            f"死亡证据级别：{death_evidence}；出现方式：{presence}；旧现实层：{layer}；"
            f"外层现实域：{domain}；重构类型：{reconstruction_type}；局部框架：{local_frame}；参与方式：{participation}；"
            f"时间线：{timeline}；伤情/状态：{cond}；位置：{loc}；身份：{identity}"
        )
    lines.append(
        "硬约束：\n"
        "  1. 只有 life_status=death_confirmed 才是死亡硬约束：不得在现实层复活、完整对话或主动决定，最多以回忆/幻觉/尸体形式存在。\n"
        "  2. death_reported/death_suspected 只是传闻或疑点，不得把传闻/疑似死亡直接写成确认死亡，也不得据此锁死角色；后文可以证实存活、假死或死亡。\n"
        "  3. 标记为确认存活的角色不得无铺垫地死亡；若本章必须让其死，必须给出与原著一致的直接证据、铺垫与因果。\n"
        "  4. fake_death_revealed 必须解释此前误报/假死；revived_confirmed 只允许在世界契约明确许可且写出 resurrection_rule_id 时使用。鬼魂、梦境、回忆不算复活。\n"
        "  5. 伤情/位置可在合理范围内推进，但不得逆转（如已断手不得无理由复原）。\n"
        "  6. 死亡方式唯一性：上方确认死亡角色的死亡时间/地点/方式，以其伤情状态所述为准，"
        "     本结局不得另编一套新的死法（例如已在书房小产失血而亡的角色，不得改写成在别处自刎/坠亡）。"
        "     若上方对某角色的死因未明示，则本结局对其死因的交代必须与前七章正文所述保持一致，不得凭空新增。\n"
        "  7. 关键数字（如死难人数、年数、在位时长）必须与前文一致，一处确立即全篇沿用，不得随场次漂移。\n"
        "  8. 收束到共同下游前，只有明确生死、现实域和不可逆身份冲突才属于硬约束；"
        "时间线、地点和局部出现方式允许通过桥接场景推进。若已揭示死亡而下游仍是现实层活人剧情，"
        "必须先写明确的治疗重构/梦境过渡，否则不得收束。\n"
        "  9. 未提及/未知字段应继承已有状态；末场空值、unknown、未出场、位置未明等标记不是新事实，"
        "不得仅因字段缺失判定冲突。时间线和地点变化可以通过过渡场景解决；普通身份粒度差异仅作警告，"
        "优先通过桥接场景解决。"
    )
    return "\n".join(lines) + "\n\n"


def _merge_character_alias_cards(
    characters: list[dict],
) -> tuple[list[dict], dict[str, str], list[dict]]:
    """Merge cards that explicitly identify one another as aliases."""
    from copy import deepcopy
    import re as _re

    merged: list[dict] = []
    alias_to_canonical: dict[str, str] = {}
    alias_conflicts: list[dict] = []
    for raw in characters:
        current = deepcopy(raw)
        name = str(current.get("name") or "").strip()
        if not name:
            continue
        current_aliases = {
            str(alias).strip()
            for alias in (current.get("aliases") or [])
            if str(alias).strip() and str(alias).strip() != name
        }
        relation_text = "；".join(
            str(item)
            for item in (current.get("key_relations") or [])
            if str(item).strip()
        )
        distinct_aliases = {
            alias
            for alias in current_aliases
            if alias not in name
            and _re.search(
                rf"与[^：；]{{0,20}}{_re.escape(alias)}[^：；]{{0,20}}的关系",
                relation_text,
            )
        }
        for alias in sorted(distinct_aliases):
            alias_conflicts.append(
                {
                    "character": name,
                    "alias": alias,
                    "reason": "key_relations 将该名称描述为独立角色",
                }
            )
        current_aliases -= distinct_aliases
        target = None
        for existing in merged:
            existing_name = str(existing.get("name") or "").strip()
            existing_aliases = {
                str(alias).strip()
                for alias in (existing.get("aliases") or [])
                if str(alias).strip()
            }
            if (
                name == existing_name
                or name in existing_aliases
                or existing_name in current_aliases
            ):
                target = existing
                break
        if target is None:
            current["aliases"] = sorted(current_aliases)
            merged.append(current)
            continue

        canonical_name = str(target.get("name") or name)
        combined_aliases = {
            *(target.get("aliases") or []),
            *current_aliases,
            name,
        }
        combined_aliases.discard(canonical_name)
        target["aliases"] = sorted(str(alias) for alias in combined_aliases if alias)
        for key, value in current.items():
            if key in {"name", "aliases"} or value in (None, "", [], {}):
                continue
            if key == "role" and str(value).startswith("主"):
                target[key] = value
            elif isinstance(value, list):
                existing_values = target.get(key)
                if not isinstance(existing_values, list):
                    existing_values = []
                target[key] = existing_values + [
                    item for item in value if item not in existing_values
                ]
            elif target.get(key) in (None, "", [], {}):
                target[key] = value

    # Canonical names and aliases share one ownership table.  If a canonical
    # name is also claimed as somebody else's alias, do not let iteration order
    # silently overwrite the map and route scenes to a random character.
    alias_owners: dict[str, set[str]] = {}
    for character in merged:
        canonical = str(character.get("name") or "").strip()
        if canonical:
            alias_owners.setdefault(canonical, set()).add(canonical)
        for alias in character.get("aliases", []) or []:
            clean = str(alias or "").strip()
            if clean:
                alias_owners.setdefault(clean, set()).add(canonical)
    for alias, owners in alias_owners.items():
        if len(owners) == 1:
            alias_to_canonical[alias] = next(iter(owners))
            continue
        alias_conflicts.append(
            {
                "character": "、".join(sorted(owners)),
                "alias": alias,
                "severity": "warning",
                "reason": "多个角色共享该称呼，无法唯一归属",
            }
        )

    return merged, alias_to_canonical, alias_conflicts


def normalize_legacy_worldview_schema(wv: dict) -> dict:
    """Migrate rich legacy worldview fields into the current chapter-branch schema."""
    from copy import deepcopy

    if not isinstance(wv, dict):
        return wv
    normalized = deepcopy(wv)
    characters = normalized.get("characters", [])
    if not isinstance(characters, list):
        characters = []
    characters = [item for item in characters if isinstance(item, dict)]

    legacy_protagonist = normalized.get("protagonist")
    if isinstance(legacy_protagonist, dict):
        target = next(
            (
                item
                for item in characters
                if (
                    legacy_protagonist.get("id")
                    and item.get("id") == legacy_protagonist.get("id")
                )
                or (
                    legacy_protagonist.get("name")
                    and item.get("name") == legacy_protagonist.get("name")
                )
            ),
            None,
        )
        if target is None:
            target = {}
            characters.insert(0, target)
        for key, value in legacy_protagonist.items():
            if value not in (None, "", [], {}) and not target.get(key):
                target[key] = deepcopy(value)
        target.setdefault("role", "主角")

    protagonist = next(
        (item for item in characters if str(item.get("role", "")).startswith("主")),
        None,
    )
    if protagonist is not None:
        axes = normalized.get("persona_axes")
        legacy_axes = protagonist.get("persona_axes")
        if (not isinstance(axes, list) or not axes) and isinstance(legacy_axes, dict):
            normalized["persona_axes"] = [
                {"key": str(key), "description": str(description)}
                for key, description in legacy_axes.items()
                if key
            ]

        current_secrets = protagonist.get("secrets")
        if not isinstance(current_secrets, list) or len(current_secrets) < 3:
            migrated_secrets = []
            for item in normalized.get("secrets", []) or []:
                if isinstance(item, dict):
                    value = item.get("content") or item.get("name")
                else:
                    value = item
                if value:
                    migrated_secrets.append(str(value))
            if migrated_secrets:
                protagonist["secrets"] = migrated_secrets

    characters, alias_to_canonical, alias_conflicts = _merge_character_alias_cards(
        characters
    )
    for relation in normalized.get("relationship_map", []) or []:
        if not isinstance(relation, dict):
            continue
        for field in ("from", "to"):
            value = str(relation.get(field) or "").strip()
            if value in alias_to_canonical:
                relation[field] = alias_to_canonical[value]
    normalized["characters"] = characters
    normalized["character_alias_map"] = alias_to_canonical
    normalized["character_alias_conflicts"] = alias_conflicts
    normalized["narrative_reality_contract"] = infer_narrative_reality_contract(
        normalized
    )
    # Keep a deterministic narrative POV contract alongside the normalized
    # worldview.  Existing checkpoints may not contain it; infer it lazily from
    # metadata here and let ``analyze_novel`` enrich it with chapter samples.
    existing_pov = normalized.get("narrative_pov_contract")
    existing_pov_label = (
        _normalize_pov_label(
            str(existing_pov.get("perspective") or existing_pov.get("mode") or "")
        )
        if isinstance(existing_pov, dict)
        else None
    )
    if existing_pov_label in (None, "unknown"):
        normalized["narrative_pov_contract"] = infer_narrative_perspective_contract(
            normalized
        )
    return normalized


def worldview_schema_issues(wv: dict) -> List[str]:
    """Return all chapter-branch worldview schema problems."""
    import re as _re

    issues: List[str] = []
    if not isinstance(wv, dict):
        return ["世界观不是 JSON 对象"]

    for conflict in wv.get("character_alias_conflicts", []) or []:
        if not isinstance(conflict, dict):
            continue
        # Shared generic称呼 (for example “老师” or “师兄”) is ambiguous for
        # route matching, but it is not malformed worldview data.  The
        # matcher deliberately ignores such aliases; treating them as a
        # schema error would make otherwise usable novels fail during the
        # repair gate.  Conflicts that explicitly describe one alias as an
        # independent character remain blocking so accidental card merges are
        # still surfaced.
        conflict_reason = str(conflict.get("reason") or "").strip()
        conflict_severity = str(conflict.get("severity") or "").strip().lower()
        if conflict_severity in {"warning", "warn", "info", "non_blocking"} or any(
            marker in conflict_reason
            for marker in ("多个角色共享", "共享称呼", "无法唯一归属", "ambiguous alias")
        ):
            continue
        issues.append(
            "角色别名冲突："
            f"{conflict.get('character', '未知角色')} 不得把 "
            f"{conflict.get('alias', '未知名称')} 作为别名，"
            f"{conflict_reason or '该名称被描述为独立角色'}"
        )

    chars = wv.get("characters", [])
    if not isinstance(chars, list):
        chars = []
        issues.append("characters 不是数组")
    protagonist = next(
        (c for c in chars if isinstance(c, dict) and c.get("role", "").startswith("主")),
        None,
    )
    if not protagonist:
        issues.append("缺少主角角色（role 以'主'开头的角色）")
    elif not (protagonist.get("fatal_flaw") or "").strip():
        issues.append("主角 fatal_flaw 为空，需从原文找出主角最致命的软肋")

    axes = wv.get("persona_axes", [])
    if not isinstance(axes, list) or len(axes) < 2:
        issues.append(
            f"persona_axes 不足（{len(axes) if isinstance(axes, list) else 0} 条，需≥2）"
        )
    else:
        is_romance = bool(wv.get("is_romance_oriented", False))
        affection_axis_count = 0
        for axis in axes:
            if not isinstance(axis, dict):
                issues.append("persona_axes 包含非对象条目")
                continue
            key = axis.get("key", "")
            if key.startswith("affection_"):
                affection_axis_count += 1
            if not key or not _re.match(r"^[a-z][a-z_0-9]*$", key):
                issues.append(
                    f"persona_axes key 不合法：'{key}'，需为英文 snake_case（如 courage/calmness）"
                )
        if is_romance and affection_axis_count == 0:
            issues.append("is_romance_oriented=true 时 persona_axes 至少需要一条 affection_ 轴")

    ending_dimensions = wv.get("ending_dimensions", [])
    if not isinstance(ending_dimensions, list) or len(ending_dimensions) < 2:
        issues.append("ending_dimensions 不足，需至少 2 条命运维度")

    secrets_total = sum(
        len(c.get("secrets") or []) for c in chars if isinstance(c, dict)
    )
    if secrets_total < 3:
        issues.append(f"全书角色 secrets 总数不足（{secrets_total} 条，需≥3）")

    for field, label in (("factions", "势力 factions"), ("hidden_threads", "伏笔 hidden_threads")):
        value = wv.get(field, [])
        if not isinstance(value, list) or not value:
            issues.append(f"{label} 为空")
    return issues


def _validate_worldview(wv: dict) -> tuple:
    """
    校验世界观档案完整性，返回 (ok: bool, reason: str)。
    ok=False 时 reason 描述第一个发现的问题，用于传给修复调用。
    """
    issues = worldview_schema_issues(wv)
    return (not issues, issues[0] if issues else "")


def _compact_worldview_for_repair(wv: dict) -> dict:
    """Create a valid, schema-preserving repair view without raw JSON slicing."""
    from copy import deepcopy

    compact = deepcopy(wv)
    characters = compact.get("characters")
    if isinstance(characters, list):
        essential_character_fields = {
            "id",
            "name",
            "aliases",
            "role",
            "background",
            "fatal_flaw",
            "secrets",
            "key_relations",
        }
        compact["characters"] = [
            {
                key: value
                for key, value in character.items()
                if key in essential_character_fields
            }
            for character in characters
            if isinstance(character, dict)
        ]

    def _trim(value):
        if isinstance(value, str):
            return value if len(value) <= 500 else value[:500] + "…"
        if isinstance(value, list):
            return [_trim(item) for item in value]
        if isinstance(value, dict):
            return {key: _trim(item) for key, item in value.items()}
        return value

    return _trim(compact)


def _repair_allowed_fields(reason: str) -> set[str] | None:
    """Limit a targeted repair to fields implicated by validation issues."""
    fields: set[str] = set()
    if "角色别名冲突" in reason or "缺少主角角色" in reason:
        fields.update(
            {"characters", "relationship_map", "character_alias_map", "character_alias_conflicts"}
        )
    if "fatal_flaw" in reason or "secrets" in reason:
        fields.update({"characters", "secrets"})
    if "persona_axes" in reason:
        fields.update({"persona_axes", "is_romance_oriented", "romance_targets"})
    if "ending_dimensions" in reason:
        fields.add("ending_dimensions")
    if "factions" in reason:
        fields.add("factions")
    if "hidden_threads" in reason:
        fields.add("hidden_threads")
    return fields or None


def _valid_repair_field(key: str, value) -> bool:
    import re

    if value in (None, "", [], {}):
        return False
    if key == "characters":
        return isinstance(value, list) and all(isinstance(item, dict) for item in value)
    if key == "persona_axes":
        return (
            isinstance(value, list)
            and len(value) >= 2
            and all(
                isinstance(item, dict)
                and re.match(r"^[a-z][a-z_0-9]*$", str(item.get("key") or ""))
                for item in value
            )
        )
    if key == "ending_dimensions":
        return isinstance(value, list) and len(value) >= 2
    if key in {"factions", "hidden_threads"}:
        return isinstance(value, list) and bool(value)
    if key in {"relationship_map", "romance_targets"}:
        return isinstance(value, list)
    if key in {"character_alias_map", "character_alias_conflicts"}:
        return isinstance(value, (dict, list))
    return True


def _repair_worldview(
    wv: dict,
    reason: str,
    text_sample: str,
    provider: str,
    output_language: str = None,
) -> dict:
    """
    针对校验失败原因，用轻量补录调用修复世界观档案的缺失/不合规字段。
    只补全有问题的字段，其余字段原样保留，避免重跑全量分析。
    """
    import json as _json
    client = LLMClient(provider=provider)
    lang_instr = get_lang_instruction(output_language)
    existing_json = _json.dumps(
        _compact_worldview_for_repair(wv),
        ensure_ascii=False,
    )
    system_prompt = (
        "你是世界观档案修复专家。给你一份不完整/不合规的世界观 JSON 档案和原文片段，"
        "请仅针对以下问题进行补全或修正，其余字段原样保留，输出完整修复后的 JSON。\n"
        f"【需要修复的问题】：{reason}\n\n"
        "修复规则：\n"
        "  1. persona_axes 必须为英文 snake_case key（如 courage/calmness/humanity/clarity）。\n"
        "  2. 若 is_romance_oriented=true，persona_axes 必须用 affection_<对象拼音> 格式。\n"
        "  3. fatal_flaw 必须从原文中找出主角最致命的软肋（情感软肋/能力短板/记忆伤痕），不得填空。\n"
        "  4. ending_dimensions 至少 2 条不重叠的命运维度（如 主角生死/复仇成败/关键人物生死）。\n"
        "  5. secrets 总数需≥3 条，从原文中真实存在的秘密中提取。\n"
        "  6. 输出严格 JSON，不要任何额外文字。"
        "  7. aliases 只能放同一人的本名/化名/称号；若 key_relations 把某名称写成朋友、兄弟、敌人等独立人物，"
        "必须从 aliases 移除，并把该人物恢复成独立 character 卡。"
        + ("\n" + lang_instr if lang_instr else "")
    )
    user_prompt = (
        f"## 原文片段（前 8000 字，供参考）\n{text_sample[:8000]}\n\n"
        f"## 现有档案（需补全/修正）\n{existing_json}\n\n"
        "请输出修复后的完整 JSON 档案。"
    )
    try:
        repaired = client.chat_completion_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=5000,
        )
        from copy import deepcopy

        merged = deepcopy(wv)
        allowed_fields = _repair_allowed_fields(reason)
        if isinstance(repaired, dict):
            for key, value in repaired.items():
                if (
                    (allowed_fields is None or key in allowed_fields)
                    and _valid_repair_field(key, value)
                ):
                    merged[key] = value
        print(f"   ✅ 世界观档案修复完成（修复原因：{reason}）")
        return merged
    except Exception as e:
        print(f"   ⚠️  世界观档案修复失败（{e}），使用原版继续")
        return wv


def ensure_novel_context_worldview(
    novel_context: NovelContext,
    full_text: str,
    provider: str,
    output_language: str = None,
) -> NovelContext:
    """Validate cached context worldview and refresh all derived prompt fields after repair."""
    cached_pov_contract = getattr(novel_context, "narrative_pov_contract", None)
    original_worldview = normalize_legacy_worldview_schema(
        getattr(novel_context, "worldview", {})
    )
    # A context checkpoint is the authoritative generation input.  Preserve a
    # valid contract stored on the context even if an older worldview snapshot
    # contains a different/unknown copy.
    if isinstance(cached_pov_contract, dict) and _normalize_pov_label(
        str(cached_pov_contract.get("perspective") or cached_pov_contract.get("mode") or "")
    ) not in (None, "unknown"):
        original_worldview["narrative_pov_contract"] = cached_pov_contract
    novel_context.worldview = original_worldview
    novel_context.narrative_pov_contract = infer_narrative_perspective_contract(
        original_worldview,
        chapters=getattr(novel_context, "chapter_summaries", []),
        style_samples=getattr(novel_context, "style_samples", ""),
    )
    original_worldview["narrative_pov_contract"] = novel_context.narrative_pov_contract
    issues = worldview_schema_issues(original_worldview)
    if not issues:
        if (
            getattr(novel_context, "branch_type_strategy_version", 0)
            == _BRANCH_TYPE_STRATEGY_VERSION
            and getattr(novel_context, "branch_type", "") in _BRANCH_TYPES
        ):
            novel_context.is_romance_oriented = (
                novel_context.branch_type == "romance"
            )
            return novel_context
        # 分支识别策略会随版本演进；即使缓存世界观结构合法，也必须刷新
        # branch_type，避免旧缓存中的 romance 快速判定继续污染新任务。
        novel_context.branch_type = detect_branch_type(
            original_worldview,
            getattr(novel_context, "style_samples", ""),
            provider,
            output_language=output_language,
            chapter_summaries=getattr(novel_context, "chapter_summaries", []),
        )
        novel_context.is_romance_oriented = novel_context.branch_type == "romance"
        novel_context.branch_type_strategy_version = _BRANCH_TYPE_STRATEGY_VERSION
        return novel_context

    reason = "；".join(issues)
    print(f"   ⚠️  缓存世界观校验未通过（{reason}），尝试补录修复...")
    worldview = _repair_worldview(
        original_worldview,
        reason,
        full_text,
        provider,
        output_language=output_language,
    )
    worldview = normalize_legacy_worldview_schema(worldview)
    remaining = worldview_schema_issues(worldview)
    if remaining:
        raise ValueError("世界观修复后仍不完整：" + "；".join(remaining))

    novel_context.worldview = worldview
    novel_context.narrative_pov_contract = infer_narrative_perspective_contract(
        worldview,
        chapters=getattr(novel_context, "chapter_summaries", []),
        style_samples=getattr(novel_context, "style_samples", ""),
    )
    worldview["narrative_pov_contract"] = novel_context.narrative_pov_contract
    novel_context.char_cards = _format_char_cards(worldview)
    novel_context.relationship_map = _format_relationship_map(worldview)
    novel_context.world_rules = _format_world_rules(worldview)
    novel_context.factions_text = _format_factions(worldview)
    novel_context.hidden_threads_text = _format_hidden_threads(worldview)
    novel_context.character_secrets_text = _format_character_secrets(worldview)
    novel_context.narrative_arc = worldview.get("narrative_arc", "")
    novel_context.persona_axes = worldview.get("persona_axes", [])
    novel_context.is_romance_oriented = bool(worldview.get("is_romance_oriented", False))
    novel_context.ending_dimensions = worldview.get("ending_dimensions", [])
    novel_context.character_behavior_anchors = _extract_behavior_anchors(
        worldview,
        getattr(novel_context, "chapter_summaries", []),
    )
    novel_context.branch_type = detect_branch_type(
        worldview,
        getattr(novel_context, "style_samples", ""),
        provider,
        output_language=output_language,
        chapter_summaries=getattr(novel_context, "chapter_summaries", []),
    )
    novel_context.is_romance_oriented = novel_context.branch_type == "romance"
    novel_context.branch_type_strategy_version = _BRANCH_TYPE_STRATEGY_VERSION
    print("   ✅ 缓存世界观修复完成，派生上下文已刷新")
    return novel_context


def filter_behavior_anchors_for_whatif(
    anchors: str,
    *,
    pivot_chapter_index: int | None,
) -> str:
    """Keep character rules and decisions up to the What-If fork point.

    A What-If replaces the sibling chapter after the pivot. Decisions from that
    replaced sibling are intentionally allowed to diverge and must not be fed
    to the consistency gate as immutable facts.
    """
    if pivot_chapter_index is None or not anchors:
        return anchors
    import re as _re
    max_chapter = int(pivot_chapter_index) + 1
    kept: list[str] = []
    for line in anchors.splitlines():
        match = _re.match(r"\s*第(\d+)章：", line)
        if match and int(match.group(1)) > max_chapter:
            continue
        kept.append(line)
    return "\n".join(kept)


def filter_behavior_anchors_for_bad_end(
    anchors: str,
    *,
    pivot_chapter_index: int | None,
) -> str:
    """Keep only facts before a Bad End fork and declare its authorized divergence.

    A death-corridor Bad End is the alternative choice made inside the pivot
    chapter itself.  The original pivot choice and every later decision must
    therefore not be audited as immutable facts.
    """
    if pivot_chapter_index is None or not anchors:
        return anchors
    import re as _re

    pivot_chapter_number = int(pivot_chapter_index) + 1
    kept: list[str] = []
    for line in anchors.splitlines():
        match = _re.match(r"\s*第(\d+)章：", line)
        if match and int(match.group(1)) >= pivot_chapter_number:
            continue
        kept.append(line)
    kept.append(
        "\n【Bad End 授权分歧】"
        f"允许反转第{pivot_chapter_number}章岔口选择及其直接后果；"
        "该章岔口发生前的事实、角色性格、世界规则仍不可修改。"
    )
    return "\n".join(kept)


def filter_behavior_anchors_for_ending(
    anchors: str,
    *,
    pivot_chapter_index: int | None,
) -> str:
    """Keep pre-pivot facts while allowing unresolved mysteries to be revealed.

    A parallel ending replaces the original continuation after its pivot.  Facts
    already established by that point remain immutable, but an unknown outcome
    (missing person, unconfirmed death, unknown identity, unresolved culprit)
    is not a permanent negative fact.  The ending may resolve it when the prose
    supplies a credible discovery process.
    """
    if pivot_chapter_index is None or not anchors:
        return anchors
    import re as _re

    pivot_chapter_number = int(pivot_chapter_index) + 1
    open_markers = (
        "去向未明",
        "下落不明",
        "生死未卜",
        "尚未确认",
        "未确认",
        "疑似死亡",
        "传闻死亡",
        "身份不明",
        "凶手未知",
        "真相未明",
        "失踪",
    )
    kept: list[str] = []
    open_mystery_found = False
    for line in anchors.splitlines():
        match = _re.match(r"\s*第(\d+)章：(.*)", line)
        if not match:
            kept.append(line)
            continue
        chapter_number = int(match.group(1))
        if chapter_number > pivot_chapter_number:
            continue
        normalized_line = f"第{chapter_number}章：{match.group(2).strip()}"
        if any(marker in normalized_line for marker in open_markers):
            kept.append(f"【开放悬念】{normalized_line}")
            open_mystery_found = True
        else:
            kept.append(normalized_line)
    kept.append(
        "\n【平行结局授权边界】"
        f"第{pivot_chapter_number}章及此前已经明确发生的事件、确认生死和世界规则不可修改；"
        "分叉点之后的原著决定不作为本结局的既定事实。"
    )
    if open_mystery_found:
        kept.append(
            "【开放悬念处理】标为开放悬念的失踪、疑似死亡、身份或去向未知，"
            "允许在结局中通过合理过程揭示；不得仅因原著未给答案就判为穿帮，"
            "但禁止角色无调查、无转场、无因果铺垫地突然出现并完整交代真相。"
        )
    return "\n".join(kept)


def _extract_behavior_anchors(worldview: Dict[str, Any], chapter_summaries: List[str]) -> str:
    """
    从角色卡 + 逐章摘要的【人物决策与后果】标签里提取行为指纹，
    生成一段可直接注入 prompt 的文本，用于约束 What-If/结局生成时的角色行为一致性。
    """
    import re as _re
    lines = ["【角色行为指纹（What-If / 结局必须遵守——角色的反应模式已在原著中定型）】"]

    # 1. 从角色卡提取性格/动机/软肋
    chars = worldview.get("characters", [])
    for c in chars[:8]:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        personality = c.get("personality", "")
        motivation = c.get("core_motivation", "")
        flaw = c.get("fatal_flaw", "")
        speech = c.get("speech_style", "")
        if not name:
            continue
        parts = []
        if personality:
            parts.append(f"性格：{personality}")
        if motivation:
            parts.append(f"核心驱动：{motivation}")
        if flaw:
            parts.append(f"软肋：{flaw}")
        if speech:
            parts.append(f"说话方式：{speech}")
        if parts:
            lines.append(f"▪ {name}（{c.get('role','')}）— " + "；".join(parts))

    # 2. 从各章摘要的【人物决策与后果】段落提取关键决策史
    decision_lines = []
    pattern = _re.compile(r"【人物决策与后果】(.*?)(?=【|\Z)", _re.DOTALL)
    for i, summary in enumerate(chapter_summaries):
        m = pattern.search(summary)
        if not m:
            continue
        block = m.group(1).strip()
        # 每条决策取前80字，避免过长
        for line in block.split("\n"):
            line = line.strip().lstrip("·•-– ").rstrip()
            if len(line) > 10:
                decision_lines.append(f"  第{i+1}章：{line[:100]}")

    if decision_lines:
        lines.append("\n【各章已定型的关键决策（What-If 不得直接逆转，只能从分叉点衍生新走向）】")
        lines.extend(decision_lines[:30])  # 最多30条，防 prompt 过长

    lines.append(
        "\n使用方式：写 What-If / 结局时，上方每个角色的性格/动机/软肋是硬约束——"
        "即使走向不同，角色的反应方式、决策逻辑必须与此一致。"
        "『决策史』是原著已定型的选择，What-If 从岔路口之后才可以改变，岔路口之前的已定型决策不可逆转。"
    )
    return "\n".join(lines)


def check_content_consistency(
    content: str,
    title: str,
    char_cards: str,
    fixed_facts: str,
    provider: str,
    output_language: str = None,
) -> tuple:
    """
    对生成内容做轻量一致性检查：角色行为是否符合原著设定、是否违反既定事实。
    返回 (ok: bool, issues: str)。ok=False 时 issues 描述发现的穿帮点。
    审核调用本身失败时按非阻塞处理；明确发现硬冲突时由调用方执行有界自愈。
    """
    client = LLMClient(provider=provider)
    lang_instr = get_lang_instruction(output_language)
    # 只取 content 前后各 2000 字检查，节省 token
    sample = content[:2000] + ("\n\n……（中间省略）……\n\n" + content[-2000:] if len(content) > 4000 else "")
    system_prompt = (
        "你是视觉小说叙事一致性审核员。给你一段生成内容，你需要检查它是否与原著角色设定和既定事实相符。\n"
        "检查维度（按优先级）：\n"
        "  1. 角色行为一致性：角色的决策逻辑、反应方式是否符合角色卡里的性格/动机/软肋？\n"
        "     特别关注：① 角色是否做出了与其核心动机完全相反且无铺垫的决定；② 软肋是否被随意无视。\n"
        "  2. 事实一致性：是否违反了【既定事实】里列出的任何一条？\n"
        "     特别关注：① 已死角色是否复活出现完整对话；② 已发生事件是否被改写。\n"
        "  3. 只报告明显穿帮，不要因为走向不同（What-If 本就是平行线）而误报。\n"
        "  4. 【开放悬念】不是永久否定事实：失踪、去向未明、疑似死亡、身份不明可以在平行结局中被合理揭示；"
        "只有缺少寻找/调查/救援/转场等因果过程时才报告问题，不得仅因原著没有揭晓就判穿帮。\n"
        "输出严格 JSON：{\"ok\": true/false, \"issues\": [\"穿帮描述1\", ...]} \n"
        "若无穿帮则 ok=true, issues=[]。"
        + ("\n" + lang_instr if lang_instr else "")
    )
    user_prompt = (
        f"## 角色卡\n{char_cards[:3000]}\n\n"
        f"## 既定事实清单\n{fixed_facts[:2000] if fixed_facts else '（未提供）'}\n\n"
        f"## 待审核内容《{title}》（节选）\n{sample}"
    )
    try:
        result = client.chat_completion_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0,
            max_tokens=800,
        )
        ok = bool(result.get("ok", True))
        issues = result.get("issues", [])
        return ok, "; ".join(str(i) for i in issues) if issues else ""
    except Exception as e:
        return True, f"（审核调用失败：{e}）"


def repair_content_consistency_or_raise(
    content: str,
    title: str,
    char_cards: str,
    fixed_facts: str,
    provider: str,
    output_language: str = None,
) -> str:
    """Audit prose, try one exact patch and one bounded segment rewrite."""
    ok, issues = check_content_consistency(
        content,
        title,
        char_cards,
        fixed_facts,
        provider,
        output_language,
    )
    if ok:
        return content

    client = LLMClient(provider=provider)
    lang_instr = get_lang_instruction(output_language)
    def _request_replacements(
        source: str,
        repair_issues: str,
        *,
        segment_rewrite: bool,
    ) -> list[dict[str, Any]]:
        if segment_rewrite:
            instruction = (
                "上一次最小补丁没有解决问题。请重写承载冲突的最小连续场景或段落，补齐必要的调查、"
                "寻找、救援、时间推进或因果转场；不要重写整章。old 必须逐字复制完整冲突段落，"
                "new 必须是可直接替换的完整修订段落，最多返回 6 个替换。"
            )
        else:
            instruction = (
                "请只针对审核指出的穿帮问题给出最小文本补丁，不要重写整章。"
                "old 必须逐字复制自原文且足够精确，new 是替换后的文本。"
                "最多返回 12 个替换，禁止改动无问题段落。"
            )
        patch_result = client.chat_completion_json(
            system_prompt=(
                "你是资深视觉小说修订编辑。" + instruction
                + ("\n" + lang_instr if lang_instr else "")
            ),
            user_prompt=(
                f"## 待修订内容《{title}》\n{source}\n\n"
                f"## 审核发现的问题\n{repair_issues}\n\n"
                f"## 角色卡\n{char_cards[:4000]}\n\n"
                f"## 既定事实\n{fixed_facts[:3000] if fixed_facts else '（未提供）'}\n\n"
                "输出严格 JSON："
                '{"replacements":[{"old":"原文中的精确片段","new":"修复后的片段"}]}。'
            ),
            temperature=0.15,
            max_tokens=3000 if segment_rewrite else 2000,
        )
        return (
            patch_result.get("replacements", [])
            if isinstance(patch_result, dict)
            else []
        )

    def _apply_replacements(
        source: str,
        replacements: list[dict[str, Any]],
        *,
        limit: int,
    ) -> tuple[str, int]:
        repaired_source = source
        applied_count = 0
        for replacement in replacements[:limit]:
            if not isinstance(replacement, dict):
                continue
            old = str(replacement.get("old", ""))
            new = str(replacement.get("new", ""))
            if not old or old not in repaired_source or old == new:
                continue
            repaired_source = repaired_source.replace(old, new, 1)
            applied_count += 1
        return repaired_source, applied_count

    repaired, applied = _apply_replacements(
        content,
        _request_replacements(content, issues, segment_rewrite=False),
        limit=12,
    )
    if applied == 0:
        repaired = content
        repaired_issues = issues
    else:
        repaired_ok, repaired_issues = check_content_consistency(
            repaired,
            title,
            char_cards,
            fixed_facts,
            provider,
            output_language,
        )
        if repaired_ok:
            print(f"   ✅ [防穿帮]《{title}》已完成定点修复并通过复检")
            return repaired

    segment_repaired, segment_applied = _apply_replacements(
        repaired,
        _request_replacements(
            repaired,
            repaired_issues or issues,
            segment_rewrite=True,
        ),
        limit=6,
    )
    if segment_applied == 0:
        raise ContentConsistencyRepairRequired(
            f"《{title}》场景级修复未返回可应用的精确补丁：{repaired_issues or issues}",
            content=repaired,
            issues=repaired_issues or issues,
        )
    final_ok, final_issues = check_content_consistency(
        segment_repaired,
        title,
        char_cards,
        fixed_facts,
        provider,
        output_language,
    )
    if not final_ok:
        raise ContentConsistencyRepairRequired(
            f"《{title}》场景级修复后仍未通过：{final_issues or repaired_issues or issues}",
            content=segment_repaired,
            issues=final_issues or repaired_issues or issues,
        )
    print(f"   ✅ [防穿帮]《{title}》已完成场景级修复并通过复检")
    return segment_repaired


_BRANCH_TYPES = ("default", "death_corridor", "romance", "fantasy", "modular")
_BRANCH_TYPE_STRATEGY_VERSION = 2


def _branch_type_summary_evidence(chapter_summaries: List[str]) -> str:
    """Build evenly distributed evidence without dropping the end of long novels."""
    summaries = [
        (index, str(summary).strip())
        for index, summary in enumerate(chapter_summaries or [])
        if str(summary).strip()
    ]
    if not summaries:
        return ""

    max_chapters = 200
    if len(summaries) > max_chapters:
        selected_positions = {
            round(position * (len(summaries) - 1) / (max_chapters - 1))
            for position in range(max_chapters)
        }
        summaries = [summaries[position] for position in sorted(selected_positions)]

    max_chars = 60000
    per_chapter_chars = max(160, min(700, max_chars // len(summaries) - 16))
    evidence = "\n\n".join(
        f"【第{original_index + 1}章】{summary[:per_chapter_chars]}"
        for original_index, summary in summaries
    )
    return evidence[:max_chars]


def detect_branch_type(
    worldview: Dict[str, Any],
    style_samples: str,
    provider: str,
    output_language: str = None,
    chapter_summaries: List[str] = None,
) -> str:
    """
    根据世界观档案、全书章节摘要和文风样本，自动判断分支类型。
    专项模式必须同时满足：占比至少 40%、领先第二名至少 8%，且通过
    对应玩法的准入校验；否则使用 default 综合剧情模式。

    返回五种类型之一：
      default         通用（软分支+What-If+数值多结局）
      death_corridor  死亡回廊（Bad End型短分支，恐怖/生存/悬疑/强剧情短篇）
      romance         恋爱向（软分支+数值多结局，无What-If）
      fantasy         玄幻修仙（What-If，无软分支，无数值多结局）
      modular         模块化轮毂（调查式软分支，无数值/What-If/多结局）
    """
    import json as _json

    client = LLMClient(provider=provider)
    summaries = [
        str(summary).strip()
        for summary in (chapter_summaries or [])
        if str(summary).strip()
    ]
    summary_evidence = _branch_type_summary_evidence(chapter_summaries or [])
    genre_signals = {
        "genre": worldview.get("genre", ""),
        "narrative_arc": (worldview.get("narrative_arc") or "")[:3000],
        "world_rules": worldview.get("world_rules") or [],
        "key_events": (worldview.get("key_events") or [])[:20],
        "ending_dimensions": worldview.get("ending_dimensions") or [],
        "character_roles": [
            {
                "name": c.get("name", ""),
                "role": c.get("role", ""),
                "aliases": c.get("aliases", []),
            }
            for c in (worldview.get("characters") or [])[:12]
            if isinstance(c, dict)
        ],
        "is_romance_oriented": worldview.get("is_romance_oriented", False),
        "romance_targets": worldview.get("romance_targets") or [],
    }
    style_sample_short = (style_samples or "")[:1200]

    system_prompt = (
        "你是视觉小说玩法类型分析师。你判断的不是作品含有哪些题材标签，而是玩家在大多数章节里主要做什么。\n"
        "请结合全书章节摘要，估算五类玩法机制覆盖关键章节和关键事件的占比，scores 五项之和应约等于 1。\n\n"
        "类型定义与准入条件：\n"
        "  default — 综合剧情型。人物命运、成长、家庭、友情、恋爱等多种机制混合，没有一种专项玩法占明显优势。\n"
        "  death_corridor — 生存型。主角持续面临即时死亡，错误选择可直接 Bad End，求生/逃脱比查明真相更重要。\n"
        "  romance — 恋爱路线型。必须是同一个主角、同一可选择阶段至少两位有效恋爱对象，玩家能通过偏向谁进入不同角色结局，且感情选择覆盖大部分章节。不同主角、前世今生、已错过或已死亡人物不得合并成可攻略池。\n"
        "  fantasy — 成长战斗型。修炼、升级、能力成长、装备资源或战斗体系持续推动主要章节；仅有鬼神、法术、阴司等超自然世界观不算。\n"
        "  modular — 调查探索型。搜集线索、验证证词、探索地点、破解规则、查案或任务模块是主要交互机制。民俗怪谈若核心是调查真相和破解禁忌，应归 modular，而不是因存在鬼神归 fantasy。\n\n"
        "eligibility 表示专项模式是否满足上述全部准入条件；default 不需要准入。\n"
        "is_romance_oriented 只是弱信号，绝不能直接决定 romance。\n"
        "输出严格 JSON："
        "{\"scores\":{\"default\":0.0,\"death_corridor\":0.0,\"romance\":0.0,\"fantasy\":0.0,\"modular\":0.0},"
        "\"eligibility\":{\"death_corridor\":true,\"romance\":false,\"fantasy\":false,\"modular\":true},"
        "\"coverage\":{\"death_corridor\":0,\"romance\":0,\"fantasy\":0,\"modular\":0},"
        "\"reason\":\"说明主导玩法、次要玩法及未通过的准入条件\"}"
    )
    user_prompt = (
        f"世界观与角色信号：{_json.dumps(genre_signals, ensure_ascii=False)}\n\n"
        f"全书章节摘要（共{len(summaries)}章）：\n{summary_evidence or '未提供章节摘要'}\n\n"
        f"文风样本（仅作辅助，不得凌驾于玩法机制）：\n{style_sample_short}"
    )
    try:
        result = client.chat_completion_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0,
            max_tokens=700,
        )
        scores_raw = result.get("scores") if isinstance(result, dict) else None
        eligibility = result.get("eligibility") if isinstance(result, dict) else None
        if not isinstance(scores_raw, dict) or not isinstance(eligibility, dict):
            raise ValueError("缺少 scores 或 eligibility 字段")
        score_values = {
            branch_type: max(0.0, float(scores_raw.get(branch_type, 0.0)))
            for branch_type in _BRANCH_TYPES
        }
        score_total = sum(score_values.values())
        if score_total <= 0:
            raise ValueError("scores 总和必须大于 0")
        scores = {
            branch_type: value / score_total
            for branch_type, value in score_values.items()
        }
        ranked = sorted(
            scores.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        winner, winner_score = ranked[0]
        runner_up_score = ranked[1][1]
        reason = str(result.get("reason") or "")
        eligible = winner == "default" or eligibility.get(winner) is True
        if (
            winner == "default"
            or winner_score < 0.40
            or winner_score - runner_up_score < 0.08
            or not eligible
        ):
            detected = "default"
        else:
            detected = winner
        print(
            f"   🎯 分支类型检测：{detected}（主导={winner}:{winner_score:.2f}，"
            f"次高={runner_up_score:.2f}，准入={eligible}，{reason}）"
        )
        return detected
    except Exception as e:
        print(f"   ⚠️  分支类型检测失败（{e}），回退到 default")
        return "default"


def analyze_novel(
    chapters: list,
    full_text: str,
    provider: str,
    output_language: str = None,
    worldview_override: Dict[str, Any] = None,
) -> NovelContext:
    """
    小说预分析主入口。
    1. 通读全文提取世界观+角色+关系网络
    2. 逐章通读生成精细摘要（结合世界观）
    3. 从各章抽取风格样本

    Args:
        chapters: 已切分的章节列表
        full_text: 完整原文
        provider: LLM provider

    Returns:
        NovelContext
    """
    client = LLMClient(provider=provider)
    total = len(chapters)

    # 1. 世界观分析
    if worldview_override is not None:
        print("   ♻️  复用同一全文的世界观缓存，仅重做章节相关分析")
        worldview = normalize_legacy_worldview_schema(dict(worldview_override))
    else:
        print("   📚 通读全文提取世界观...")
        worldview = normalize_legacy_worldview_schema(
            _analyze_worldview(full_text, provider, output_language=output_language)
        )

    # 校验世界观完整性（问题3/4：merge 结果可能缺字段 / persona_axes 不合规）
    worldview_issues = worldview_schema_issues(worldview)
    if worldview_issues:
        from copy import deepcopy

        worldview_before_repair = deepcopy(worldview)
        wv_reason = "；".join(worldview_issues)
        print(f"   ⚠️  世界观校验未通过（{wv_reason}），尝试补录修复...")
        worldview = _repair_worldview(
            worldview, wv_reason, full_text, provider, output_language=output_language
        )
        worldview = normalize_legacy_worldview_schema(worldview)
        remaining_issues = worldview_schema_issues(worldview)
        if remaining_issues:
            error = ValueError("世界观修复后仍不完整：" + "；".join(remaining_issues))
            error.worldview_before_repair = worldview_before_repair
            error.worldview_after_repair = deepcopy(worldview)
            error.worldview_issues = list(remaining_issues)
            raise error
        print("   ✅ 修复后世界观校验通过")

    char_cards = _format_char_cards(worldview)
    relationship_map = _format_relationship_map(worldview)
    world_rules_text = _format_world_rules(worldview)
    factions_text = _format_factions(worldview)
    hidden_threads_text = _format_hidden_threads(worldview)
    character_secrets_text = _format_character_secrets(worldview)
    narrative_arc = worldview.get("narrative_arc", "")
    persona_axes = worldview.get("persona_axes", [])
    ending_dimensions = worldview.get("ending_dimensions", [])
    print(
        f"   ✅ 世界观提取完成：角色 {len(worldview.get('characters', []))}，"
        f"势力 {len(worldview.get('factions', []))}，"
        f"伏笔 {len(worldview.get('hidden_threads', []))}，"
        f"世界观恋爱信号={bool(worldview.get('is_romance_oriented', False))}，"
        f"persona_axes={[a.get('key') for a in persona_axes]}"
    )

    # 2. 逐章摘要
    print(f"   📖 逐章通读生成摘要（共{total}章）...")
    chapter_summaries = []
    for i, ch in enumerate(chapters):
        summary = _summarize_one_chapter(ch, i, worldview, client, output_language=output_language)
        chapter_summaries.append(summary)
        print(f"   ✅ 第{i+1}/{total}章摘要完成")

    # 3. 风格样本（LLM 精选 3-5 段，失败回退到启发式抽取）
    print("   🎨 LLM 精选风格样本...")
    style_samples = _extract_style_samples_llm(chapters, provider, output_language=output_language)

    # 3.5 叙事视角契约：一次推断，供所有正文生成/校验步骤复用。视角是
    # 原文风格的一部分，但不能只依赖 style_notes；章节样本可在缓存缺失
    # 或旧世界观未记录视角时提供确定性补充证据。
    narrative_pov_contract = infer_narrative_perspective_contract(
        worldview,
        chapters=chapters,
        style_samples=style_samples,
    )
    worldview["narrative_pov_contract"] = narrative_pov_contract
    print(
        "   👁️  叙事视角契约："
        f"{narrative_pov_contract.get('perspective', 'unknown')}"
        f"（主角={narrative_pov_contract.get('protagonist_name') or '未确定'}，"
        f"置信度={narrative_pov_contract.get('confidence', 0):.2f}）"
    )

    # 4. 角色行为指纹（防穿帮锚点）
    print("   🔍 提取角色行为指纹（防穿帮锚点）...")
    behavior_anchors = _extract_behavior_anchors(worldview, chapter_summaries)

    # 5. 分支类型自动检测
    print("   🎯 自动检测分支类型...")
    branch_type = detect_branch_type(
        worldview,
        style_samples,
        provider,
        output_language=output_language,
        chapter_summaries=chapter_summaries,
    )
    is_romance_oriented = branch_type == "romance"

    return NovelContext(
        worldview=worldview,
        chapter_summaries=chapter_summaries,
        style_samples=style_samples,
        char_cards=char_cards,
        relationship_map=relationship_map,
        world_rules=world_rules_text,
        narrative_arc=narrative_arc,
        persona_axes=persona_axes if isinstance(persona_axes, list) else [],
        is_romance_oriented=is_romance_oriented,
        factions_text=factions_text,
        hidden_threads_text=hidden_threads_text,
        character_secrets_text=character_secrets_text,
        ending_dimensions=ending_dimensions if isinstance(ending_dimensions, list) else [],
        character_behavior_anchors=behavior_anchors,
        branch_type=branch_type,
        branch_type_strategy_version=_BRANCH_TYPE_STRATEGY_VERSION,
        narrative_pov_contract=narrative_pov_contract,
    )
