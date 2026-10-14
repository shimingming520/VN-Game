"""soft_branch.py - 场景切段 + 差分剧场软分支注入"""
from difflib import SequenceMatcher
import json
import logging
import re
from services.story_planner.core.llm_client import LLMClient, get_lang_instruction
from services.story_planner.core.novel_reader import (
    detect_narrative_pov_drift,
    infer_narrative_perspective_contract,
)
from services.story_planner.core.effects_validator import (
    calculate_effect_caps,
    sanitize_effect_pair,
    sanitize_romance_effect_pair,
)
from services.story_planner.core.chapter_branch_runtime import (
    content_sha256,
    get_soft_branch_gap,
    initialize_soft_branch_gap_checkpoint,
    record_soft_branch_gap,
    repair_unbalanced_prose_delimiters,
)
from services.story_planner.core.runtime_observability import structured_log


logger = logging.getLogger(__name__)


def _narrative_pov_prompt_block(contract: dict | None) -> str:
    """Render the shared narrative POV contract for prose generation prompts.

    Choice labels are UI copy and may remain first-person; this block applies
    only to generated prose fields (content/bridge).  An empty or unknown
    contract is intentionally non-binding to preserve legacy callers.
    """
    if not isinstance(contract, dict):
        return ""
    perspective = str(
        contract.get("perspective")
        or contract.get("mode")
        or contract.get("pov")
        or "unknown"
    ).strip()
    protagonist = str(
        contract.get("protagonist_name")
        or contract.get("pov_character")
        or "主角"
    ).strip()
    rule_text = str(contract.get("rule_text") or "").strip()
    if not rule_text:
        if perspective == "first_person":
            rule_text = (
                f"保持主角{protagonist}的第一人称叙事；正文中的‘我’只能指向{protagonist}，"
                "不得切换为其他角色的第一人称。"
            )
        elif perspective == "third_person_limited":
            rule_text = (
                f"保持围绕{protagonist}的第三人称限知叙事；正文叙述使用姓名、他/她等第三人称，"
                "不得把叙述者改成‘我’。"
            )
        elif perspective == "third_person_omniscient":
            rule_text = (
                f"保持围绕{protagonist}的第三人称全知叙事；不得无铺垫切换成第一人称旁白。"
            )
        elif perspective == "mixed":
            rule_text = "原著允许有限多视角；每个场景必须明确跟随一个视角，不得无标记切换人称。"
        else:
            rule_text = "原著视角未确定；优先模仿相邻原文，不得在同一场景无理由切换人称。"
    allow_switch = bool(contract.get("allow_switch"))
    switch_rule = (
        "如原著允许章节级切换，只能在明确的章节/场景边界切换。"
        if allow_switch
        else "当前契约禁止章节或场景内切换叙事视角。"
    )
    return (
        "【叙事视角契约（仅约束正文，不约束 choice_text UI 文案）】\n"
        f"perspective={perspective}; protagonist={protagonist}\n"
        f"{rule_text}\n{switch_rule}\n"
        "对白、书信、日记中的人称按原文保留；不要把按钮文案的‘我’写进第三人称正文。\n\n"
    )


def _context_narrative_pov_contract(novel_context, explicit: dict | None = None) -> dict:
    """Get a POV contract from an explicit argument or NovelContext."""
    if isinstance(explicit, dict):
        return explicit
    contract = getattr(novel_context, "narrative_pov_contract", None)
    if isinstance(contract, dict) and contract:
        return contract
    worldview = getattr(novel_context, "worldview", {}) if novel_context else {}
    contract = worldview.get("narrative_pov_contract") if isinstance(worldview, dict) else None
    return contract if isinstance(contract, dict) else {}

SOFT_BRANCH_OPEN = (
    "\n\n════════════════════════════════════════\n"
    "◆◆◆  软分支选择  ◆◆◆\n"
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
_SOFT_BRANCH_BLOCK_RE = re.compile(
    re.escape(SOFT_BRANCH_OPEN.strip())
    + r".*?"
    + re.escape(SOFT_BRANCH_CLOSE.strip()),
    flags=re.DOTALL,
)
_NEGATIVE_ROMANCE_CHOICE_MARKERS = (
    "不想", "不愿", "不该", "拒绝", "远离", "避开", "放下", "离开", "离去", "松手",
    "不回应", "不追", "不见", "告别", "算了", "保持距离", "退回朋友", "不能贪", "不能只", "不能靠近", "别靠近",
)

_CHOICE_BEHAVIOR_TEMPLATES = {
    ("courage", "positive"): (
        "我决定主动开口", "我向前一步说清楚", "我抬头直视他们", "我伸手拦下这件事",
    ),
    ("courage", "negative"): (
        "我先沉默不作回答", "我后退一步让开", "我移开视线不再争辩", "我把决定交给别人",
    ),
    ("self_worth", "positive"): (
        "我为自己认真辩解", "我拒绝替流言道歉", "我坚持保留自己的署名", "我不再让出应有位置",
    ),
    ("self_worth", "negative"): (
        "我删掉自己的署名", "我沉默接受那些污名", "我后退让出这个位置", "我把解释交给别人",
    ),
}
_GENERIC_CHOICE_TEMPLATES = {
    "positive": (
        "我开口说明决定", "我上前落实选择", "我伸手承担后果", "我直视眼前的人",
        "我把证据放到桌上", "我请同伴一起确认", "我当场写下自己的决定", "我主动提出下一步",
    ),
    "negative": (
        "我先沉默下来", "我后退一步观察", "我移开视线等待", "我把决定交给别人",
        "我暂时收起手里的证据", "我请大家先离开现场", "我把问题留到明天", "我先去核对另一条线索",
    ),
}
_ROMANCE_CHOICE_TEMPLATES = {
    "positive": (
        "我主动问清她的心意", "我把位置留在她身边", "我先叫住她的名字", "我迎上她等候的目光",
        "我把手伸向她", "我承认自己想靠近", "我留下陪她走完这段路", "我说出一直没说的话",
    ),
    "negative": (
        "我先松开握住她的手", "我把没说完的话咽下", "我避开她追问的目光", "我退回朋友该有的位置",
        "我暂时不回应她的心意", "我把这份靠近留到以后", "我先让彼此冷静下来", "我没有追上她的脚步",
    ),
}
_ROMANCE_INCOMPATIBLE_CHOICE_MARKERS = (
    "证据", "现场", "线索", "核对", "名单", "病历", "口供", "物证", "调查", "查验",
)
_FLAVOR_BRANCH_CUT_MARKERS = (
    "决定", "发现", "看见", "听见", "走进", "离开", "拿起", "放下", "说", "问",
    "笑", "哭", "沉默", "准备", "脚步", "门", "雨", "灯", "信", "茶", "吃", "回头", "等待",
)
_MODULAR_CHOICE_TEMPLATES = (
    "复问证人证词", "回到现场复验", "查阅旧案记录", "跟踪可疑人物",
    "核对物证来源", "走访附近住户", "检查出入口痕迹", "比对两份口供",
    "询问当值守卫", "追查线索去向", "复原案发路线", "验证机关痕迹",
)
_INVESTIGATION_ACTION_MARKERS = (
    "查", "问", "访", "验", "核对", "比对", "跟踪", "追", "复原", "检查", "勘察", "搜",
)
_AVOIDANCE_MARKERS = ("藏", "躲", "缩回", "影子", "人群", "装作没看见")
_PASSIVE_CHOICE_MARKERS = (
    "的声音", "的目光", "的脚步", "刺回来", "走进来", "走过来",
    "看着我", "望着我", "落在我身上", "传来", "响起", "出现",
)
_PASSIVE_FIRST_PERSON_PATTERNS = (
    r"^我被",
    r"^我让",
    r"^我听见",
    r"^我看见",
    r"^我感到",
    r"^我意识到",
)
_MODULAR_COMPLETION_MARKERS = (
    "拆开", "打开", "撬开", "取出", "拿出", "发现", "找到", "确认", "揭开",
    "揭露", "查明", "锁定", "拿到", "获得", "交给", "召集", "前往", "抵达",
    "离开", "带走", "拿走", "收好", "揣进", "藏起", "拆下",
)
_MODULAR_OBJECT_MOVE_MARKERS = (
    "带走", "拿走", "收好", "揣进", "藏起", "交给", "拆下", "取下", "移走",
)
_MODULAR_OBJECT_STAY_MARKERS = (
    "仍在", "还在", "卡着", "卡在", "留在", "放在", "摆在", "挂在", "夹在",
)
_CLUE_SOURCE_TYPES = {"source_fact", "safe_observation", "decisive_clue"}
_MODULAR_FLAVOR_CHOICE_TEMPLATES = (
    "记下眼前细节", "留意刚才声响", "回想片刻变化", "观察周围动静",
    "记住广播时间", "留心灯光变化", "整理已有信息", "暂不深究异样",
)

_SOFT_BRANCH_METRIC_DEFAULTS = {
    "total": 0,
    "first_pass_success": 0,
    "normalized_success": 0,
    "targeted_repair_success": 0,
    "full_retry_success": 0,
    "simple_llm_fallback": 0,
    "hardcoded_fallback": 0,
    "skipped_no_route_target": 0,
    "rescheduled_effect_key": 0,
    "validation_failures": 0,
    "generation_exceptions": 0,
    "protagonist_presence_skipped": 0,
    "delayed_presence_recovered": 0,
    "offscreen_flavor_generated": 0,
    "semantic_presence_calls": 0,
    "semantic_presence_fallbacks": 0,
    "presence_active": 0,
    "presence_mentioned": 0,
    "presence_memory": 0,
    "presence_offscreen": 0,
    "presence_unknown": 0,
    "transition_conflicts": 0,
    "transition_repairs": 0,
    "transition_repair_success": 0,
    "transition_dropped": 0,
    "validation_dropped": 0,
    "deleted_branch_count": 0,
    "premature_action_leaks": 0,
    "decisive_clue_leaks": 0,
    "button_template_rewrites": 0,
    "theoretical_gap_count": 0,
    "eligible_gap_count": 0,
    "flavor_branch_count": 0,
    "generated_branch_count": 0,
    "validation_attempted": 0,
    "validation_warning_count": 0,
}


def _initialize_soft_branch_metrics(metrics: dict | None) -> dict:
    target = metrics if isinstance(metrics, dict) else {}
    for key, value in _SOFT_BRANCH_METRIC_DEFAULTS.items():
        target.setdefault(key, value)
    target.setdefault("validation_failures_by_category", {})
    return target


def new_soft_branch_metrics() -> dict:
    """Create one serializable metrics bucket for a node or a complete run."""
    return _initialize_soft_branch_metrics({})


def merge_soft_branch_metrics(target: dict, source: dict | None) -> dict:
    """Accumulate one node's metrics into a run-level metrics bucket."""
    merged = _initialize_soft_branch_metrics(target)
    incoming = _initialize_soft_branch_metrics(dict(source or {}))
    for key in _SOFT_BRANCH_METRIC_DEFAULTS:
        merged[key] = int(merged.get(key, 0) or 0) + int(incoming.get(key, 0) or 0)
    merged_categories = merged.setdefault("validation_failures_by_category", {})
    for category, count in incoming.get("validation_failures_by_category", {}).items():
        merged_categories[str(category)] = int(merged_categories.get(str(category), 0) or 0) + int(count or 0)
    return merged


def _increment_soft_branch_metric(metrics: dict, key: str, amount: int = 1) -> None:
    metrics[key] = int(metrics.get(key, 0) or 0) + int(amount)


# Optional branches should be resilient to model formatting and stylistic
# imperfections.  Only a branch that cannot provide a runnable pair of routes
# is fatal; semantic and presentation findings are retained as diagnostics.
_SOFT_BRANCH_FATAL_MARKERS = (
    "返回结果不是 JSON",
    "不是 JSON 对象",
    "候选为空",
    "两个正文都为空",
    "缺少可运行正文",
)


def classify_soft_branch_validation_issue(
    reason: str,
    *,
    candidate: dict | None = None,
    category: str | None = None,
) -> str:
    """Classify a soft-branch finding as ``fatal`` or ``warning``.

    The validator remains strict while a candidate is being repaired, but the
    retry exhaustion policy is intentionally tolerant.  A usable candidate is
    kept for optional gameplay unless its structure is absent altogether.
    ``category`` is accepted for callers that already performed categorisation.
    """
    if not isinstance(candidate, dict):
        return "fatal"
    text = str(reason or "")
    if any(marker in text for marker in _SOFT_BRANCH_FATAL_MARKERS):
        return "fatal"
    # Both routes are required for a playable A/B branch.  A missing route is
    # structural, not a stylistic defect, so it stays in the fatal minority.
    content_a = str(candidate.get("content_a") or "").strip()
    content_b = str(candidate.get("content_b") or "").strip()
    if not content_a or not content_b:
        return "fatal"
    # ``invalid_candidate`` is fatal only when it describes missing route
    # content.  All other validation categories (effects, POV, delimiters,
    # choice wording, clue metadata and transition hints) are warnings after
    # the bounded repair budget is exhausted.
    if str(category or "") == "invalid_candidate" and not (content_a and content_b):
        return "fatal"
    return "warning"


def retain_soft_branch_candidate(
    candidate: dict,
    warnings: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Return a runnable candidate annotated for downstream review.

    This helper deliberately does not rewrite prose.  Safe structural defaults
    are supplied only for missing UI fields so that a warning candidate remains
    parseable by the player and by resume/checkpoint code.
    """
    retained = dict(candidate or {})
    retained.setdefault("choice_a", "继续当前行动")
    retained.setdefault("choice_b", "暂缓当前行动")
    retained.setdefault("content_a", "")
    retained.setdefault("content_b", "")
    retained.setdefault("bridge", "")
    retained.setdefault("effects_a", {})
    retained.setdefault("effects_b", {})
    unique_warnings: list[str] = []
    for item in warnings or ():
        text = str(item or "").strip()
        if text and text not in unique_warnings:
            unique_warnings.append(text)
    retained["validation_status"] = "warning"
    retained["validation_warnings"] = unique_warnings
    return retained


def classify_modular_transition_issue(category: str | None) -> str:
    """Classify modular transition findings for optional-branch retention."""
    # A malformed/empty candidate is handled separately by the caller.  These
    # findings make the downstream scene factually impossible, so they remain
    # in the small fatal set; wording, object placement and minor clue issues
    # are retained as warnings after repair exhaustion.
    return (
        "fatal"
        if str(category or "") in {
            "invalid_candidate",
            "downstream_action_completed",
            "decisive_clue_leaked",
            "character_state_conflict",
        }
        else "warning"
    )


def _record_validation_failure(
    metrics: dict,
    *,
    chapter_idx: int,
    gap_idx: int,
    attempt: int,
    branch_type: str,
    category: str,
    reason: str,
    repair_fields: list[str],
) -> None:
    _increment_soft_branch_metric(metrics, "validation_failures")
    by_category = metrics.setdefault("validation_failures_by_category", {})
    by_category[category] = int(by_category.get(category, 0) or 0) + 1
    structured_log(
        logger,
        "soft_branch_validation_failed",
        level="warning",
        chapter=chapter_idx + 1,
        gap=gap_idx + 1,
        attempt=attempt,
        branch_type=branch_type,
        category=category,
        reason=reason,
        repair_fields=repair_fields,
    )
    print(
        f"   ↪️  第{chapter_idx+1}章第{gap_idx+1}个软分支校验未通过"
        f"（attempt={attempt}, category={category}）：{reason}"
    )


_ROMANCE_NAME_SPLIT_RE = re.compile(
    r"[()（）\[\]［］【】{}<>《》/／、，,；;|]+"
)
_ROMANCE_NAME_TRIM_CHARS = " \t\r\n\u3000·・:：;；,，、/／()（）[]［］【】{}<>《》"
_ROMANCE_GENERIC_LABELS = {
    "男主",
    "女主",
    "男一",
    "女一",
    "男1",
    "女1",
    "主角",
    "主人公",
    "攻略对象",
    "恋爱对象",
}


def _is_declared_protagonist(character: object) -> bool:
    """Return whether a character card explicitly identifies the protagonist."""

    if not isinstance(character, dict):
        return False
    if bool(character.get("is_protagonist")):
        return True
    role = str(character.get("role") or "").strip().lower()
    role_head = re.split(r"[（(\[【{/:：,，、;；\s]", role, maxsplit=1)[0].strip()
    if role in {
        "主人公",
        "protagonist",
        "main character",
        "main_protagonist",
        "hero",
        "main hero",
        "main heroine",
        "男主",
        "女主",
        "男一",
        "女一",
    }:
        return True
    if role_head in {"男主", "女主", "男主角", "女主角"} or role_head in {
        "第一主角",
        "第二主角",
    }:
        return True
    # Support common labels such as 男主角/女主角/第一主角 while avoiding
    # incidental phrases like 主角好友.
    return role_head == "主角" or role_head.endswith("主角")


class _RomanceRouteLookup(dict):
    """Name-to-affection mapping with canonical-name metadata.

    The dict interface is kept for compatibility with existing callers.  The
    metadata lets the matcher retain old suffix fallbacks for legacy maps
    without deriving false short aliases from every explicit character alias.
    """

    def __init__(self, *args, canonical_names: set[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.canonical_names = set(canonical_names or ())


def _romance_name_tokens(value: object) -> list[str]:
    """Split a route name/alias field into explicit character-name tokens.

    The analyser occasionally emits compact route labels such as
    ``许定（明镜非台/青山不许）``.  Treating that whole label as one name makes
    scene presence checks impossible.  This helper deliberately only splits
    explicit separators; it does not guess arbitrary substrings of a name.
    """

    tokens: list[str] = []
    def _flatten(raw_value: object):
        if isinstance(raw_value, (list, tuple, set)):
            for nested in raw_value:
                yield from _flatten(nested)
        elif isinstance(raw_value, dict):
            for field in (
                "name",
                "character_name",
                "canonical_name",
                "alias",
                "aliases",
                "character_aliases",
            ):
                if field in raw_value:
                    yield from _flatten(raw_value.get(field))
        else:
            yield raw_value

    for raw in _flatten(value):
        text = str(raw or "").strip()
        if not text:
            continue
        for part in _ROMANCE_NAME_SPLIT_RE.split(text):
            token = str(part or "").strip(_ROMANCE_NAME_TRIM_CHARS)
            if (
                len(token) >= 2
                and token not in _ROMANCE_GENERIC_LABELS
                and token not in tokens
            ):
                tokens.append(token)
    return tokens


def _romance_aliases(name: str) -> list[str]:
    """Return explicit aliases for a route label, longest and deterministic first."""

    return sorted(_romance_name_tokens(name), key=lambda item: (-len(item), item))


def _romance_legacy_aliases(name: str) -> list[str]:
    """Return conservative historical suffix aliases for old route data.

    Explicit aliases are preferred.  These suffixes are used only as a
    fallback when no explicit alias matched, preserving compatibility with
    older analyses that stored only a full three/four-character name.
    """

    tokens = _romance_name_tokens(name)
    values: set[str] = set()
    for token in tokens:
        if len(token) >= 4:
            values.add(token[2:])
        elif len(token) >= 3:
            values.add(token[1:])
    return sorted((value for value in values if len(value) >= 2), key=lambda item: (-len(item), item))


def _romance_route_hits(text: str, romance_name_to_key: dict) -> list[tuple[str, str]]:
    """Find route keys and the longest matched explicit alias in *text*.

    Multiple aliases belonging to the same route count once.  If an alias is
    shared by two routes, both keys are returned so callers can treat the
    title as ambiguous instead of randomly selecting a route.
    """

    source = str(text or "")
    if not source or not isinstance(romance_name_to_key, dict):
        return []

    best_by_key: dict[str, tuple[int, int, str]] = {}
    explicit_matches = False
    for name, key in romance_name_to_key.items():
        route_key = str(key or "").strip()
        if not route_key:
            continue
        for alias in _romance_aliases(str(name)):
            if alias and alias in source:
                explicit_matches = True
                candidate = (len(alias), source.find(alias), alias)
                previous = best_by_key.get(route_key)
                if previous is None or (candidate[0], -candidate[1]) > (
                    previous[0], -previous[1]
                ):
                    best_by_key[route_key] = candidate

    # Compatibility fallback for legacy maps with no explicit aliases in the
    # text.  Never mix guessed suffixes with explicit matches.
    if not explicit_matches:
        canonical_names = getattr(romance_name_to_key, "canonical_names", None)
        for name, key in romance_name_to_key.items():
            if canonical_names is not None and name not in canonical_names:
                continue
            route_key = str(key or "").strip()
            if not route_key:
                continue
            for alias in _romance_legacy_aliases(str(name)):
                if alias and alias in source:
                    candidate = (len(alias), source.find(alias), alias)
                    previous = best_by_key.get(route_key)
                    if previous is None or (candidate[0], -candidate[1]) > (
                        previous[0], -previous[1]
                    ):
                        best_by_key[route_key] = candidate

    return [
        (key, alias)
        for key, (_length, _position, alias) in sorted(
            best_by_key.items(),
            key=lambda item: (item[1][1], -item[1][0], item[0]),
        )
    ]


def _build_romance_name_to_key(
    romance_targets: list | None,
    worldview: dict | None,
    variable_defs: list | None,
    aff_keys: list | None,
) -> dict[str, str]:
    """Build one canonical route lookup including every safe character alias.

    ``romance_targets`` is LLM-produced and may contain a compact label such
    as ``许定（明镜非台/青山不许）``.  Character cards and the normalized
    ``character_alias_map`` are treated as authoritative aliases for the same
    route.  The returned map intentionally contains only unambiguous aliases;
    an alias claimed by two routes is removed rather than assigned randomly.
    """

    source_worldview = worldview if isinstance(worldview, dict) else {}
    if not isinstance(aff_keys, (list, tuple, set)):
        return {}
    keys = [
        str(value).strip()
        for value in (aff_keys or [])
        if str(value or "").strip()
    ]
    if not keys:
        return {}

    targets = [item for item in (romance_targets or []) if isinstance(item, dict)]
    characters = [
        item
        for item in (source_worldview.get("characters") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    raw_alias_map = source_worldview.get("character_alias_map")
    alias_map = {
        str(alias).strip(): str(canonical).strip()
        for alias, canonical in (raw_alias_map.items() if isinstance(raw_alias_map, dict) else [])
        if str(alias or "").strip() and str(canonical or "").strip()
    }
    def _conflict_aliases(raw_conflicts: object) -> set[str]:
        """Read normalized and legacy alias-conflict encodings safely."""

        aliases: set[str] = set()

        def _add(value: object) -> None:
            for token in _romance_name_tokens(value):
                aliases.add(token)

        if isinstance(raw_conflicts, dict):
            _add(raw_conflicts.get("alias"))
            _add(raw_conflicts.get("aliases"))
            # Legacy form: {"共享称呼": ["角色A", "角色B"]} or
            # {"alias": {"characters": [...]}}.  The mapping key is the
            # only unambiguous alias candidate; values may be owner names.
            for key, value in raw_conflicts.items():
                key_text = str(key or "").strip()
                if key_text and key_text not in {
                    "character",
                    "characters",
                    "alias",
                    "aliases",
                    "reason",
                }:
                    _add(key_text)
                if isinstance(value, dict):
                    _add(value.get("alias"))
                    _add(value.get("aliases"))
        elif isinstance(raw_conflicts, (list, tuple, set)):
            for item in raw_conflicts:
                if isinstance(item, dict):
                    _add(item.get("alias"))
                    _add(item.get("aliases"))
                elif isinstance(item, str):
                    _add(item)
        return aliases

    ambiguous_aliases = _conflict_aliases(
        source_worldview.get("character_alias_conflicts")
    )

    # Detect duplicate aliases even when an older worldview did not persist
    # the normalized ``character_alias_conflicts`` field.
    alias_owners: dict[str, set[str]] = {}
    canonical_names: set[str] = set()
    for character in characters:
        canonical = str(character.get("name") or "").strip()
        if canonical:
            canonical_names.add(canonical)
        for alias in _romance_name_tokens(character.get("aliases")):
            alias_owners.setdefault(alias, set()).add(canonical)
    ambiguous_aliases.update(
        alias for alias, owners in alias_owners.items() if len(owners) > 1
    )
    # A legacy cache can contain one card whose canonical name is another
    # card's alias.  That token is not safe for route matching unless the
    # cards were normalized into one identity first; disable it to avoid
    # assigning a scene to the wrong affection axis.
    ambiguous_aliases.update(
        alias
        for alias, owners in alias_owners.items()
        if alias in canonical_names and any(owner != alias for owner in owners)
    )
    ambiguous_aliases.update(
        alias
        for alias, canonical in alias_map.items()
        if alias in canonical_names and canonical != alias
    )

    def _safe_name_tokens(value: object) -> list[str]:
        return [
            token
            for token in _romance_name_tokens(value)
            if token not in ambiguous_aliases
        ]

    def _unique(values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            clean = str(value or "").strip()
            if len(clean) >= 2 and clean not in result:
                result.append(clean)
        return result

    specs: list[dict[str, object]] = []
    for target in targets:
        raw_name = target.get("name") or target.get("character_name") or target.get("canonical_name")
        explicit = _safe_name_tokens(
            [
                raw_name,
                target.get("aliases"),
                target.get("alias"),
                target.get("character_aliases"),
            ]
        )
        if not explicit:
            continue
        canonical_candidates = {
            alias_map.get(token, token)
            for token in explicit
        }
        aliases = list(explicit)

        # Match a normalized character card when either its canonical name or
        # one of its aliases occurs in the route label.
        matched_cards: list[dict] = []
        for character in characters:
            canonical = str(character.get("name") or "").strip()
            card_aliases = _romance_name_tokens(character.get("aliases"))
            card_names = {canonical, *card_aliases}
            safe_card_names = card_names - ambiguous_aliases
            safe_explicit = set(explicit) - ambiguous_aliases
            normalized_card_names = {
                alias_map.get(item, item) for item in safe_card_names if item
            }
            if (
                canonical in canonical_candidates
                or bool(safe_card_names.intersection(safe_explicit))
                or bool(normalized_card_names.intersection(canonical_candidates))
            ):
                matched_cards.append(character)
                aliases.extend([canonical, *(alias for alias in card_aliases if alias not in ambiguous_aliases)])
                canonical_candidates.add(canonical)

        # Expand aliases that point to a matched canonical character.  This
        # covers older cached worldviews where the character card omitted one
        # alias but ``character_alias_map`` retained it.
        for alias, canonical in alias_map.items():
            if alias in ambiguous_aliases:
                continue
            if canonical in canonical_candidates or alias in explicit:
                aliases.append(alias)

        primary = next(
            (
                str(character.get("name") or "").strip()
                for character in matched_cards
                if str(character.get("name") or "").strip()
            ),
            explicit[0],
        )
        specs.append(
            {
                "primary": primary,
                "aliases": _unique(aliases),
                "variable_key": str(
                    target.get("variable_key")
                    or target.get("variable")
                    or target.get("key")
                    or ""
                ).strip(),
            }
        )

    # Preserve the historical fallback for romance analyses that omitted the
    # explicit target list: use named character cards, but do not include the
    # declared protagonist as a route unless it is explicitly targeted.
    if not specs:
        for character in characters:
            name = str(character.get("name") or "").strip()
            if not name or _is_declared_protagonist(character):
                continue
            aliases = _safe_name_tokens([name, character.get("aliases")])
            specs.append({"primary": name, "aliases": aliases, "variable_key": ""})

    primary_names = [str(spec["primary"]) for spec in specs if spec.get("primary")]
    aliases_by_primary = {
        str(spec.get("primary") or ""): [
            str(alias)
            for alias in (spec.get("aliases") or [])
            if str(alias or "").strip()
        ]
        for spec in specs
        if str(spec.get("primary") or "").strip()
    }
    mapped_primary = _match_names_to_affection_keys(
        primary_names,
        keys,
        variable_defs,
        aliases_by_name=aliases_by_primary,
    )

    # A target may carry an explicit variable key; trust it only when it is a
    # declared affection axis.  Otherwise use deterministic name matching.
    route_map: _RomanceRouteLookup = _RomanceRouteLookup(
        canonical_names={str(spec.get("primary") or "") for spec in specs}
    )
    conflicts: dict[str, set[str]] = {}
    for spec in specs:
        explicit_key = str(spec.get("variable_key") or "")
        primary = str(spec.get("primary") or "")
        key = explicit_key if explicit_key in keys else mapped_primary.get(primary, "")
        if not key:
            continue
        for alias in spec.get("aliases", []) or []:
            clean = str(alias or "").strip()
            if len(clean) < 2:
                continue
            previous = route_map.get(clean)
            if previous and previous != key:
                conflicts.setdefault(clean, {previous}).add(key)
            else:
                route_map[clean] = key

    for alias in conflicts:
        route_map.pop(alias, None)
        logger.warning(
            "恋爱路线别名存在歧义，已禁用该别名: alias=%s routes=%s",
            alias,
            sorted(conflicts[alias]),
        )
    return route_map


def _romance_key_for_title(title: str, romance_name_to_key: dict) -> str | None:
    text = str(title or "")
    if any(marker in text for marker in _NEGATIVE_ROMANCE_CHOICE_MARKERS):
        return None
    hits = _romance_route_hits(text, romance_name_to_key)
    keys = {key for key, _alias in hits}
    return hits[0][0] if len(keys) == 1 and hits else None


def _romance_keys_present_in_text(text: str, romance_name_to_key: dict) -> list[str]:
    return [key for key, _alias in _romance_route_hits(text, romance_name_to_key)]


def _scene_has_flavor_cut(upper_text: str, lower_text: str = "") -> bool:
    """Return whether a romance-free gap still has a concrete, player-visible cut.

    Romance mode may contain ordinary character/action beats between route
    scenes.  Those beats are eligible for a non-numeric flavor branch; empty
    or purely structural gaps remain omitted to avoid noisy buttons.
    """
    text = "\n".join((str(upper_text or ""), str(lower_text or ""))).strip()
    if len(text) < 20:
        return False
    sentence_count = len(re.findall(r"[。！？!?\n]", text))
    return sentence_count >= 2 and any(marker in text for marker in _FLAVOR_BRANCH_CUT_MARKERS)


def _normalize_romance_mirror_effects(
    payload: dict,
    romance_name_to_key: dict,
    caps: dict[str, int],
) -> tuple[dict, bool]:
    """Normalize a clear A-target/B-target romance split into a legal mirrored pair."""
    normalized = dict(payload or {})
    key_a = _romance_key_for_title(normalized.get("choice_a", ""), romance_name_to_key)
    key_b = _romance_key_for_title(normalized.get("choice_b", ""), romance_name_to_key)
    if not key_a or not key_b or key_a == key_b:
        return normalized, False
    strength_a = max(3, int(caps.get(key_a, 3)))
    strength_b = max(3, int(caps.get(key_b, 3)))
    sign_a = -1 if any(marker in str(normalized.get("choice_a") or "") for marker in _NEGATIVE_ROMANCE_CHOICE_MARKERS) else 1
    sign_b = -1 if any(marker in str(normalized.get("choice_b") or "") for marker in _NEGATIVE_ROMANCE_CHOICE_MARKERS) else 1
    mirrored_a = {key_a: sign_a * strength_a, key_b: -sign_b * strength_b}
    mirrored_b = {key_a: -sign_a * strength_a, key_b: sign_b * strength_b}
    changed = (
        normalized.get("effects_a") != mirrored_a
        or normalized.get("effects_b") != mirrored_b
    )
    normalized["effects_a"] = mirrored_a
    normalized["effects_b"] = mirrored_b
    return normalized, changed


def _validation_category(reason: str) -> str:
    text = str(reason or "")
    if any(field in text for field in ("content_a", "content_b", "bridge")) and any(
        marker in text for marker in ("引号", "括号", "书名号", "闭合", "符号")
    ):
        return "prose_delimiter"
    if "第一人称身份漂移" in text:
        return "romance_pov_drift"
    if "倾向" in text and "effects" in text:
        return "romance_effect_direction"
    if "指定变量" in text or "目标变量" in text:
        return "required_effect_missing"
    if "effects" in text or "变量" in text:
        return "effects_contract"
    return "content_contract"


def _repair_fields_for_issue(reason: str) -> list[str]:
    text = str(reason or "")
    fields = [
        field
        for field in ("content_a", "content_b", "bridge")
        if field in text
    ]
    if fields:
        return fields
    if "第一人称身份漂移" in text:
        return ["choice_a", "content_a", "choice_b", "content_b", "bridge"]
    if "effects" in text or "变量" in text or "倾向" in text:
        # A named romance mismatch is often caused by the button wording;
        # repair the semantic pair together instead of flipping numbers alone.
        return ["choice_a", "choice_b", "effects_a", "effects_b"]
    return [
        "choice_a",
        "content_a",
        "effects_a",
        "choice_b",
        "content_b",
        "bridge",
        "effects_b",
    ]


def _build_targeted_repair_prompt(
    candidate: dict,
    *,
    reason: str,
    fields: list[str],
    variable_hint: str,
    narrative_pov_block: str = "",
    scene_context: str = "",
) -> str:
    selected = {field: candidate.get(field) for field in fields}
    return (
        f"{narrative_pov_block}"
        f"{variable_hint}"
        f"## 场景上下文（仅用于保持语义一致）\n{scene_context}\n"
        "下面是一组软分支的局部字段校验失败。保留未列出的字段不变，只修复指定字段。\n"
        f"失败原因：{reason}\n"
        f"只返回以下字段：{fields}\n"
        f"待修复字段：{json.dumps(selected, ensure_ascii=False)}\n"
        "输出严格 JSON 对象，不得返回解释，也不得添加未要求的字段。"
    )


def strip_soft_branch_blocks(content: str) -> str:
    """Remove generated A/B branch theatres while preserving the mainline prose."""
    return _SOFT_BRANCH_BLOCK_RE.sub("\n\n", str(content or "")).strip()


def normalize_soft_branch_prose_fields(payload: dict) -> tuple[dict, bool, str]:
    """Repair only safe tail quote omissions and reject ambiguous prose fields."""
    normalized = dict(payload or {})
    for field in ("content_a", "content_b", "bridge"):
        value = str(normalized.get(field) or "")
        if not value.strip():
            continue
        repaired, repaired_ok, reason = repair_unbalanced_prose_delimiters(value)
        if not repaired_ok:
            return normalized, False, f"{field}: {reason}"
        normalized[field] = repaired
    return normalized, True, ""


def remember_choice_text(history: list[str], text: str, limit: int = 2000) -> None:
    """记录本次构建的选项文案。

    精确重复需要覆盖整次运行；语义近似判断仍只读取最近 30 条，避免
    历史很长时把正常的同类动作全部判成重复。
    """
    normalized = str(text or "").strip()
    if not normalized:
        return
    history.append(normalized)
    del history[:-max(int(limit), 1)]


def remember_choices_from_markdown(history: list[str], content: str) -> None:
    """Restore player choice history from a completed node during resume."""
    for match in re.finditer(r"▶\s*选项[AB]：([^\n]+)", str(content or "")):
        remember_choice_text(history, match.group(1).strip())


def _choice_text_is_similar(candidate: str, previous: str) -> bool:
    left = re.sub(r"[\s，。！？、,.!?]", "", str(candidate or ""))
    right = re.sub(r"[\s，。！？、,.!?]", "", str(previous or ""))
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    if (
        any(marker in left for marker in _AVOIDANCE_MARKERS)
        and any(marker in right for marker in _AVOIDANCE_MARKERS)
    ):
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.68


def ensure_distinct_choice_text(
    choice_text: str,
    recent: list[str] | None,
    *,
    variable_key: str = "",
    polarity: str = "positive",
    branch_type: str = "",
) -> str:
    """在最近 30 条内发现重复/近义表达时，改写成可观察的具体行为。"""
    candidate = str(choice_text or "").strip()
    all_history = [str(item) for item in (recent or []) if str(item).strip()]
    recent_window = all_history[-30:]
    is_romance = str(branch_type or "").lower() == "romance"
    romance_incompatible = is_romance and any(
        marker in candidate for marker in _ROMANCE_INCOMPATIBLE_CHOICE_MARKERS
    )
    exact_duplicate = any(candidate == old for old in all_history)
    passive_description = (
        candidate
        and not candidate.startswith(("我", "选择", "先", "主动", "暂时", "不"))
        and any(marker in candidate for marker in _PASSIVE_CHOICE_MARKERS)
    )
    passive_first_person = any(
        re.search(pattern, candidate) for pattern in _PASSIVE_FIRST_PERSON_PATTERNS
    )
    prefix_family = candidate[:2] if candidate else ""
    prefix_crowded = bool(prefix_family) and sum(
        item.startswith(prefix_family) for item in recent_window[-20:]
    ) >= 2
    if (
        candidate
        and not romance_incompatible
        and not exact_duplicate
        and not passive_description
        and not passive_first_person
        and not prefix_crowded
        and not any(_choice_text_is_similar(candidate, old) for old in recent_window)
    ):
        return candidate

    direction = "negative" if str(polarity).lower() == "negative" else "positive"
    if is_romance:
        templates = _ROMANCE_CHOICE_TEMPLATES[direction]
    else:
        templates = _CHOICE_BEHAVIOR_TEMPLATES.get(
            (str(variable_key or "").lower(), direction),
            _GENERIC_CHOICE_TEMPLATES[direction],
        )
    for replacement in templates:
        if not any(_choice_text_is_similar(replacement, old) for old in recent_window):
            return replacement
    # 模板耗尽时组合具体动作，避免在玩家可见文案后追加机械数字。
    if is_romance:
        verbs = ("叫住", "回应", "握住", "松开", "记住", "说出")
        objects = ("她的名字", "她的目光", "她的手", "这份心意", "那句约定", "没说完的话")
    else:
        verbs = ("核对", "交给", "写下", "收起", "询问", "带走")
        objects = ("证据", "名单", "地址", "病历", "回信", "录音")
    for offset in range(len(verbs) * len(objects)):
        verb = verbs[(len(recent_window) + offset) % len(verbs)]
        obj = objects[(len(recent_window) * 2 + offset) % len(objects)]
        replacement = f"我先{verb}{obj}"
        if not any(_choice_text_is_similar(replacement, old) for old in recent_window):
            return replacement
    return "我换一种方式处理眼前的事"


def _romance_pov_drift_reason(payload: dict, protagonist_names: list[str]) -> str:
    """Detect clear cases where romance prose makes a supporting character the first-person narrator."""
    if not isinstance(payload, dict) or not protagonist_names:
        return ""
    text = "\n".join(
        str(payload.get(field) or "")
        for field in ("content_a", "content_b", "bridge")
    )
    for name in sorted(set(protagonist_names), key=len, reverse=True):
        if not name:
            continue
        escaped = re.escape(name)
        patterns = (
            rf"我[^。！？!?\n]{{0,10}}(?:和|跟|同|陪|拉住|拉回|握住|握紧|牵住|看着|望着|叫住|抱住|亲吻|护着|靠近){escaped}",
            rf"{escaped}(?:的手|的肩|的目光|被我|让我|看着我|望着我|对我|和我|跟我)",
        )
        if any(re.search(pattern, text) for pattern in patterns):
            return f"第一人称身份漂移：正文中的‘我’把主角{name}当成了第三人称对象"
    return ""


def _romance_effect_direction_issue(payload: dict, romance_name_to_key: dict) -> str:
    """Reject clear named-route/effect mismatches before deterministic sanitizing."""
    if not isinstance(payload, dict) or not romance_name_to_key:
        return ""
    for suffix in ("a", "b"):
        title = str(payload.get(f"choice_{suffix}") or "")
        hits = _romance_route_hits(title, romance_name_to_key)
        keys = {key for key, _alias in hits}
        if len(keys) != 1 or any(marker in title for marker in _NEGATIVE_ROMANCE_CHOICE_MARKERS):
            continue
        key = next(iter(keys))
        matched_alias = next(alias for route_key, alias in hits if route_key == key)
        value = _safe_int((payload.get(f"effects_{suffix}") or {}).get(key, 0))
        if value <= 0:
            return f"choice_{suffix} 明确倾向{matched_alias}，但 {key} 未获得正向 effects"
    return ""


def ensure_distinct_modular_choice_text(
    choice_text: str,
    recent: list[str] | None,
) -> str:
    """Keep modular buttons concrete, investigative and distinct."""
    candidate = str(choice_text or "").strip()
    recent_window = [str(item) for item in (recent or [])[-30:] if str(item).strip()]
    is_investigation = any(marker in candidate for marker in _INVESTIGATION_ACTION_MARKERS)
    candidate_is_view_ask = candidate.startswith(("查看", "询问"))
    recent_view_ask = sum(
        item.startswith(("查看", "询问")) for item in recent_window[-10:]
    )
    template_family_crowded = candidate_is_view_ask and recent_view_ask >= 2
    if (
        candidate
        and is_investigation
        and not template_family_crowded
        and not any(_choice_text_is_similar(candidate, old) for old in recent_window)
    ):
        return candidate
    for replacement in _MODULAR_CHOICE_TEMPLATES:
        if not any(_choice_text_is_similar(replacement, old) for old in recent_window):
            return replacement
    return "换一处现场继续查验"


def normalize_clue_flags(
    flags: dict | None,
    *,
    source_text: str,
) -> tuple[dict[str, dict], list[dict[str, str]]]:
    """Normalize route clue metadata and reject unsupported decisive clues."""
    normalized: dict[str, dict] = {}
    issues: list[dict[str, str]] = []
    if not isinstance(flags, dict):
        return normalized, issues
    source = str(source_text or "")
    for raw_key, raw_value in flags.items():
        key = str(raw_key or "").strip()
        if not key or not isinstance(raw_value, dict):
            continue
        source_type = str(raw_value.get("source_type") or "safe_observation").strip()
        if source_type not in _CLUE_SOURCE_TYPES:
            source_type = "safe_observation"
        excerpt = str(raw_value.get("source_excerpt") or "").strip()
        if source_type == "decisive_clue" and (not excerpt or excerpt not in source):
            issues.append(
                {
                    "category": "clue_source_missing",
                    "route": key,
                    "reason": f"决定性线索 {key} 缺少前置原文来源",
                    "repair_field": "clue_flags",
                }
            )
            continue
        if source_type == "source_fact" and excerpt and excerpt not in source:
            source_type = "safe_observation"
        normalized[key] = {
            "source_type": source_type,
            "source_excerpt": excerpt,
            "category": str(raw_value.get("category") or "general").strip() or "general",
        }
    return normalized, issues


def _shared_distinctive_phrase(left: str, right: str, *, min_size: int = 4) -> str:
    left_clean = re.sub(r"[\s，。！？；：、‘’“”（）()《》<>]", "", str(left or ""))
    right_clean = re.sub(r"[\s，。！？；：、‘’“”（）()《》<>]", "", str(right or ""))
    match = SequenceMatcher(None, left_clean, right_clean).find_longest_match()
    phrase = left_clean[match.a : match.a + match.size]
    generic = (
        "我把", "随后", "继续", "准备", "已经", "就是", "这里", "那里", "众人",
        "主角", "他们", "我们", "下来", "起来", "前往", "回到", "发现",
    )
    if len(phrase) < min_size or any(phrase == item for item in generic):
        return ""
    return phrase


def _action_phrases(text: str) -> list[str]:
    phrases: list[str] = []
    for marker in _MODULAR_COMPLETION_MARKERS:
        for match in re.finditer(
            re.escape(marker) + r"[^，。！？；\n]{0,10}",
            str(text or ""),
        ):
            phrase = match.group(0).strip()
            if len(phrase) >= 4:
                phrases.append(phrase)
    return phrases


def validate_soft_branch_transition(
    candidate: dict | None,
    *,
    upper_text: str,
    downstream_text: str,
) -> list[dict[str, str]]:
    """Fast local check that a modular detour can still enter the original downstream scene."""
    if not isinstance(candidate, dict):
        return [
            {
                "category": "invalid_candidate",
                "route": "both",
                "reason": "调查软分支不是有效 JSON 对象",
                "repair_field": "all",
            }
        ]
    issues: list[dict[str, str]] = []
    downstream = str(downstream_text or "")
    upper = str(upper_text or "")
    downstream_actions = _action_phrases(downstream)
    downstream_reveal = any(
        marker in downstream
        for marker in ("才发现", "这才发现", "终于发现", "才确认", "真相", "凶手", "机关")
    )
    for suffix in ("a", "b"):
        route = str(candidate.get(f"content_{suffix}") or "")
        route_label = suffix.upper()
        if not route.strip():
            issues.append(
                {
                    "category": "invalid_candidate",
                    "route": route_label,
                    "reason": f"content_{suffix} 为空",
                    "repair_field": f"content_{suffix}",
                }
            )
            continue

        exact_action = next(
            (
                action
                for action in downstream_actions
                if action[: min(len(action), 6)] in route
            ),
            "",
        )
        if exact_action:
            issues.append(
                {
                    "category": "downstream_action_completed",
                    "route": route_label,
                    "reason": f"提前完成下游动作：{exact_action[:24]}",
                    "repair_field": f"content_{suffix}",
                }
            )

        if (
            any(marker in route for marker in _MODULAR_OBJECT_MOVE_MARKERS)
            and any(marker in downstream for marker in _MODULAR_OBJECT_STAY_MARKERS)
        ):
            shared_object = _shared_distinctive_phrase(route, downstream, min_size=2)
            if shared_object and shared_object not in {"那张", "一张", "仍在", "还在"}:
                issues.append(
                    {
                        "category": "object_state_conflict",
                        "route": route_label,
                        "reason": f"软分支移动了下游仍需留在原处的对象：{shared_object[:16]}",
                        "repair_field": f"content_{suffix}",
                    }
                )

        departing_names = [
            match.group(1)
            for match in re.finditer(
                r"(?:我让|让|叫)?([\u4e00-\u9fff]{2,4}?)(?:转身|先行|独自|当即|便|就)?"
                r"(?:离开|离去|回城|回府|回家|退场)",
                route,
            )
        ]
        for name in departing_names:
            if name in downstream and any(
                marker in downstream
                for marker in ("仍", "还", "继续", "当即", "随即", "开口", "站在", "坐在")
            ):
                issues.append(
                    {
                        "category": "character_state_conflict",
                        "route": route_label,
                        "reason": f"软分支让{name}离场，但下游仍要求其在场",
                        "repair_field": f"content_{suffix}",
                    }
                )

        raw_flags = candidate.get(f"clue_flags_{suffix}")
        _, flag_issues = normalize_clue_flags(raw_flags, source_text=upper)
        for flag_issue in flag_issues:
            issues.append(
                {
                    "category": "decisive_clue_leaked",
                    "route": route_label,
                    "reason": flag_issue["reason"],
                    "repair_field": f"clue_flags_{suffix}",
                }
            )

        leaked_phrase = _shared_distinctive_phrase(route, downstream, min_size=4)
        if (
            leaked_phrase
            and leaked_phrase not in upper
            and (downstream_reveal or any(marker in route for marker in ("确认", "发现", "真相", "证据")))
        ):
            issues.append(
                {
                    "category": "decisive_clue_leaked",
                    "route": route_label,
                    "reason": f"提前使用下游才揭示的信息：{leaked_phrase[:20]}",
                    "repair_field": f"content_{suffix}",
                }
            )

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (issue["category"], issue["route"], issue["repair_field"])
        if key not in seen:
            seen.add(key)
            deduped.append(issue)
    return deduped


def _protagonist_names_from_worldview(worldview: dict) -> list[str]:
    names: list[str] = []
    if not isinstance(worldview, dict):
        return names
    for character in worldview.get("characters", []) or []:
        if not isinstance(character, dict):
            continue
        if not _is_declared_protagonist(character):
            continue
        for name in [character.get("name"), *(character.get("aliases") or [])]:
            clean = str(name or "").strip()
            if clean and clean not in names:
                names.append(clean)
    return names


def soft_branch_generation_identity(branch_type: str) -> dict[str, int]:
    """Return branch-type-specific generation identity for checkpoint invalidation."""

    normalized_branch_type = str(branch_type or "").strip().lower()
    identity = {"original_scene_budget_version": 2}
    if normalized_branch_type == "modular":
        # v4 adds semantic judgment for ambiguous named references; old
        # omitted gaps must be reconsidered.
        identity["protagonist_presence_rule_version"] = 4
        return identity
    if normalized_branch_type == "romance":
        # Covers composite-target parsing, character-card aliases, legacy
        # conflict shapes, canonical-name collisions, protagonist exclusion,
        # and the bounded downstream presence window/prompt hint. Bump this
        # whenever those rules change so old omitted-gap checkpoints are
        # recomputed.
        identity["romance_presence_rule_version"] = 4
        return identity
    return identity


def plan_has_current_soft_branch_generation_identity(plan: dict) -> bool:
    """Reject artifacts produced before the active branch-specific rules."""

    if not isinstance(plan, dict):
        return False
    branch_type = str(plan.get("branch_type") or "").strip().lower()
    expected = soft_branch_generation_identity(branch_type)
    # Legacy checkpoints may carry the romance signal separately from the
    # branch type.  Invalidate those checkpoints unless they have the current
    # romance presence-rule identity.
    if bool(plan.get("is_romance_oriented")) and branch_type != "romance":
        expected = dict(expected)
        expected["romance_presence_rule_version"] = 4
    if not expected:
        return True
    return plan.get("soft_branch_generation_identity") == expected


def _text_outside_dialogue(text: str) -> str:
    """Remove quoted speech before checking narrator-person pronouns."""

    value = str(text or "")
    for pattern in (
        r"“[^”]*”",
        r"「[^」]*」",
        r"『[^』]*』",
        r'"[^"\n]*"',
    ):
        value = re.sub(pattern, "", value, flags=re.DOTALL)
    return value


def _first_person_narrator_is_unique_protagonist(
    worldview: dict,
    chapter_text: str,
) -> bool:
    """Conservatively map narrative 'I' to the sole declared protagonist."""

    if not isinstance(worldview, dict):
        return False
    protagonists = [
        character
        for character in (worldview.get("characters", []) or [])
        if isinstance(character, dict)
        and _is_declared_protagonist(character)
        and str(character.get("name") or "").strip()
    ]
    if len(protagonists) != 1:
        return False
    # Keep this soft-branch adjacency check on the same canonical POV
    # inference as terminal-state extraction.  A style note may legitimately
    # mention third-person dialogue/转述 while the narrator remains first
    # person; substring rejection here used to suppress valid protagonist
    # branches.
    contract = infer_narrative_perspective_contract(
        worldview,
        text=chapter_text,
    )
    if contract.get("perspective") not in {"first_person", "mixed"}:
        return False
    narrator_text = _text_outside_dialogue(chapter_text)
    return len(re.findall(r"(?<![自忘无唯舍])我(?:们)?", narrator_text)) >= 2


def _protagonist_present_in_adjacent_scenes(
    *,
    upper: str,
    lower_head: str,
    chapter_text: str,
    worldview: dict,
    protagonist_names: list[str],
) -> bool:
    if any(name in upper or name in lower_head for name in protagonist_names):
        return True
    if not _first_person_narrator_is_unique_protagonist(worldview, chapter_text):
        return False
    adjacent_narration = _text_outside_dialogue(f"{upper}\n{lower_head}")
    return bool(re.search(r"(?<![自忘无唯舍])我(?:们)?", adjacent_narration))


_PROTAGONIST_MEMORY_MARKERS = (
    "回忆", "想起", "记起", "记得", "记忆", "往事", "曾经", "当年",
    "梦里", "梦中", "幻觉", "照片", "相片", "画像", "录音", "录像",
    "日记里", "信里", "档案里", "旧闻",
)
_PROTAGONIST_MENTION_MARKERS = (
    "提到", "提及", "说起", "谈到", "听说", "传闻", "据说", "名字",
    "记录", "资料", "名单", "报道", "通知", "口中", "称他", "称她",
    "关于", "有关",
)
_PROTAGONIST_OFFSCREEN_MARKERS = (
    "不在场", "不在这里", "没来", "未到", "没有来", "离场", "离开现场",
    "远在", "场外", "缺席", "不曾出现", "未曾出现",
)
_PROTAGONIST_ACTIVE_MARKERS = (
    "走", "来", "去", "进", "出", "站", "坐", "看", "望", "听", "说",
    "问", "答", "道", "喊", "叫", "回应", "回头", "抬", "低头", "拿",
    "放", "推", "拉", "打开", "关上", "检查", "调查", "寻找", "发现",
    "确认", "等待", "跟上", "转身", "停下", "走来", "走进", "走出",
)
_PROTAGONIST_PRESENCE_MARKERS = (
    "仍在", "还在", "留在", "站在", "坐在", "守在", "待在", "位于",
    "仍然站", "仍然坐", "没有离开现场", "并未离开现场",
)


def _offscreen_marker_is_negated(segment: str, marker: str) -> bool:
    """Return whether an offscreen phrase is negated by nearby wording."""
    start = str(segment).find(marker)
    if start < 0:
        return False
    prefix = str(segment)[max(0, start - 4):start]
    return bool(re.search(r"(?:没|没有|未|并未|不|尚未)$", prefix))


def _named_protagonist_evidence(
    text: str,
    protagonist_names: list[str],
) -> tuple[str, str]:
    """Classify explicit protagonist evidence in a bounded scene window.

    Name substring matching is intentionally not enough to mean ``active``:
    names in photographs, memories, records, rumours, or explicit absence
    statements describe the protagonist without placing them in the scene.
    The result is ``(status, reason)`` and is conservative when context is
    ambiguous.
    """
    value = str(text or "").strip()
    names = [str(name).strip() for name in (protagonist_names or []) if str(name).strip()]
    if not value or not names:
        return "unknown", "empty_window"

    segments = [
        segment.strip()
        for segment in re.split(r"[。！？!?\n；;]+", value)
        if segment.strip()
    ]
    matched = [segment for segment in segments if any(name in segment for name in names)]
    if not matched:
        return "offscreen", "no_named_reference"

    saw_mentioned = False
    explicit_mention = False
    saw_memory = False
    saw_offscreen = False
    for segment in matched:
        if any(marker in segment for marker in _PROTAGONIST_MEMORY_MARKERS):
            saw_memory = True
            continue
        explicit_offscreen = any(
            marker in segment
            and not _offscreen_marker_is_negated(segment, marker)
            for marker in _PROTAGONIST_OFFSCREEN_MARKERS
        )
        if explicit_offscreen:
            saw_offscreen = True
            continue
        # A nearby action/speech predicate is required before declaring a
        # named character active.  Bare names remain ``mentioned``.
        if any(marker in segment for marker in _PROTAGONIST_ACTIVE_MARKERS) or any(
            marker in segment for marker in _PROTAGONIST_PRESENCE_MARKERS
        ):
            return "active", "named_action_or_speech"
        if any(marker in segment for marker in _PROTAGONIST_MENTION_MARKERS):
            saw_mentioned = True
            explicit_mention = True
            continue
        saw_mentioned = True

    if saw_memory:
        return "memory", "memory_or_record_reference"
    if saw_mentioned:
        return (
            "mentioned",
            "explicit_mention_marker" if explicit_mention else "named_reference_without_action",
        )
    if saw_offscreen:
        return "offscreen", "explicit_offscreen_statement"
    return "unknown", "ambiguous_named_reference"


def _semantic_protagonist_presence_status(
    *,
    upper: str,
    lower_prefix: str,
    lower: str,
    protagonist_names: list[str],
    semantic_client=None,
    metrics: dict | None = None,
) -> tuple[str, str]:
    """Use one bounded LLM call for an otherwise ambiguous name reference."""
    if semantic_client is None:
        return "unknown", "semantic_client_unavailable"
    names = [str(name).strip() for name in (protagonist_names or []) if str(name).strip()]
    if not names:
        return "unknown", "no_protagonist_name"
    if isinstance(metrics, dict):
        _increment_soft_branch_metric(metrics, "semantic_presence_calls")
    prompt = (
        "判断主角是否在当前切分点实际参与场景。只能返回 JSON，不要输出解释。\n"
        f"主角姓名或别名：{', '.join(names)}\n"
        "status 只能是 active、mentioned、memory、offscreen、unknown。\n"
        "active=当前场景中正在行动、说话或作为叙事视角存在；"
        "mentioned=只在记录、传闻、名单或他人话语中被提及；"
        "memory=回忆、照片、梦境、录音、日记等非当前时空；"
        "offscreen=明确不在现场、未到场或已离开；unknown=证据不足。\n"
        "不得把姓名出现本身当作 active；必须以当前场景语义为依据。\n"
        f"上游场景：\n{str(upper or '')[:2400]}\n"
        f"下游前段：\n{str(lower_prefix or '')[:2400]}\n"
        f"下游完整场景（仅用于判断是否延迟出场）：\n{str(lower or '')[:4000]}\n"
        '返回格式：{"status":"active|mentioned|memory|offscreen|unknown",'
        '"confidence":0.0,"evidence":"简短依据","source_span":"原文片段"}'
    )
    try:
        result = semantic_client.chat_completion_json(
            system_prompt="你是小说场景实体与叙事视角判定器。严格输出 JSON。",
            user_prompt=prompt,
            temperature=0.1,
            max_tokens=500,
        )
        if not isinstance(result, dict):
            raise ValueError("semantic presence result is not an object")
        status = str(result.get("status") or "").strip().lower()
        confidence = float(result.get("confidence", 0) or 0)
        if status not in {"active", "mentioned", "memory", "offscreen", "unknown"}:
            raise ValueError("semantic presence status is invalid")
        if confidence < 0.55:
            return "unknown", "semantic_low_confidence"
        return status, "semantic_llm"
    except Exception:
        if isinstance(metrics, dict):
            _increment_soft_branch_metric(metrics, "semantic_presence_fallbacks")
        return "unknown", "semantic_error"


def _modular_protagonist_presence_status(
    *,
    upper: str,
    lower: str,
    protagonist_names: list[str],
    worldview: dict | None = None,
    chapter_text: str = "",
    semantic_client=None,
    metrics: dict | None = None,
) -> tuple[str, str]:
    """Classify adjacency so delayed protagonist entry is not treated as absence."""
    names = [str(name).strip() for name in (protagonist_names or []) if str(name).strip()]
    upper_text = str(upper or "")
    lower_text = str(lower or "")
    lower_prefix = _scene_presence_prefix(lower_text)
    upper_status, upper_reason = _named_protagonist_evidence(upper_text, names)
    prefix_status, prefix_reason = _named_protagonist_evidence(lower_prefix, names)
    if "active" in {upper_status, prefix_status}:
        return "active", "upper_or_downstream_prefix"
    # A first-person novel may never spell out the protagonist's name in the
    # prose.  Reuse the canonical POV inference used by the legacy adjacency
    # check, but still require an actual first-person marker in this adjacent
    # window so a style note alone cannot make an offscreen gap active.
    if _first_person_narrator_is_unique_protagonist(
        worldview or {}, chapter_text,
    ):
        adjacent_narration = _text_outside_dialogue(
            f"{upper_text}\n{lower_prefix}"
        )
        if re.search(r"(?<![自忘无唯舍])我(?:们)?", adjacent_narration):
            return "active", "first_person_narrator"
    lower_status, lower_reason = _named_protagonist_evidence(lower_text, names)
    if lower_status == "active" and prefix_status != "active":
        return "delayed", "downstream_late_entry"
    evidence = (
        (upper_status, upper_reason),
        (prefix_status, prefix_reason),
        (lower_status, lower_reason),
    )
    # Prefer explicit contextual evidence over a window with no name at all.
    # Otherwise a clean downstream prefix would hide an upper-scene mention.
    for status, reason in evidence:
        if status == "memory" or (
            status == "mentioned" and reason == "explicit_mention_marker"
        ):
            return status, reason
    for status, reason in evidence:
        if status == "offscreen" and reason == "explicit_offscreen_statement":
            return status, reason
    for status, reason in evidence:
        if status == "mentioned" or status == "unknown":
            semantic_status, semantic_reason = _semantic_protagonist_presence_status(
                upper=upper_text,
                lower_prefix=lower_prefix,
                lower=lower_text,
                protagonist_names=names,
                semantic_client=semantic_client,
                metrics=metrics,
            )
            if semantic_reason == "semantic_client_unavailable":
                return status, reason
            return semantic_status, semantic_reason
    if any(status == "offscreen" for status, _reason in evidence):
        return "offscreen", "no_explicit_name_in_adjacent_scenes"
    return "offscreen", "no_explicit_name_in_adjacent_scenes"


def ensure_distinct_modular_flavor_choice_text(
    choice_text: str,
    recent: list[str] | None,
) -> str:
    """Normalize a non-investigative flavor button while retaining diversity."""
    candidate = str(choice_text or "").strip()
    recent_window = [str(item) for item in (recent or [])[-30:] if str(item).strip()]
    if (
        4 <= len(candidate) <= 15
        and not any(_choice_text_is_similar(candidate, old) for old in recent_window)
    ):
        return candidate
    for replacement in _MODULAR_FLAVOR_CHOICE_TEMPLATES:
        if not any(_choice_text_is_similar(replacement, old) for old in recent_window):
            return replacement
    return "留意周围动静"


def _fix_romance_effect_direction(res: dict, romance_name_to_key: dict) -> dict:
    """
    恋爱向方向自洽纠偏（保守版）：只在证据非常明确时才交换 effects 的正负对象，
    使“选项标题里点名倾向的角色”获得正值。

    关键设计（吸取教训）：
    - 只看 **choice 标题**（短、指向明确），不看正文——正文里“被牵挂/被牺牲”的对象
      往往比“情感真正倾斜”的对象出现更多次，用正文词频判断会误判、制造假选择。
    - 标题里必须**唯一**点名某个 romance 角色（只出现 1 个角色名），才认定倾向明确。
    - 纠偏后若导致 effects_a == effects_b（假选择），则**回滚**该次纠偏，宁可不改。
    - 只在恋爱向（romance_name_to_key 非空）下生效；人格向不处理。
    就地修改并返回 res。
    """
    if not romance_name_to_key:
        return res

    def _sole_route_in_title(title: str) -> tuple[str, str]:
        """Return ``(route_key, matched_alias)`` only for an unambiguous title."""

        hits = _romance_route_hits(title, romance_name_to_key)
        keys = {key for key, _alias in hits}
        if len(keys) != 1 or not hits:
            return "", ""
        return hits[0]

    def _norm(eff: dict) -> tuple:
        return tuple(sorted((k, _safe_int(v)) for k, v in eff.items())) if isinstance(eff, dict) else ()

    before_a, before_b = _norm(res.get("effects_a")), _norm(res.get("effects_b"))

    # 两个标题分别明确点名不同攻略角色时，直接构造双轴镜像：
    # A 点名者在 A 路为正，B 点名者在 B 路为正，避免“想慕声却扣慕声好感”。
    key_a, _alias_a = _sole_route_in_title(str(res.get("choice_a", "")))
    key_b, _alias_b = _sole_route_in_title(str(res.get("choice_b", "")))
    if key_a and key_b and key_a != key_b:
        magnitudes = [
            abs(_safe_int(value))
            for effects in (res.get("effects_a"), res.get("effects_b"))
            if isinstance(effects, dict)
            for value in effects.values()
            if _safe_int(value)
        ]
        strength = max(magnitudes or [3])
        sign_a = -1 if any(marker in str(res.get("choice_a") or "") for marker in _NEGATIVE_ROMANCE_CHOICE_MARKERS) else 1
        sign_b = -1 if any(marker in str(res.get("choice_b") or "") for marker in _NEGATIVE_ROMANCE_CHOICE_MARKERS) else 1
        res["effects_a"] = {key_a: sign_a * strength, key_b: -sign_b * strength}
        res["effects_b"] = {key_a: -sign_a * strength, key_b: sign_b * strength}
        return res

    for suffix in ("a", "b"):
        eff = res.get(f"effects_{suffix}")
        if not isinstance(eff, dict) or not eff:
            continue
        title = str(res.get(f"choice_{suffix}", ""))
        target_key, matched_alias = _sole_route_in_title(title)
        if not target_key:
            continue
        if target_key not in eff:
            continue
        expects_positive = not any(marker in title for marker in _NEGATIVE_ROMANCE_CHOICE_MARKERS)
        current = _safe_int(eff[target_key])
        if (expects_positive and current < 0) or (not expects_positive and current > 0):
            desired_keys = [
                k for k, v in eff.items()
                if k != target_key
                and k in romance_name_to_key.values()
                and ((_safe_int(v) < 0) if not expects_positive else (_safe_int(v) > 0))
            ]
            if desired_keys:
                other_key = desired_keys[0]
                eff[target_key], eff[other_key] = eff[other_key], eff[target_key]
                direction = "正值" if expects_positive else "负值"
                print(f"      🔧 effects_{suffix} 方向纠偏：标题点名'{matched_alias}'应为{direction}，"
                      f"已交换 {target_key} ↔ {other_key}")

    # 假选择护栏：若纠偏后 A==B，说明判断有误，回滚到 LLM 原始输出
    if _norm(res.get("effects_a")) == _norm(res.get("effects_b")):
        # 交换正负对象可能把另一条路也变成完全相同的 effects。恢复原始
        # 两路后，只把标题明确点名的路线轴校正为正值，保留其余变量差异，
        # 既满足方向契约又不制造“两个按钮实际相同”的假选择。
        print("      ⚠️  纠偏后 A==B（假选择），改为仅校正点名路线轴")
        res["effects_a"] = {k: v for k, v in before_a}
        res["effects_b"] = {k: v for k, v in before_b}
        for suffix in ("a", "b"):
            target_key, _matched_alias = _sole_route_in_title(
                str(res.get(f"choice_{suffix}", ""))
            )
            if not target_key:
                continue
            effect = res.get(f"effects_{suffix}")
            if not isinstance(effect, dict):
                effect = {}
                res[f"effects_{suffix}"] = effect
            magnitude = abs(_safe_int(effect.get(target_key))) or 3
            effect[target_key] = magnitude if suffix == "a" else -magnitude
        # If only one route names a character, make the sibling route the
        # explicit counter-direction on that same affection axis.
        for suffix, other_suffix in (("a", "b"), ("b", "a")):
            target_key, _matched_alias = _sole_route_in_title(
                str(res.get(f"choice_{suffix}", ""))
            )
            if not target_key:
                continue
            source = res.get(f"effects_{suffix}") or {}
            sibling = res.get(f"effects_{other_suffix}")
            if not isinstance(sibling, dict):
                sibling = {}
                res[f"effects_{other_suffix}"] = sibling
            magnitude = abs(_safe_int(source.get(target_key))) or 3
            sibling[target_key] = -magnitude if suffix == "a" else magnitude

    return res


def _safe_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _match_names_to_affection_keys(
    names: list,
    aff_keys: list,
    variable_defs: list | None = None,
    aliases_by_name: dict[str, list[str]] | None = None,
) -> dict:
    """
    把角色中文名匹配到 affection_<拼音> 变量 key。
    如 沈清舒 → affection_shen_qingshu。用 pypinyin 转拼音后做子串匹配；
    pypinyin 不可用或匹配不上时，退化为按顺序对应。
    返回 {角色名: key}。
    """
    result = {}
    remaining = list(aff_keys)

    aliases_by_name = aliases_by_name if isinstance(aliases_by_name, dict) else {}

    def _description_score(name: str, description: str) -> tuple[int, int, int]:
        """Score exact route-name evidence in a variable description.

        A route description may use an alias rather than the canonical name.
        Prefer the longest explicit alias, then occurrence count; this avoids
        letting a short alias win merely because it is a substring of a longer
        character title.  The final component keeps canonical-name evidence
        deterministic when both canonical and alias forms are present.
        """

        candidates = []
        seen: set[str] = set()
        for value in [name, *(aliases_by_name.get(name) or [])]:
            clean = str(value or "").strip()
            if len(clean) < 2 or clean in seen:
                continue
            seen.add(clean)
            count = description.count(clean)
            if count:
                candidates.append((len(clean), count, int(clean == name)))
        if not candidates:
            return (0, 0, 0)
        longest, count, canonical_hit = max(candidates)
        return (longest, count, canonical_hit)

    # 变量描述通常直接写明攻略角色名或其别名，优先使用这一层确定性证据。
    # 这样即使没有 pypinyin，也不会把“世界观全部角色”按顺序错配到 affection 轴。
    for variable in variable_defs or []:
        if not isinstance(variable, dict):
            continue
        key = str(variable.get("key", ""))
        if key not in remaining:
            continue
        description = str(variable.get("description", ""))
        scores = {
            name: _description_score(name, description)
            for name in names
            if name and name not in result
        }
        highest = max(scores.values(), default=(0, 0, 0))
        hits = [
            name for name, score in scores.items()
            if score == highest and score != (0, 0, 0)
        ]
        if len(hits) == 1:
            result[hits[0]] = key
            remaining.remove(key)

    try:
        from pypinyin import lazy_pinyin

        def _pinyin_forms(name: str) -> list[str]:
            values = [name, *(aliases_by_name.get(name) or [])]
            forms: list[str] = []
            for value in values:
                clean = str(value or "").strip()
                if not clean:
                    continue
                normalized = "".join(lazy_pinyin(clean)).lower()
                if normalized and normalized not in forms:
                    forms.append(normalized)
            return forms

        for nm in names:
            if nm in result:
                continue
            pinyin_forms = _pinyin_forms(nm)
            candidates: list[tuple[tuple[int, int], str]] = []
            for k in remaining:
                # key 里的拼音去掉下划线：affection_shen_qingshu -> shenqingshu
                k_py = k.replace("affection_", "").replace("_", "").lower()
                for form in pinyin_forms:
                    if k_py == form:
                        candidates.append(((3, len(form)), k))
                    elif form in k_py or k_py in form:
                        candidates.append(((1, len(form)), k))
            best_score = max((score for score, _key in candidates), default=None)
            best_keys = {
                key for score, key in candidates if score == best_score
            }
            if len(best_keys) == 1:
                hit = next(iter(best_keys))
                result[nm] = hit
                remaining.remove(hit)
        # 未匹配上的名字，按剩余 key 顺序兜底对应
        unmatched = [n for n in names if n not in result]
        for n, k in zip(unmatched, remaining):
            result[n] = k
    except Exception:
        # pypinyin 不可用：按顺序对应兜底
        unmatched = [n for n in names if n not in result]
        for n, k in zip(unmatched, remaining):
            result[n] = k
    return result


# 场景转换标记词：出现在段落开头时是极佳的切分点（作为下半场第一段）
_SCENE_TRANSITION_MARKERS = [
    "次日", "翌日", "与此同时", "同一时间", "几天后", "三日后", "半月后",
    "夜，", "深夜", "黎明", "清晨", "傍晚", "日落", "天亮",
    "【", "第",  # 章节内小节标题
]

# 情绪收尾词：段落以这些内容结束，适合作为上半场末尾
_EMOTION_CLOSING_MARKERS = [
    "门", "关上", "消失", "沉默", "死寂", "走了", "离开",
    "……", "。"
]


_CLOSING_QUOTES = ('\u201d', '\u300d', '"', '\u2019')
_CLIMAX_ACTION_ENDINGS = ('了。', '去。', '下。', '上。', '开。', '来。', '走了', '离开', '消失', '跪下', '站起')


def _has_completed_climax(paragraphs: list, idx: int, lookback: int = 4) -> tuple:
    """
    检查 idx 之前 lookback 段内是否存在'已完成的情绪高潮'：
    - 大段直接引语（含台词，>20字）以闭引号结尾
    - 或短促的行动收束句（以常见完成动词结尾，<60字）
    返回 (found: bool, distance: int) —— distance=1 表示紧邻上方
    """
    for d in range(1, lookback + 1):
        j = idx - d
        if j < 0:
            break
        p = paragraphs[j]
        # 完成的直接引语（实质性台词）
        if any(p.endswith(q) for q in _CLOSING_QUOTES) and len(p) > 20:
            return True, d
        # 短促行动收束句
        if len(p) < 60 and any(p.endswith(e) for e in _CLIMAX_ACTION_ENDINGS):
            return True, d
    return False, 0


def _score_split_point(paragraphs: list, idx: int) -> int:
    """为段落索引 idx 作为'下半场开头'打分，分越高越适合作为切分点。"""
    if idx <= 0 or idx >= len(paragraphs):
        return 0
    p_before = paragraphs[idx - 1]  # 上半场末尾段
    p_after = paragraphs[idx]        # 下半场开头段
    score = 0
    # 下半场开头是场景转换词：极佳
    if any(p_after.startswith(m) for m in _SCENE_TRANSITION_MARKERS):
        score += 10
    # 上半场末尾不在对话中（无引号开头）：较好
    if not p_before.startswith("\u201c") and not p_before.startswith("\u300c"):
        score += 5
    # 上半场末尾是短促段落（动作收尾、声响、叹息）：好
    if len(p_before) < 50:
        score += 3
    # 避免切在上半场末尾是对话进行中
    if p_before.endswith("\u201d") or p_before.endswith("\u300d"):
        score -= 8
    # 核心新增：避免切在情绪高潮（关键台词/行动）完成之后
    # 如果切分点前 1-4 段内有已完成的高潮，距离越近惩罚越重
    found, dist = _has_completed_climax(paragraphs, idx, lookback=4)
    if found:
        # dist=1: -12, dist=2: -9, dist=3: -6, dist=4: -3
        score -= (5 - dist) * 3
    return score


def _split_scenes(chapter_text: str, scene_count: int = 3) -> list:
    """
    将章节按自然段落切分为 scene_count 个场景段。
    在均分点附近滑动寻找语义断点（场景转换、情绪收尾），避免腰斩对话。
    """
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', chapter_text) if p.strip()]
    # 部分中文小说以“每个自然段一行”的形式保存，段间没有空行。
    # 当空行分段不足以形成目标场景时，退回到非空单行作为自然段，
    # 否则整章会被当成一个段落，软分支注入会静默跳过。
    if len(paragraphs) <= scene_count:
        line_paragraphs = [line.strip() for line in chapter_text.splitlines() if line.strip()]
        if len(line_paragraphs) > len(paragraphs):
            paragraphs = line_paragraphs
    if len(paragraphs) <= scene_count:
        return paragraphs if paragraphs else [chapter_text]

    chunk = len(paragraphs) // scene_count
    split_indices = []
    for i in range(1, scene_count):
        target = i * chunk
        # 在 target 前后 6 个段落内寻找最佳切分点（扩大窗口以绕开已完成的高潮）
        # 1w 章长方案：每章约 4-5 段，切点搜索窗口缩小到 ±4 以避免跨过 1/3 章
        search_start = max(1, target - 4)
        search_end = min(len(paragraphs) - 1, target + 4)
        best_idx = target
        best_score = _score_split_point(paragraphs, target)
        for j in range(search_start, search_end + 1):
            s = _score_split_point(paragraphs, j)
            if s > best_score:
                best_score = s
                best_idx = j
        split_indices.append(best_idx)

    # 按 split_indices 切分
    scenes = []
    prev = 0
    for idx in split_indices:
        scenes.append('\n\n'.join(paragraphs[prev:idx]))
        prev = idx
    scenes.append('\n\n'.join(paragraphs[prev:]))
    return [s for s in scenes if s.strip()]


def _scene_presence_prefix(
    scene_text: str,
    *,
    max_paragraphs: int = 3,
    max_chars: int = 2400,
) -> str:
    """Return a bounded prefix used only for adjacent-character presence.

    The first paragraph alone is often a setting transition; a route target
    may enter in the next one or two paragraphs.  We inspect a small bounded
    prefix instead of the entire downstream scene so presence detection is
    less brittle without leaking a future scene wholesale into prompts.
    """

    text = str(scene_text or "").strip()
    if not text:
        return ""
    paragraphs = [
        item.strip()
        for item in re.split(r"\n\s*\n", text)
        if item.strip()
    ]
    # Many imported novels use one non-empty line per paragraph.  Fall back
    # to lines when blank-line splitting produced only one item.
    if len(paragraphs) <= 1:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > len(paragraphs):
            paragraphs = lines
    selected = paragraphs[: max(int(max_paragraphs), 1)]
    result = "\n\n".join(selected)
    limit = max(int(max_chars), 1)
    return result[:limit]


_LEGACY_FLAGS_BY_VARIABLE = {
    "self_worth": ("signed_clarification", "school_record_preserved"),
    "honesty": ("illness_disclosed", "truth_record_preserved"),
}


def _legacy_flags_for_effects(effects: dict, legacy_slot: int) -> dict[str, int]:
    """把一次正向人格选择映射为一个可验证遗产标记，避免单次选择解锁整条结局。"""
    flags: dict[str, int] = {}
    for variable_key, candidates in _LEGACY_FLAGS_BY_VARIABLE.items():
        try:
            is_positive = int((effects or {}).get(variable_key, 0)) > 0
        except (TypeError, ValueError):
            is_positive = False
        if is_positive and candidates:
            flags[candidates[int(legacy_slot) % len(candidates)]] = 1
    return flags


def _build_soft_branch_marker(
    choice_a: str,
    content_a: str,
    effects_a: dict,
    choice_b: str,
    content_b: str,
    effects_b: dict,
    bridge: str = "",
    legacy_slot: int = 0,
    clue_flags_a: dict | None = None,
    clue_flags_b: dict | None = None,
    branch_kind: str = "numeric",
    validation_warnings: list[str] | tuple[str, ...] | None = None,
) -> str:
    # bridge 是 B 路心理坍塌的收束段，必须并入 content_b 范围内（在 SOFT_BRANCH_CLOSE 之前）。
    # 若放在 CLOSE 之后，下游解析会把它当成主线正文，导致 A 路玩家也读到 B 路的坍塌——逻辑错位。
    content_b_combined = content_b.strip()
    if bridge.strip():
        content_b_combined += "\n\n" + bridge.strip()
    legacy_flags_a = _legacy_flags_for_effects(effects_a, legacy_slot)
    legacy_flags_b = _legacy_flags_for_effects(effects_b, legacy_slot)
    marker = (
        SOFT_BRANCH_OPEN
        + SOFT_BRANCH_CHOICE_A.format(choice_a=choice_a)
        + content_a.strip() + "\n"
        + SOFT_BRANCH_CHOICE_B.format(choice_b=choice_b)
        + content_b_combined + "\n"
        + f"<!-- effects_a={effects_a} effects_b={effects_b} "
        + f"legacy_flags_a={legacy_flags_a} legacy_flags_b={legacy_flags_b} "
        + f"branch_kind={str(branch_kind or 'numeric')} "
        + f"clue_flags_a={dict(clue_flags_a or {})} clue_flags_b={dict(clue_flags_b or {})} "
        + f"validation_status={'warning' if validation_warnings else 'passed'} "
        + f"validation_warnings={list(validation_warnings or [])} -->\n"
        + SOFT_BRANCH_CLOSE
    )
    return marker


def _extract_last_completed_beat(text: str) -> str:
    """
    从全段文本中提取最后一句已完成的关键台词或行动（用于生成'禁止重复'提示）。
    优先找最后一段实质性直接引语（>15字），其次找最后一句以完成动词收束的短句。
    """
    # 找最后一段直接引语
    matches = list(re.finditer(
        r'[「\u201c\u300c"](.*?)[」\u201d\u300d"]',
        text, re.DOTALL
    ))
    for m in reversed(matches):
        content = m.group(1).strip().replace('\n', '')
        if len(content) > 15:
            return content[:120]
    # 找最后一句以句号结束的短句（<80字，行动收束）
    sentences = re.split(r'[。！？]', text)
    for s in reversed(sentences):
        s = s.strip()
        if 10 < len(s) < 80:
            return s
    return ""


def inject_soft_branches(
    chapter_text: str,
    chapter_idx: int,
    provider: str,
    novel_context=None,
    scenes_count: int = 8,
    variable_defs: list = None,
    total_branches: int = None,
    output_language: str = None,
    current_chapter_summary: str = "",
    branch_type: str = "default",
    required_effect_keys: list[str] | None = None,
    choice_text_history: list[str] | None = None,
    metrics: dict | None = None,
    resume_state: dict | None = None,
    checkpoint_callback=None,
    narrative_pov_contract: dict | None = None,
) -> str:
    """
    将章节切成 scenes_count 个场景段，在段间插入软分支（软分支数 = scenes_count - 1）。
    novel_context: NovelContext，提供角色卡、关系线、风格样本。
    variable_defs: 拓扑规划输出的全局数值变量表，软分支 effects 只能使用其中的 key。
    total_branches: 全书所有会打软分支的节点的分支总数，用于自适应 effects 上限校准。
                    由调用方（tasks.py）在循环前预算并传入；不传则按章节数估算兜底。
    current_chapter_summary: 当前章节的结构化摘要。chapter_idx >= 0 时若不传，从 chapter_summaries[idx] 取；
                              chapter_idx == -1（What-If）时必须由调用方传入。
    返回注入后的 MD 文本。
    """
    client = LLMClient(provider=provider)
    metrics = _initialize_soft_branch_metrics(metrics)
    if choice_text_history is None:
        choice_text_history = []
    scenes = _split_scenes(chapter_text, scene_count=scenes_count)

    if len(scenes) < 2:
        return chapter_text
    _increment_soft_branch_metric(metrics, "theoretical_gap_count", len(scenes) - 1)
    checkpoint_input = {
        "chapter_sha256": content_sha256(chapter_text),
        "scene_count": len(scenes),
        "branch_type": branch_type,
        "output_language": output_language,
        "variable_defs": variable_defs or [],
        "required_effect_keys": required_effect_keys or [],
        "current_chapter_summary": current_chapter_summary,
    }
    checkpoint_input.update(soft_branch_generation_identity(branch_type))
    gap_checkpoint = initialize_soft_branch_gap_checkpoint(
        resume_state,
        gap_count=len(scenes) - 1,
        input_payload=checkpoint_input,
    )

    def _persist_gap(gap_idx: int, marker: str = "", *, omitted: bool = False) -> None:
        record_soft_branch_gap(
            gap_checkpoint,
            gap_index=gap_idx,
            marker=marker,
            omitted=omitted,
        )
        if checkpoint_callback is not None:
            checkpoint_callback(gap_checkpoint)

    def _append_checkpointed_gap(parts: list[str], gap_idx: int) -> bool:
        checkpoint = get_soft_branch_gap(gap_checkpoint, gap_idx)
        if checkpoint is None:
            return False
        if checkpoint.get("omitted"):
            parts.append(scenes[gap_idx + 1])
            print(
                f"   ♻️  恢复第{chapter_idx+1}章第{gap_idx+1}个软分支省略断点"
            )
            return True
        marker = str(checkpoint.get("marker") or "")
        for pattern in (r"▶ 选项A：([^\n]+)", r"▶ 选项B：([^\n]+)"):
            match = re.search(pattern, marker)
            if match:
                remember_choice_text(choice_text_history, match.group(1).strip())
        parts.append(marker)
        next_scene = scenes[gap_idx + 1]
        next_head = next_scene.strip().split("\n\n")[0].strip()
        if next_head and next_head in marker:
            remainder = next_scene.strip()[len(next_head):].lstrip("\n")
            if remainder:
                parts.append(remainder)
        else:
            parts.append(next_scene)
        print(f"   ♻️  恢复第{chapter_idx+1}章第{gap_idx+1}个软分支断点")
        return True

    lang_instr = get_lang_instruction(output_language)

    # ───────────── 锚点块构建（六管齐下）─────────────
    char_cards = getattr(novel_context, "char_cards", "") if novel_context else ""
    relationship_map = getattr(novel_context, "relationship_map", "") if novel_context else ""
    style_samples = getattr(novel_context, "style_samples", "") if novel_context else ""
    narrative_arc = getattr(novel_context, "narrative_arc", "") if novel_context else ""
    world_rules = getattr(novel_context, "world_rules", "") if novel_context else ""
    factions_text = getattr(novel_context, "factions_text", "") if novel_context else ""
    hidden_threads_text = getattr(novel_context, "hidden_threads_text", "") if novel_context else ""
    character_behavior_anchors = getattr(novel_context, "character_behavior_anchors", "") if novel_context else ""
    is_romance_oriented = bool(getattr(novel_context, "is_romance_oriented", False)) if novel_context else False
    chapter_summaries = getattr(novel_context, "chapter_summaries", []) if novel_context else []
    worldview = getattr(novel_context, "worldview", {}) if novel_context else {}
    narrative_pov_contract = _context_narrative_pov_contract(
        novel_context,
        narrative_pov_contract,
    )
    narrative_pov_block = _narrative_pov_prompt_block(narrative_pov_contract)
    _pov_perspective = str(
        (narrative_pov_contract or {}).get("perspective")
        or (narrative_pov_contract or {}).get("mode")
        or "unknown"
    )
    _pov_protagonist = str(
        (narrative_pov_contract or {}).get("protagonist_name")
        or (narrative_pov_contract or {}).get("pov_character")
        or "主角"
    )
    _modular_content_pov_instruction = (
        f"content_a/content_b 正文保持{_pov_protagonist}的第三人称叙述，不得把旁白写成‘我’。"
        if _pov_perspective in {"third_person_limited", "third_person_omniscient"}
        else f"content_a/content_b 正文保持{_pov_protagonist}的第一人称叙述，‘我’只能指主角。"
        if _pov_perspective == "first_person"
        else "content_a/content_b 正文优先严格模仿相邻原文的人称。"
    )
    romance_targets = worldview.get("romance_targets", []) if isinstance(worldview, dict) else []
    protagonist_names = _protagonist_names_from_worldview(worldview)
    protagonist_name = protagonist_names[0] if protagonist_names else "原著主角"

    # 主角 fatal_flaw — Bridge 首选压垮素材
    protagonist_flaw = ""
    protagonist_secrets = []
    if isinstance(worldview, dict):
        for c in worldview.get("characters", []):
            if isinstance(c, dict) and c.get("role", "").startswith("主角"):
                protagonist_flaw = c.get("fatal_flaw", "")
                protagonist_secrets = [s for s in (c.get("secrets") or []) if s]
                break
    protagonist_flaw_block = ""
    if protagonist_flaw:
        protagonist_flaw_block = (
            f"【主角致命软肋（Bridge 设计的首选压垮重量）】{protagonist_flaw}\n"
            f"软分支 B 路 Bridge 在挑选『压垮主角的具体重量』时，请优先调用此软肋——"
            f"让主角的反抗冲动被这个软肋直接掐灭，写出具体的画面/记忆/瞬间。\n\n"
        )

    romance_lookup_text = ""
    romance_name_to_key = {}  # 角色名 -> affection_<key>，用于 effects 方向自洽纠偏
    if is_romance_oriented:
        _aff_keys = [v["key"] for v in (variable_defs or [])
                     if isinstance(v, dict) and str(v.get("key", "")).startswith("affection_")]
        romance_name_to_key = _build_romance_name_to_key(
            romance_targets,
            worldview,
            variable_defs,
            _aff_keys,
        )
        if romance_name_to_key:
            # Keep prompt output compact: aliases are consumed by matching,
            # while the LLM only needs one representative name per route.
            display_by_key: dict[str, str] = {}
            for name, key in romance_name_to_key.items():
                display_by_key.setdefault(key, name)
            lines = [
                f"  - {name} → {key}"
                for key, name in display_by_key.items()
            ]
            romance_lookup_text = (
                "【恋爱向变量映射 — effects 改哪条 affection 轴时，请明确对应到这些角色】\n"
                + "\n".join(lines)
                + "\n\n"
            )
    protagonist_identity_rule = (
        f"【玩家视角身份铁律】玩家第一人称始终固定为主角{protagonist_name}；"
        f"choice/content/bridge 中的‘我’只能指{protagonist_name}，不得切换成恋爱对象或其他配角。"
        f"禁止写成‘我和{protagonist_name}’、‘我握住{protagonist_name}的手’或‘{protagonist_name}被我拉住’。\n\n"
        if is_romance_oriented else ""
    )

    # 取本章/上一章的结构化摘要 + 全书摘要，供"前后文连贯"使用
    # current_chapter_summary 优先（What-If 章必须由调用方传入）
    # 注：不再注入下一章摘要——下一章是软分支选择会塑造的"未来"，提前给会变成剧透源
    if current_chapter_summary:
        cur_summary = current_chapter_summary
    else:
        cur_summary = chapter_summaries[chapter_idx] if 0 <= chapter_idx < len(chapter_summaries) else ""
    prev_summary = (
        chapter_summaries[chapter_idx - 1]
        if 0 <= chapter_idx - 1 < len(chapter_summaries)
        else ""
    )
    chapter_position_label = (
        "What-If 章节（位于原著主线之外的平行线）"
        if chapter_idx == -1 else f"第 {chapter_idx + 1} 章"
    )

    # 分层章节上下文：当前章 ±2 + 相关历史决策召回，不再全量塞入整本摘要。
    summaries_block = ""
    if chapter_summaries:
        from services.story_planner.core.chapter_context import build_layered_summary_block

        context_index = chapter_idx if chapter_idx >= 0 else max(len(chapter_summaries) - 1, 0)
        summaries_block = build_layered_summary_block(
            chapter_summaries,
            current_index=context_index,
            query=current_chapter_summary or cur_summary,
        )

    anchor_block = ""
    if narrative_arc:
        anchor_block += f"【固定世界观摘要/叙事弧线】\n{str(narrative_arc)[:2500]}\n\n"
    if world_rules:
        anchor_block += f"【世界观规则（角色行为必须在此框架内）】\n{str(world_rules)[:2500]}\n\n"
    if factions_text:
        anchor_block += f"{factions_text}\n\n"
    if summaries_block:
        anchor_block += summaries_block
    if cur_summary:
        anchor_block += f"【当前所在位置】{chapter_position_label}\n【本章结构化摘要】\n{cur_summary}\n\n"
    if prev_summary:
        anchor_block += f"【上一章摘要（叙事连贯依据）】\n{prev_summary}\n\n"
    if hidden_threads_text:
        anchor_block += f"{hidden_threads_text}\n\n"
    if char_cards:
        anchor_block += f"【核心人物卡】\n{str(char_cards)[:5000]}\n\n"
    if relationship_map:
        anchor_block += f"{relationship_map}\n\n"
    if protagonist_flaw_block:
        anchor_block += protagonist_flaw_block
    if style_samples:
        anchor_block += f"【语言风格样本（严格模仿）】\n{str(style_samples)[:2000]}\n\n"
    if character_behavior_anchors:
        anchor_block += f"{character_behavior_anchors}\n\n"

    # 自适应数值波动上限：根据总软分支数 + 各变量阈值距离动态计算
    # 假设玩家约 65% 概率持续选同一方向，刚好在最后触达阈值
    if total_branches is not None and total_branches > 0:
        total_branches_estimate = total_branches
    else:
        # 兜底估算：用 chapter_summaries 数量推断章节数
        total_chapters = max(len(chapter_summaries), 1) if chapter_summaries else 3
        total_branches_estimate = (scenes_count - 1) * total_chapters

    # 构建数值变量约束块
    if variable_defs:
        per_branch_caps = calculate_effect_caps(variable_defs, total_branches_estimate)

        var_lines = "\n".join(
            f"  - {v['key']}（初始{v.get('init', 50)}，阈值{v.get('threshold', '?')}，"
            f"本支上限±{per_branch_caps[v['key']]}）："
            f"{v.get('description', '')}，"
            f"结局节点 {v.get('affects_node', '?')}（{v.get('condition', '')}）"
            for v in variable_defs
        )
        var_hint = (
            f"【全局数值变量表（全书约{total_branches_estimate}个软分支，波动上限已按触达概率自动校准）】\n"
            f"rules：① effects 只能用以下 key，禁止自造；"
            f"② 每支 effects 绝对值不超过括号内上限；"
            f"③ 每次只选1-2个最相关的维度，不要每次都堆同一个 key。\n"
            f"{var_lines}\n\n"
        )
        default_key_a = variable_defs[0]["key"]
        default_key_b = variable_defs[-1]["key"]
    else:
        var_hint = ""
        per_branch_caps = {}
        default_key_a = "courage"
        default_key_b = "trust"

    system_prompt = (
        "你是顶级的视觉小说（Visual Novel）编剧，精通人格维度驱动的叙事设计。\n\n"
        + narrative_pov_block
        + protagonist_identity_rule
        +
        "══════════════════════════════════════\n"
        "【第零定律：维度优先，从数值出发】\n"
        "══════════════════════════════════════\n"
        "你设计软分支的第一步，永远是问：「这一段剧情，在考验主角人格维度表里的哪个轴？」\n"
        "A路和B路，是同一个维度轴的两个极点的内心体验，而不是两种随机的『如果我做了别的事』。\n"
        "B路的冲动必须有维度根源：它是主角在某个维度上长期积压的张力，在此刻找到了缺口。\n"
        "如果B路的冲动换成任何其他角色也能有——那就说明你没有从维度出发，重写。\n\n"

        "══════════════════════════════════════\n"
        "【第一定律：薛定谔的物理现实】\n"
        "══════════════════════════════════════\n"
        "软分支绝对不能改变原著的既定事实！\n"
        "无论玩家选A还是B，【接续点】里主角说出口的决定性台词、做出的决定性动作、以及其他人的反应，一字不差。\n"
        "你只能改写主角在那个决定发生前的内心宇宙：他是带着什么情绪走过去的？他在脑海里杀了几次？他的手在颤抖吗？\n"
        "严禁B路出现：①让其他人听到的公开反抗台词；②不可逆的物理动作（拔剑、摔杯、推人）；③任何改变外部事实的行为。\n\n"

        "══════════════════════════════════════\n"
        "【A路设计法则：维度顺流】\n"
        "══════════════════════════════════════\n"
        "主角在这个维度上选择了「顺应」：他压下了内心的另一种可能，接受了命运的走向。\n"
        "重点描写：顺应时内心的平静/麻木/悲哀，以及他是如何带着这种情绪走向接续点的。\n"
        "A路不是『快乐的选择』，有时候顺流比逆流更沉重——写出宿命感，不要写成主角心甘情愿。\n"
        "A路末尾必须平滑衔接【接续点】第一段，读者感觉不到缝隙。\n\n"

        "══════════════════════════════════════\n"
        "【B路设计法则：维度逆流（未遂）】\n"
        "══════════════════════════════════════\n"
        "主角在这个维度上涌现出了「反抗」的极端冲动——这个冲动必须是他这个人、在这个维度上的特有反应，\n"
        "不是任何人在任何情况下都会有的普通冲动。\n\n"
        "B路冲动的三种合法形态（选其中最贴合维度的一种）：\n"
        "  ① 内心独白型：在脑海里把所有想说的话全部排演了一遍，每一个字都是真实的愤怒/绝望/渴望，但嘴唇一直没动。\n"
        "  ② 微动作型：手指握紧、指节泛白、咬碎了后槽牙、袖子里的手指已经触到了匕首柄……但始终没有迈出那一步。\n"
        "  ③ 幻想画面型：他的眼前真实地浮现出了另一种结局的画面——他冲上去、他喊出来、他拒绝了——那画面清晰到像真的发生过。\n\n"
        "B路严禁：①主角开口说出任何他人能听到的反抗台词；②主角做出任何改变当下物理现实的动作；\n"
        "③用「但是」「然而」等转折词直接回避冲动（要让冲动先充分燃烧，再被真实的重量压灭）。\n\n"

        "══════════════════════════════════════\n"
        "【Bridge 定律：心理坍塌（B路专属）】\n"
        "══════════════════════════════════════\n"
        "Bridge 是B路最重要的一笔。它不是收尾，它是这个维度在这一刻的「失守」——\n"
        "主角的冲动被某个具体的重量压垮了，他无声地回到了原位。\n"
        "Bridge 必须写清楚：是什么东西压垮了他？是某个具体的人/场景/记忆/恐惧，还是他突然想通了某件事？\n"
        "**首选压垮素材**：主角的『致命软肋』（见下方 anchor 中的【主角致命软肋】块）。"
        "如果当前场景能让那个软肋被触发，就用它；否则才考虑其他类型的『重量』。\n\n"
        "✅ 好的 Bridge 示例（每条展示不同的「压垮」机制）：\n"
        "  · 软肋型：他几乎已经开口，却在那一瞬间看见她袖口遮不住的旧伤——是他当年没能护住留下的。那股话生生烂在了喉咙里。\n"
        "  · 恐惧型：他的手指已经扣上了——然后他看见了大殿两侧持戟的甲士，看见了台阶下跪着的三十七名族人。他什么都做不了。\n"
        "  · 理智型：他想了一秒钟他能做到什么，再想了一秒钟他会失去什么，第三秒他的手已经松开了。\n"
        "  · 记忆型：他父亲临死前说的那句话像一根钉子，这一刻猝不及防地重新穿透了他——他还不能死在这里。\n\n"
        "❌ 严禁的 Bridge 写法：\n"
        "  · 机械降神：「突然门外传来通报声」「恰在此时鸽哨响起」（外部物理打断 = 绝对禁止）\n"
        "  · 上帝视角废话：「无论如何，命运已经决定了」「但故事还要继续」\n"
        "  · 直接复述接续点原文（Bridge 停在状态扭转完成的那一刻，后续由系统自动拼接，禁止提前复述）\n\n"

        "══════════════════════════════════════\n"
        "【effects 设计法则：维度叙事因果】\n"
        "══════════════════════════════════════\n"
        "effects 不是标签，是这个选择在主角人格轴上留下的痕迹。它必须有叙事逻辑：\n"
        "  - 选A（顺流）：这次顺应让主角在某个维度上更深地向某个方向倾斜，用正值体现。\n"
        "  - 选B（逆流被压）：那股冲动虽然未遂，但它的存在已经改变了主角内心的什么，用负值体现。\n\n"
        "effects 叙事因果示例（好 vs 坏）：\n"
        "  ✅ 好：主角选A，压下了对皇帝的仇恨，顺从地谢了恩——「压抑」维度 +10（他又一次选择了吞咽）\n"
        "  ✅ 好：主角选B，脑海里把弑君画面过了一遍，最终被族人脸孔压垮——「压抑」维度 -8（那个画面以后会反复出现）\n"
        "  ❌ 坏：effects_a = {\"压抑\": 10}，effects_b = {\"压抑\": -8}，但正文里根本没有写「压抑」维度相关的行为\n"
        "  ❌ 坏：选A加「勇气」，选B减「信任」——维度选择跟剧情内容完全不相关\n\n"
        "只能使用变量表中定义的 key，数值范围 -15 到 +15，禁止自造新 key。\n\n"

        "══════════════════════════════════════\n"
        "══════════════════════════════════════\n"
        "【世界锚定：保持叙事连贯】\n"
        "══════════════════════════════════════\n"
        "正文内容应自然地扎根于当前故事的世界观和人物关系中，不要写成脱离剧情的通用心理描写。\n"
        "当场景里有具体的地点、物件、称谓、人物时，优先用这些细节来承载情绪，而不是抽象地描述感受。\n"
        "不强制堆砌世界观词汇，但内容读起来必须像是发生在这个故事里，而不是任何故事里都能套用的模板。\n\n"

        "══════════════════════════════════════\n"
        "【排版与字数硬性要求】\n"
        "══════════════════════════════════════\n"
        "content_a / content_b：必须用 \\n\\n 分成 3 个自然段，每路 400-600 字，写出层次（情绪蓄积 → 冲动/接受顶点 → 回到现实）。\n"
        "bridge：80-150 字，独立成段，有具体的「压垮」原因，不是一笔带过。\n"
        "选项标题：**严格 4-15 字**的第一人称内心独白短句，简短利落，玩家一眼能看懂在选什么。\n"
        "  ① 必须能体现该维度的方向（顺/逆、进/退、留/走）；\n"
        "  ② 优先用动词驱动的短句，可带一个本场景的关键词（人名/物件）但不强求；\n"
        "  ③ 禁止四字成语堆叠（如「血诏入腹，万言俱灭」），禁止超过 15 字的长句独白。\n"
        "  示例（维度「压抑↔爆发」）：\n"
        "    A路：「把那些话咽进血里」（8 字）\n"
        "    B路：「指尖已经摸到刀柄」（9 字）\n"
        "    A路：「我替清舒咽下这口气」（10 字）\n"
        "    B路：「我想一刀劈了那盏茶」（10 字）\n\n"

        "══════════════════════════════════════\n"
        "【effects 强制铁律 — 不准为空】\n"
        "══════════════════════════════════════\n"
        "effects_a 与 effects_b **绝对不能为空对象**。\n"
        "每路至少修改 1 个变量；至少有 1 个变量的绝对值 ≥3（不能全是 ±1/±2 的小数值，那样玩家积累不到阈值）。\n"
        "key 必须严格来自变量表，不得自造、不得拼写错。\n"
        "effects_a 与 effects_b 必须围绕**同一个**主导维度做正负对比（不要 a 改维度 X，b 改维度 Y），\n"
        "次要维度可以辅助修改 1 个，但主导维度的正负绝对值之差应 ≥6。\n"
        "数值范围：每个 key 的修改值在 -15 到 +15 之间。\n\n"

        "══════════════════════════════════════\n"
        "【双模板：恋爱向 vs 人格向】\n"
        "══════════════════════════════════════\n"
        "若变量表 key 形如 `affection_<拼音>`（恋爱向）：\n"
        "  - A 路写「向某位 affection 对象的情感倾斜」（接受亲近、保护、信任她/他的瞬间）\n"
        "  - B 路写「向另一位 affection 对象的情感倾斜的冲动」（回忆与另一人的关键时刻、内心拉扯）\n"
        "  - effects 同时修改 2 条 affection 轴：A路 +cap_main 给主导对象，可附 -低值给另一对象\n"
        "  🚨【方向自洽铁律 — 恋爱向最容易出错，务必遵守】🚨\n"
        "     choice/content 里主角情感倾向、保护、拼命、心软的那个角色，effects 里对应 affection 轴**必须是正值**；\n"
        "     被冷落、割舍、推开的那个角色，对应 affection 轴**必须是负值**。\n"
        "     ❌ 致命错误示例：choice_b=「我想拔剑救红叶」，正文通篇为红叶拼命，effects_b 却是 {affection_红叶:-2, affection_清舒:+3}——\n"
        "        为谁拼命就该给谁加分，此处方向完全标反，绝对禁止！\n"
        "     ✅ 正确：choice_b=「我想拔剑救红叶」→ effects_b={affection_红叶:+3, affection_清舒:-1}。\n"
        "     生成后自查：把 choice_a 里的人名和 effects_a 的正值 key 对齐，choice_b 同理，对不上就改数值方向。\n"
        "若变量表 key 是人格英文（courage/calmness 等）：\n"
        "  - A/B 路就按本 prompt 主体描述的「同一维度的两极」设计\n\n"

        "══════════════════════════════════════\n"
        "【完整正向示例（仿这个调，不要直接复制内容）】\n"
        "══════════════════════════════════════\n"
        "假设变量表里有 humanity（人性，阈值 65）和 obsession（执念，阈值 60）。\n"
        "前置剧情：主角在地牢里看着旧友被刑求；接续点是「他转身签下供词」。\n"
        "好的输出：\n"
        '{"choice_a":"我攥紧袖中的玉珏",'
        '"content_a":"<400-600字三段，写主角看着旧友的伤、用玉珏的温度让自己冷下来、最终带着旁观者一样的平静走向案前>",'
        '"effects_a":{"humanity":-7,"obsession":+5},'
        '"choice_b":"笔尖在脑里反插上座那张脸",'
        '"content_b":"<400-600字三段，写主角脑内的复仇画面、那画面清晰到真实、然后看见旧友被打得发抖的肩膀的瞬间>",'
        '"bridge":"<80-150字，他想起昨夜旧友替他挨的一刀——那刀疤现在就在他能看见的位置——他终于把笔捡了起来>",'
        '"effects_b":{"humanity":+6,"obsession":-4}}\n'
        "解读：A 路顺流（人性磨损，执念加深），B 路逆流被压（人性反而被点燃，执念退潮），同维度正负对比清晰，与正文情节一一对应。\n"
        + ("\n" + lang_instr if lang_instr else "")
    )

    # ───────────── effects 校验工具 ─────────────
    valid_keys = {v["key"] for v in (variable_defs or []) if isinstance(v, dict) and v.get("key")}

    def _validate_effects(effects: dict) -> bool:
        if not isinstance(effects, dict) or not effects:
            return False
        used_keys = [k for k in effects.keys() if k in valid_keys]
        if not used_keys:
            return False
        try:
            return any(abs(int(effects[k])) >= 3 for k in used_keys)
        except (TypeError, ValueError):
            return False

    fallback_key = variable_defs[0]["key"] if variable_defs else "courage"
    fallback_cap = per_branch_caps.get(fallback_key, 6) if variable_defs else 6

    def _dominant_axis(effects: dict) -> tuple[str, str]:
        if not isinstance(effects, dict) or not effects:
            return fallback_key, "positive"
        key, value = max(effects.items(), key=lambda item: abs(_safe_int(item[1])))
        return str(key), "negative" if _safe_int(value) < 0 else "positive"

    def _distinct_pair(choice_a: str, effects_a: dict, choice_b: str, effects_b: dict) -> tuple[str, str]:
        key_a, polarity_a = _dominant_axis(effects_a)
        clean_a = ensure_distinct_choice_text(
            choice_a, choice_text_history, variable_key=key_a, polarity=polarity_a,
            branch_type=("default" if flavor_mode else ("romance" if is_romance_oriented else branch_type)),
        )
        remember_choice_text(choice_text_history, clean_a)
        key_b, polarity_b = _dominant_axis(effects_b)
        clean_b = ensure_distinct_choice_text(
            choice_b, choice_text_history, variable_key=key_b, polarity=polarity_b,
            branch_type=("default" if flavor_mode else ("romance" if is_romance_oriented else branch_type)),
        )
        remember_choice_text(choice_text_history, clean_b)
        return clean_a, clean_b

    def _required_key_for_gap(gap_idx: int, scene_text: str = "") -> str | None:
        if not required_effect_keys or gap_idx >= len(required_effect_keys):
            return None
        candidate = str(required_effect_keys[gap_idx])
        if candidate not in per_branch_caps:
            return None
        if not is_romance_oriented:
            return candidate
        available = _romance_keys_present_in_text(scene_text, romance_name_to_key)
        if not available:
            return None
        if candidate in available:
            return candidate
        _increment_soft_branch_metric(metrics, "rescheduled_effect_key")
        return available[0]

    def _has_required_positive(res: dict | None, required_key: str | None) -> bool:
        if not required_key:
            return True
        if not isinstance(res, dict):
            return False
        return max(
            _safe_int((res.get("effects_a") or {}).get(required_key, 0)),
            _safe_int((res.get("effects_b") or {}).get(required_key, 0)),
        ) > 0

    def _validate_candidate(
        candidate: dict | None,
        required_key: str | None,
        *,
        flavor_mode: bool = False,
    ) -> tuple[dict | None, bool, str, list[str], bool]:
        if not isinstance(candidate, dict):
            return None, False, "LLM 返回结果不是 JSON 对象", [
                "choice_a", "content_a", "effects_a", "choice_b",
                "content_b", "bridge", "effects_b",
            ], False

        original = dict(candidate)
        normalized, prose_ok, prose_reason = normalize_soft_branch_prose_fields(candidate)
        if not prose_ok:
            return normalized, False, prose_reason, _repair_fields_for_issue(prose_reason), False

        normalized_changed = normalized != original
        if narrative_pov_contract:
            pov_issues: list[str] = []
            for field in ("content_a", "content_b", "bridge"):
                field_text = str(normalized.get(field) or "").strip()
                if not field_text:
                    continue
                detected = detect_narrative_pov_drift(
                    field_text,
                    narrative_pov_contract,
                )
                if detected:
                    pov_issues.append(f"{field}: {'；'.join(detected[:2])}")
            if pov_issues:
                reason = "叙事视角漂移：" + "；".join(pov_issues)
                return (
                    normalized,
                    False,
                    reason,
                    _repair_fields_for_issue(reason),
                    normalized_changed,
                )
        if is_romance_oriented and not flavor_mode:
            normalized, mirror_changed = _normalize_romance_mirror_effects(
                normalized,
                romance_name_to_key,
                per_branch_caps,
            )
            normalized_changed = normalized_changed or mirror_changed
            # Deterministic title/effect mismatches are safe to repair locally;
            # do this before semantic validation so the same candidate is not
            # needlessly sent through three LLM attempts.
            before_direction = json.dumps(
                {
                    "effects_a": normalized.get("effects_a"),
                    "effects_b": normalized.get("effects_b"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            normalized = _fix_romance_effect_direction(
                normalized,
                romance_name_to_key,
            )
            after_direction = json.dumps(
                {
                    "effects_a": normalized.get("effects_a"),
                    "effects_b": normalized.get("effects_b"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            normalized_changed = normalized_changed or before_direction != after_direction
            pov_issue = _romance_pov_drift_reason(normalized, protagonist_names)
            if pov_issue:
                return normalized, False, pov_issue, _repair_fields_for_issue(pov_issue), normalized_changed
            direction_issue = _romance_effect_direction_issue(
                normalized,
                romance_name_to_key,
            )
            if direction_issue:
                return (
                    normalized,
                    False,
                    direction_issue,
                    _repair_fields_for_issue(direction_issue),
                    normalized_changed,
                )

        if variable_defs and not flavor_mode:
            if is_romance_oriented:
                clean_a, clean_b, effect_issues = sanitize_romance_effect_pair(
                    normalized.get("effects_a"),
                    normalized.get("effects_b"),
                    variable_defs,
                    per_branch_caps,
                    fallback_key=gap_fallback_key,
                    required_positive_key=required_key,
                )
            else:
                clean_a, clean_b, effect_issues = sanitize_effect_pair(
                    normalized.get("effects_a"),
                    normalized.get("effects_b"),
                    variable_defs,
                    per_branch_caps,
                    fallback_key=gap_fallback_key,
                    required_positive_key=required_key,
                    allow_romance_mirror=False,
                )
            fatal_markers = (
                "不是对象",
                "缺少有效",
                "假选择",
                "主导变量不一致",
                "必须是相反方向",
            )
            fatal_issues = [
                issue
                for issue in effect_issues
                if any(marker in issue for marker in fatal_markers)
            ]
            if fatal_issues:
                reason = "；".join(fatal_issues)
                return normalized, False, reason, ["effects_a", "effects_b"], normalized_changed
            if (
                clean_a != normalized.get("effects_a")
                or clean_b != normalized.get("effects_b")
                or effect_issues
            ):
                normalized_changed = True
            normalized["effects_a"] = clean_a
            normalized["effects_b"] = clean_b

        if flavor_mode:
            missing = [
                field
                for field in ("choice_a", "content_a", "choice_b", "content_b")
                if not str(normalized.get(field) or "").strip()
            ]
            if missing:
                reason = "普通趣味分支缺少字段：" + ", ".join(missing)
                return normalized, False, reason, missing, normalized_changed
            return normalized, True, "", [], normalized_changed
        if not (
            _validate_effects(normalized.get("effects_a"))
            and _validate_effects(normalized.get("effects_b"))
            and _has_required_positive(normalized, required_key)
        ):
            reason = "effects 为空、幅度不足或未覆盖指定变量"
            return normalized, False, reason, ["effects_a", "effects_b"], normalized_changed
        return normalized, True, "", [], normalized_changed

    # ── modular 调查选项路径（探案/推理/盗墓/种田）────────────────────
    if branch_type == "modular":
        protagonist_names = _protagonist_names_from_worldview(worldview)
        protagonist_name = protagonist_names[0] if protagonist_names else "原著主角"
        modular_anchor_parts = []
        if world_rules:
            modular_anchor_parts.append(
                f"【世界观规则（不得补造尚未出现的事实）】\n{str(world_rules)[:2500]}"
            )
        if char_cards:
            modular_anchor_parts.append(f"【核心人物卡】\n{str(char_cards)[:4000]}")
        if prev_summary:
            modular_anchor_parts.append(f"【上一章已公开事实】\n{prev_summary}")
        modular_anchor_block = "\n\n".join(modular_anchor_parts)
        if modular_anchor_block:
            modular_anchor_block += "\n\n"
        modular_system = (
            "你是互动小说编剧，负责在剧情切分点设计「调查选项」（Investigation Choices）。\n"
            + narrative_pov_block
            +
            "调查选项设计原则：\n"
            "1. A/B 两路都是玩家主动选择调查方向——不是内心挣扎，而是行动路径的分叉。\n"
            "2. 两路都是有效的调查结果，玩家在一局只能选一路，因此每路都要有独立的发现/线索/信息价值。\n"
            "3. 内容聚焦于：玩家去了哪里/问了谁/找到了什么——写出具体的发现，让玩家觉得自己做了真正的选择。\n"
            "4. 不需要 Bridge（两路都是完整路径，不存在冲动被压垮的概念）。\n"
            "5. 严格遵守角色卡设定，不创造新角色，不违背世界规则。\n"
             f"6. 玩家第一人称始终固定为主角{protagonist_name}；玩家第一人称交互身份始终固定为主角{protagonist_name}；{_modular_content_pov_instruction}"
             f"若契约为第一人称，‘我’只能指{protagonist_name}，不得切换成配角。\n"
            "7. 不得提前揭露接续点之后才首次公开的核心谜底、机关原理、凶手身份或隐藏动机；"
            "只能使用前置剧情已经公开的事实，并生成旁支线索、验证动作或无剧透信息。\n"
            "8. 每路必须输出 clue_flags，标明线索来源。source_type 只能是 source_fact、"
            "safe_observation、decisive_clue；decisive_clue 必须逐字引用前置剧情 source_excerpt，"
            "不得把接续点或未来章节里的证据伪装成已知线索。"
            + ("\n" + lang_instr if lang_instr else "")
        )
        modular_result_parts = [scenes[0]]
        flavor_branch_generated = False
        allow_offscreen_flavor = bool(
            isinstance(narrative_pov_contract, dict)
            and str(
                narrative_pov_contract.get("perspective")
                or narrative_pov_contract.get("mode")
                or ""
            ).strip()
            not in {"", "unknown"}
        )
        for gap_idx in range(len(scenes) - 1):
            if _append_checkpointed_gap(modular_result_parts, gap_idx):
                continue
            required_key = _required_key_for_gap(gap_idx)
            gap_fallback_key = required_key or fallback_key
            gap_fallback_cap = per_branch_caps.get(gap_fallback_key, fallback_cap)
            upper = scenes[gap_idx]
            lower = scenes[gap_idx + 1]
            lower_head = lower.strip().split('\n\n')[0]
            lower_presence_window = _scene_presence_prefix(lower)
            downstream_action_list = _action_phrases(lower_head)
            downstream_guard = (
                "## 下游禁止提前完成清单\n"
                + "\n".join(f"- {action}" for action in downstream_action_list[:8])
                + "\n以上动作、证据取得与人物离场只能由接续点原著完成；A/B 路写到可进入该动作之前。\n\n"
                if downstream_action_list
                else ""
            )
            flavor_mode = False
            presence_status = "active"
            presence_reason = ""
            if protagonist_names:
                presence_status, presence_reason = _modular_protagonist_presence_status(
                    upper=upper,
                    lower=lower,
                    protagonist_names=protagonist_names,
                    worldview=worldview,
                    chapter_text=chapter_text,
                    semantic_client=client,
                    metrics=metrics,
                )
                if presence_status in {
                    "active", "mentioned", "memory", "offscreen", "unknown"
                }:
                    _increment_soft_branch_metric(
                        metrics, f"presence_{presence_status}"
                    )
                if presence_status != "active":
                    # An offscreen/delayed POV must not be forced into a full
                    # investigation action. Generate at most one short,
                    # non-state-changing flavor branch per chapter when the
                    # gap contains a concrete observable beat.
                    flavor_mode = (
                        allow_offscreen_flavor
                        and not flavor_branch_generated
                        and _scene_has_flavor_cut(upper, lower_presence_window)
                    )
                    if presence_status == "delayed":
                        _increment_soft_branch_metric(metrics, "delayed_presence_recovered")
                    if not flavor_mode:
                        _increment_soft_branch_metric(metrics, "protagonist_presence_skipped")
                        modular_result_parts.append(lower)
                        if presence_status == "delayed":
                            skip_reason = (
                                f"主角{protagonist_name}仅在下游较后位置出现，"
                                "本段没有安全趣味分支位"
                            )
                        elif flavor_branch_generated:
                            skip_reason = (
                                f"主角{protagonist_name}未出现在相邻场景，"
                                "本章趣味分支配额已用完"
                            )
                        else:
                            skip_reason = f"主角{protagonist_name}未出现在相邻场景"
                        print(f"   ℹ️  第{chapter_idx+1}章第{gap_idx+1}个调查选项跳过：{skip_reason}")
                        _persist_gap(gap_idx, omitted=True)
                        continue
                    print(
                        f"   🎈 第{chapter_idx+1}章第{gap_idx+1}个调查选项降级为趣味分支："
                        f"主角{protagonist_name}{'延后出场' if presence_status == 'delayed' else '不在场'}"
                    )
            _increment_soft_branch_metric(metrics, "eligible_gap_count")
            _increment_soft_branch_metric(metrics, "total")
            if flavor_mode:
                modular_system_for_gap = (
                    "你是互动小说编剧，负责设计不改变主线的场景观察型趣味分支。\n"
                    + narrative_pov_block
                    + "当前主角不在切分点附近，禁止强行让主角执行调查、移动地点或与新人物互动。"
                    "两路只能观察、记录或回想前置正文已经出现的细节；不新增事实、不新增线索、"
                    "不改变人物状态，不提前完成下游动作。只输出严格 JSON。"
                )
                modular_user = (
                    f"{narrative_pov_block}"
                    f"{modular_anchor_block}"
                    "## 当前可见场景\n"
                    f"{upper}\n\n"
                    "## 下游接续点（不得提前执行）\n"
                    f"> {lower_head}\n\n"
                    "## 任务\n"
                    "生成一组场景观察型趣味分支，不要把阿木写进当前场景，也不要把玩家变成其他角色。\n"
                    "- choice_a / choice_b：4-15 字，分别是不同的观察、记录或回想动作；\n"
                    "- content_a / content_b：每路 80-180 字，只使用当前可见场景已有细节；\n"
                    "- effects_a / effects_b：必须为 {}；clue_flags_a / clue_flags_b：必须为 {}；\n"
                    "- 不得出现‘玩家不在场’等元叙述，不得新增人名、地点、时间、证据或关键动作；\n"
                    "- 两路结束后都直接回到接续点之前。\n"
                    '只输出 JSON：{"choice_a":"观察选项A","content_a":"趣味正文A",'
                    '"effects_a":{},"clue_flags_a":{},"choice_b":"观察选项B",'
                    '"content_b":"趣味正文B","effects_b":{},"clue_flags_b":{}}'
                )
            else:
                modular_system_for_gap = modular_system
                modular_user = (
                f"{narrative_pov_block}"
                f"{modular_anchor_block}"
                f"{var_hint}"
                f"## 前置剧情（玩家当前所在场景，理解情境）\n{upper}\n\n"
                f"## 接续点（两路选完后都回到这里）\n> {lower_head}\n\n"
                f"{downstream_guard}"
                "## 任务\n"
                "在此场景末设计一个「调查选项」，A路和B路是玩家可以选择的两个不同调查方向：\n"
                "  - choice_a：调查方向A的标题（4-12字，动词开头，如「前往X调查」「询问Y关于Z」）\n"
                 f"  - content_a：选A后玩家发现/得到的内容（200-400字，{_modular_content_pov_instruction}，具体的线索/信息/场景细节）\n"
                "  - choice_b：调查方向B的标题（4-12字，动词开头，与A路不同的方向）\n"
                 f"  - content_b：选B后玩家发现/得到的内容（200-400字，{_modular_content_pov_instruction}，具体的线索/信息/场景细节）\n"
                "  - effects_a / effects_b：若有变量表，填写调查结果对变量的影响；若无，填空对象 {}。\n"
                "  - clue_flags_a / clue_flags_b：对象；每条含 source_type、source_excerpt、category。"
                "没有新增线索时填 {}。\n"
                "两路都必须在内容结束时，让玩家处于能自然进入【接续点】的状态。\n\n"
                "输出严格 JSON：\n"
                '{"choice_a": "调查方向A（4-12字）", "content_a": "发现内容A（200-400字）",'
                ' "effects_a": {},'
                ' "clue_flags_a": {},'
                ' "choice_b": "调查方向B（4-12字）", "content_b": "发现内容B（200-400字）",'
                ' "effects_b": {}, "clue_flags_b": {}}'
                 )
            transition_repaired = False
            modular_validation_warnings: list[str] = []
            try:
                mod_res = client.chat_completion_json(
                    system_prompt=modular_system_for_gap,
                    user_prompt=modular_user,
                    temperature=0.7,
                    max_tokens=4000,
                )
                mod_res, prose_ok, prose_reason = normalize_soft_branch_prose_fields(mod_res)
                if not prose_ok:
                    if isinstance(mod_res, dict) and classify_soft_branch_validation_issue(
                        prose_reason,
                        candidate=mod_res,
                        category="prose_delimiter",
                    ) == "warning":
                        modular_validation_warnings.append(prose_reason)
                        print(
                            f"   ⚠️  第{chapter_idx+1}章第{gap_idx+1}个调查选项正文符号不完整："
                            f"{prose_reason}，保留并记录 warning"
                        )
                    else:
                        mod_res = None
                elif flavor_mode:
                    # Flavor branches are intentionally non-stateful. Ignore
                    # any accidental numeric/clue payload emitted by the LLM
                    # instead of letting an offscreen scene mutate gameplay.
                    mod_res["effects_a"] = {}
                    mod_res["effects_b"] = {}
                    mod_res["clue_flags_a"] = {}
                    mod_res["clue_flags_b"] = {}
            except Exception as e:
                print(f"   ⚠️  第{chapter_idx+1}章第{gap_idx+1}个调查选项失败（{e}），使用兜底")
                mod_res = None

            if isinstance(mod_res, dict):
                transition_issues = validate_soft_branch_transition(
                    mod_res,
                    upper_text=upper,
                    downstream_text=lower,
                )
                if transition_issues:
                    _increment_soft_branch_metric(metrics, "transition_conflicts")
                    for issue in transition_issues:
                        if issue["category"] == "downstream_action_completed":
                            _increment_soft_branch_metric(metrics, "premature_action_leaks")
                        elif issue["category"] == "decisive_clue_leaked":
                            _increment_soft_branch_metric(metrics, "decisive_clue_leaks")
                    _increment_soft_branch_metric(metrics, "transition_repairs")
                    issue_text = "；".join(issue["reason"] for issue in transition_issues)
                    repair_fields = sorted(
                        {issue["repair_field"] for issue in transition_issues}
                    )
                    print(
                        f"   🩹 第{chapter_idx+1}章第{gap_idx+1}个调查选项存在衔接冲突，"
                        f"仅定向修复一次：{issue_text}"
                    )
                    try:
                        repair = client.chat_completion_json(
                            system_prompt=(
                                "你是调查软分支衔接修复器。只修改指定字段，禁止提前完成下游动作、"
                                "改变物品位置或揭露未来证据；输出完整 JSON 对象。"
                                + narrative_pov_block
                            ),
                            user_prompt=(
                                f"{narrative_pov_block}"
                                f"## 待修复字段\n{repair_fields}\n\n"
                                f"## 冲突\n{issue_text}\n\n"
                                f"## 前置正文\n{upper}\n\n"
                                f"## 下游原著（不得提前执行或泄露）\n{lower}\n\n"
                        f"## 当前 JSON\n{json.dumps(mod_res, ensure_ascii=False)}\n\n"
                                f"{narrative_pov_block}"
                                "保持 A/B 调查价值，但只能加入不改变主线物态的安全观察或旁证。"
                            ),
                            temperature=0.2,
                            max_tokens=2200,
                        )
                        if isinstance(repair, dict):
                            repaired = {**mod_res, **repair}
                            repaired, prose_ok, _ = normalize_soft_branch_prose_fields(repaired)
                            if prose_ok:
                                remaining = validate_soft_branch_transition(
                                    repaired,
                                    upper_text=upper,
                                    downstream_text=lower,
                                )
                                if not remaining:
                                    repaired["clue_flags_a"], _ = normalize_clue_flags(
                                        repaired.get("clue_flags_a"), source_text=upper,
                                    )
                                    repaired["clue_flags_b"], _ = normalize_clue_flags(
                                        repaired.get("clue_flags_b"), source_text=upper,
                                    )
                                    mod_res = repaired
                                    transition_repaired = True
                                    _increment_soft_branch_metric(
                                        metrics, "transition_repair_success"
                                    )
                                else:
                                    fatal_remaining = [
                                        issue for issue in remaining
                                        if classify_modular_transition_issue(
                                            issue.get("category")
                                        ) == "fatal"
                                    ]
                                    if fatal_remaining:
                                        mod_res = None
                                    else:
                                        modular_validation_warnings.extend(
                                            issue["reason"] for issue in remaining
                                        )
                                        mod_res = retain_soft_branch_candidate(
                                            repaired,
                                            modular_validation_warnings,
                                        )
                                        _increment_soft_branch_metric(
                                            metrics, "validation_warning_count"
                                        )
                            else:
                                fatal_transition = [
                                    issue for issue in transition_issues
                                    if classify_modular_transition_issue(
                                        issue.get("category")
                                    ) == "fatal"
                                ]
                                if fatal_transition:
                                    mod_res = None
                                else:
                                    modular_validation_warnings.extend(
                                        issue["reason"] for issue in transition_issues
                                    )
                                    mod_res = retain_soft_branch_candidate(
                                        mod_res,
                                        modular_validation_warnings,
                                    )
                        else:
                            fatal_transition = [
                                issue for issue in transition_issues
                                if classify_modular_transition_issue(
                                    issue.get("category")
                                ) == "fatal"
                            ]
                            if fatal_transition:
                                mod_res = None
                            else:
                                modular_validation_warnings.extend(
                                    issue["reason"] for issue in transition_issues
                                )
                                mod_res = retain_soft_branch_candidate(
                                    mod_res,
                                    modular_validation_warnings,
                                )
                    except Exception as repair_error:
                        print(
                            f"   ⚠️  调查软分支衔接修复失败：{repair_error}"
                        )
                        fatal_transition = [
                            issue for issue in transition_issues
                            if classify_modular_transition_issue(
                                issue.get("category")
                            ) == "fatal"
                        ]
                        if fatal_transition:
                            mod_res = None
                        else:
                            modular_validation_warnings.extend(
                                issue["reason"] for issue in transition_issues
                            )
                            mod_res = retain_soft_branch_candidate(
                                mod_res,
                                modular_validation_warnings,
                            )
                    if mod_res is None:
                        _increment_soft_branch_metric(metrics, "transition_dropped")
                        _increment_soft_branch_metric(metrics, "deleted_branch_count")
                        print(
                            f"   🗑️  第{chapter_idx+1}章第{gap_idx+1}个调查选项修复后仍冲突，"
                            "删除该组并恢复原著接续"
                        )
                        modular_result_parts.append(lower)
                        _persist_gap(gap_idx, omitted=True)
                        continue
                else:
                    if modular_validation_warnings:
                        mod_res = retain_soft_branch_candidate(
                            mod_res,
                            modular_validation_warnings,
                        )
                        _increment_soft_branch_metric(
                            metrics, "validation_warning_count"
                        )
                    mod_res["clue_flags_a"], _ = normalize_clue_flags(
                        mod_res.get("clue_flags_a"), source_text=upper,
                    )
                    mod_res["clue_flags_b"], _ = normalize_clue_flags(
                        mod_res.get("clue_flags_b"), source_text=upper,
                    )

            if mod_res is None:
                _increment_soft_branch_metric(metrics, "validation_dropped")
                _increment_soft_branch_metric(metrics, "deleted_branch_count")
                print(
                    f"   🗑️  第{chapter_idx+1}章第{gap_idx+1}个调查选项重试后仍不合规，"
                    "删除该组并恢复原著接续"
                )
                modular_result_parts.append(lower)
                _persist_gap(gap_idx, omitted=True)
                continue
            else:
                raw_ea = mod_res.get("effects_a", {})
                raw_eb = mod_res.get("effects_b", {})
                # 无变量表时 effects 留空
                if variable_defs:
                    effects_a, effects_b, effect_issues = sanitize_effect_pair(
                        raw_ea,
                        raw_eb,
                        variable_defs,
                        per_branch_caps,
                        fallback_key=gap_fallback_key,
                        required_positive_key=required_key,
                    )
                    if effect_issues:
                        print(
                            f"   ⚠️  第{chapter_idx+1}章第{gap_idx+1}个调查选项 effects 已规范化："
                            + "；".join(effect_issues)
                        )
                else:
                    effects_a, effects_b = {}, {}
                if flavor_mode:
                    choice_a = ensure_distinct_modular_flavor_choice_text(
                        mod_res.get("choice_a", "记下眼前细节"), choice_text_history,
                    )
                else:
                    choice_a = ensure_distinct_modular_choice_text(
                        mod_res.get("choice_a", "前往现场调查"), choice_text_history,
                    )
                if choice_a != str(mod_res.get("choice_a", "前往现场调查")).strip():
                    _increment_soft_branch_metric(metrics, "button_template_rewrites")
                remember_choice_text(choice_text_history, choice_a)
                if flavor_mode:
                    choice_b = ensure_distinct_modular_flavor_choice_text(
                        mod_res.get("choice_b", "留意周围动静"), choice_text_history,
                    )
                else:
                    choice_b = ensure_distinct_modular_choice_text(
                        mod_res.get("choice_b", "先询问知情人"), choice_text_history,
                    )
                if choice_b != str(mod_res.get("choice_b", "先询问知情人")).strip():
                    _increment_soft_branch_metric(metrics, "button_template_rewrites")
                remember_choice_text(choice_text_history, choice_b)
                marker = _build_soft_branch_marker(
                    choice_a=choice_a,
                    content_a=mod_res.get("content_a", "你发现了一些线索。"),
                    effects_a=effects_a,
                    choice_b=choice_b,
                    content_b=mod_res.get("content_b", "你获得了一些信息。"),
                    effects_b=effects_b,
                    bridge="",
                    legacy_slot=chapter_idx + gap_idx,
                    clue_flags_a=mod_res.get("clue_flags_a", {}),
                    clue_flags_b=mod_res.get("clue_flags_b", {}),
                    branch_kind="flavor" if flavor_mode else "numeric",
                    validation_warnings=mod_res.get("validation_warnings", []),
                )
                if not transition_repaired:
                    _increment_soft_branch_metric(metrics, "first_pass_success")
                if flavor_mode:
                    _increment_soft_branch_metric(metrics, "flavor_branch_count")
                    flavor_branch_generated = True
                    if presence_status == "offscreen":
                        _increment_soft_branch_metric(metrics, "offscreen_flavor_generated")
                _increment_soft_branch_metric(metrics, "generated_branch_count")
                print(f"   ✅ 第{chapter_idx+1}章第{gap_idx+1}个调查选项生成完成")

            _persist_gap(gap_idx, marker)
            modular_result_parts.append(marker)
            next_scene = scenes[gap_idx + 1]
            next_head = next_scene.strip().split('\n\n')[0].strip()
            if next_head and next_head in marker:
                remainder = next_scene.strip()[len(next_head):].lstrip('\n')
                if remainder:
                    modular_result_parts.append(remainder)
            else:
                modular_result_parts.append(next_scene)
        return "\n\n".join(modular_result_parts)
    # ── modular 调查选项路径结束 ─────────────────────────────────────

    result_parts = [scenes[0]]

    for gap_idx in range(len(scenes) - 1):
        if _append_checkpointed_gap(result_parts, gap_idx):
            continue
        upper = scenes[gap_idx]
        lower = scenes[gap_idx + 1]

        # 上半场全段（不再截断）+ 下半场有限前缀。提示词仍只展示第一段，
        # 但恋爱路线在场检测扩展到下游前 3 段，避免角色稍晚出场被误判。
        upper_full = upper
        lower_head = lower.strip().split('\n\n')[0]
        # 路线角色是否在场只能依据插入点前后正文判断。章节摘要常包含
        # “各路线未明示”等全局角色清单，不能将其误当作当前场景出场证据。
        lower_presence_window = _scene_presence_prefix(lower)
        gap_scene_text = "\n".join((upper_full, lower_presence_window))
        available_romance_keys = (
            _romance_keys_present_in_text(gap_scene_text, romance_name_to_key)
            if is_romance_oriented else []
        )
        flavor_mode = bool(
            is_romance_oriented
            and not available_romance_keys
            and _scene_has_flavor_cut(upper_full, lower_presence_window)
        )
        if is_romance_oriented and not available_romance_keys and not flavor_mode:
            _increment_soft_branch_metric(metrics, "skipped_no_route_target")
            result_parts.append(lower)
            structured_log(
                logger,
                "soft_branch_skipped",
                chapter=chapter_idx + 1,
                gap=gap_idx + 1,
                branch_type=branch_type,
                reason="no_romance_route_target_in_gap",
            )
            print(
                f"   ℹ️  第{chapter_idx+1}章第{gap_idx+1}个软分支跳过："
                "当前场景没有可用恋爱路线角色"
            )
            _persist_gap(gap_idx, omitted=True)
            continue
        _increment_soft_branch_metric(metrics, "eligible_gap_count")
        romance_presence_hint = ""
        if is_romance_oriented and available_romance_keys:
            matched_route_names = [
                alias
                for key, alias in _romance_route_hits(
                    gap_scene_text, romance_name_to_key
                )
                if key in available_romance_keys
            ]
            matched_label = "、".join(dict.fromkeys(matched_route_names))
            romance_presence_hint = (
                "## 相邻正文已确认的恋爱路线角色（仅用于在场判断，不得提前续写）\n"
                f"已命中角色/称呼：{matched_label or '未命名路线角色'}\n"
                "命中范围为下游正文前 3 个自然段/约 2400 字；原文窗口不直接展示，"
                "避免把未来动作或证据泄露给分支生成。分支正文不得提前完成该窗口中的动作、"
                "揭露其中尚未公开的事实，仍须停在接续点之前。\n\n"
            )
        _increment_soft_branch_metric(metrics, "total")
        required_key = _required_key_for_gap(gap_idx, gap_scene_text)
        gap_fallback_key = required_key or (
            available_romance_keys[0] if available_romance_keys else fallback_key
        )
        gap_fallback_cap = per_branch_caps.get(gap_fallback_key, fallback_cap)

        # 提取 upper 全段中最后一句完成的关键台词/行动，生成禁止重复提示
        last_beat = _extract_last_completed_beat(upper_full)
        no_repeat_block = ""
        if last_beat:
            no_repeat_block = (
                f"## ⚠️ 已完成的关键台词/动作（选项中绝对不得重复或改写）\n"
                f"> 「{last_beat}」\n"
                f"以上内容在主文中已完整发生。选项 A/B 必须写**这一刻之前**主角尚未表露的内心挣扎，"
                f"或这一刻**同时发生**的另一层心理活动，而非回放、重写上述台词或动作。\n\n"
            )

        # 把 anchor_block 中的 gap_idx 占位符替换为当前场段位置
        anchor_block_resolved = anchor_block.replace(
            "{gap_idx_placeholder}", f"{gap_idx + 1}/{len(scenes) - 1}"
        )

        romance_hint = (
            "本段没有恋爱路线角色，按【普通趣味分支】设计：围绕当前场景的生活细节、轻微误会、幽默或人格反应，保留玩家可见的 A/B 小选择。此类分支不改变恋爱数值，effects_a/effects_b 必须都是 {}，不要点名恋爱角色，也不要伪造好感因果。\n"
            if flavor_mode else
            "本作 is_romance_oriented=true，请按【双模板：恋爱向】部分设计 effects（同时改 2 条 affection 轴）。\n"
            if is_romance_oriented else
            "本作 is_romance_oriented=false，按【双模板：人格向】设计——同一人格维度轴的两极对比。\n"
        )
        required_effect_hint = (
            f"【本软分支覆盖任务】本次必须让变量 {required_key} 在 A/B 至少一路获得正向加分，"
            "且选项标题与正文必须明确体现该变量对应的角色或人格倾向。\n"
            if required_key and not flavor_mode else ""
        )
        flavor_contract_hint = (
            "【普通趣味分支合同】choice_a/choice_b 各为 4-15 字；content_a/content_b 为 200-400 字；"
            "effects_a/effects_b 必须为 {}；branch_kind=flavor。两路只改变当下的轻微体验或心理，不改变主线事实。\n\n"
            if flavor_mode else ""
        )

        if flavor_mode:
            base_user_prompt = (
                f"{narrative_pov_block}"
                f"{protagonist_identity_rule}"
                f"{anchor_block_resolved}"
                f"{flavor_contract_hint}"
                f"## 前置剧情（当前可用事实）\n{upper_full}\n\n"
                f"{no_repeat_block}"
                f"## 接续点（A/B 两路都必须自然回到这里）\n> {lower_head}\n\n"
                "## 任务\n"
                "当前场景没有恋爱路线角色。请从生活细节、轻微误会、幽默反应、临场习惯或人物性格中，"
                "设计一组不改变主线事实的普通趣味分支。\n"
                "- choice_a / choice_b：各 4-15 字，玩家能看懂具体选择，语义不得重复。\n"
                "- content_a / content_b：各 200-400 字，只改变当下体验或心理，不新增关键证据、关系结论、地点或时间线。\n"
                "- 不得虚构或点名当前场景未出现的恋爱路线角色。\n"
                "- 两路结尾都停在接续点动作发生前；bridge 可用于简短收束，也可为空。\n"
                "- effects_a / effects_b 必须严格为 {}，不得输出任何 affection 或人格数值。\n"
                "只输出严格 JSON：\n"
                '{"choice_a":"4-15字选项A","content_a":"趣味分支A正文",'
                '"effects_a":{},"choice_b":"4-15字选项B","content_b":"趣味分支B正文",'
                '"bridge":"可选的简短收束","effects_b":{}}'
                + (f"\n{lang_instr}" if lang_instr else "")
            )
        else:
            base_user_prompt = (
                f"{narrative_pov_block}"
                f"{var_hint}"
                f"{required_effect_hint}"
                f"{romance_lookup_text}"
                f"{anchor_block_resolved}"
                f"## 模板选择\n{romance_hint}\n"
                f"## 前置剧情（上半场全文，理解氛围，勿直接续写）\n{upper_full}\n\n"
                f"{no_repeat_block}"
                f"{romance_presence_hint}"
                f"## 【接续点】下半场开头第一段（A/B两路的物理终点，不可改变）\n> {lower_head}\n\n"
                "## 你的任务：严格按以下五步执行\n\n"
                "**第一步：维度诊断（内部推理，不输出）**\n"
                "阅读【全局数值变量表】和【前置剧情】+ 本章/上一章摘要 + 全书章节摘要，判断：此刻哪 1-2 个维度的内在张力最强烈？\n"
                "这个维度在这段剧情里，主角是被迫压抑它，还是被迫激活它？\n"
                "该维度积累到一定程度会触发哪个 whatif/ending 节点？effects 必须服务于把玩家推向这个目标。\n\n"
                "**第二步：核查接续点（内部推理，不输出）**\n"
                "仔细读【接续点】第一段：主角做了什么动作？说了什么话？语气是什么？\n"
                "这是 A/B 两路都必须无缝收束到的物理现实，任何违背它的写法都是错误。\n\n"
                "**第三步：A 路正文（400-600 字，分 3 段）**\n"
                "写主角带着「维度顺流」的情绪走向接续点的内心过程：\n"
                "  - 第 1 段：此刻压倒他的是什么感受（麻木/宿命感/苍凉的平静）\n"
                "  - 第 2 段：他内心是否有过一丝动摇？那丝动摇是怎么被他自己压下去的？\n"
                "  - 第 3 段：他最终带着什么样的表情/心境，走向接续点的那一刻——写到他「即将开口/即将动手」的状态为止，绝对禁止把接续点里的台词或动作写进来。\n\n"
                "**第四步：B 路正文（400-600 字，分 3 段）+ Bridge（80-150 字）**\n"
                "写主角在同一个维度上涌现出「逆流」冲动的内心过程：\n"
                "  - 第 1 段：那股冲动是怎么升起来的？它的根源是什么（具体的记忆/仇恨/渴望，必须与维度强相关）\n"
                "  - 第 2 段：冲动的顶点——他的内心/微动作/幻想画面达到了最极端的状态（充分燃烧，不要提前收场）\n"
                "  - 第 3 段：冲动开始动摇，他感到某种沉重的东西在拉扯他（为 Bridge 铺垫）\n"
                "  - Bridge：写清楚那个具体的「重量」是什么，它是怎么把冲动生生掐灭的。严禁外部打断！必须是内心压垮！\n\n"
                "**第五步：effects 叙事因果（必须填，不准为空）**\n"
                "A 路 effects：主角顺流，主导维度 +cap_main，可附次要维度小数值\n"
                "B 路 effects：主角逆流被压，主导维度 -cap_main，可附次要维度反向数值\n"
                "两路必须围绕同一主导维度做正负对比。effects 数值必须在正文里有对应的行为或心理描写作为依据。\n\n"
                "输出严格 JSON（禁止输出任何 JSON 以外的内容）：\n"
                '{"choice_a": "A 路选项（严格 4-15 字，第一人称短句，体现维度顺流）",'
                ' "content_a": "A 路正文（\\n\\n 分 3 段，400-600 字，末段写到主角即将开口/即将动手的状态为止）",'
                ' "effects_a": {"变量表 key": 数值, ...},'
                ' "choice_b": "B 路选项（严格 4-15 字，第一人称短句，体现维度逆流）",'
                ' "content_b": "B 路正文（\\n\\n 分 3 段，400-600 字，末段为冲动动摇）",'
                ' "bridge": "心理坍塌段落（\\n\\n 与前文隔开，80-150 字，写清楚具体的压垮原因）",'
                ' "effects_b": {"变量表 key": 数值, ...}}'
            )

        # 首次完整生成；后两次只修复失败字段。只有完全没有可用 JSON 时才重新完整生成。
        res = None
        candidate = None
        last_validation_issue = ""
        last_repair_fields: list[str] = []
        validation_warning_history: list[str] = []
        for attempt_index in range(3):
            attempt = attempt_index + 1
            full_generation = attempt == 1 or not isinstance(candidate, dict)
            if full_generation:
                current_prompt = base_user_prompt
                current_system = (
                    "你是视觉小说的普通趣味分支编剧。只设计不改变主线事实、不改变任何数值的 A/B 小选择；"
                    "不得虚构当前场景未出现的恋爱路线角色；严格只输出 JSON。\n"
                    + narrative_pov_block
                    + protagonist_identity_rule
                    if flavor_mode
                    else system_prompt
                )
                temperature = 0.45
                max_tokens = 10000
            else:
                current_prompt = _build_targeted_repair_prompt(
                    candidate,
                    reason=last_validation_issue,
                    fields=last_repair_fields,
                    variable_hint=(
                        flavor_contract_hint
                        if flavor_mode
                        else var_hint
                        + required_effect_hint
                        + romance_lookup_text
                        + romance_presence_hint
                    ),
                    scene_context=(
                        f"前置场景：{upper_full}\n接续点：{lower_head}\n"
                        f"本段类型：{'普通趣味分支' if flavor_mode else '恋爱数值分支'}"
                    ),
                    narrative_pov_block=narrative_pov_block,
                )
                current_system = (
                    "你是视觉小说结构化字段修复器。严格保留未要求修改的字段，只输出 JSON 补丁。"
                    + narrative_pov_block
                )
                temperature = 0.2
                max_tokens = 3000
            try:
                generated = client.chat_completion_json(
                    system_prompt=current_system,
                    user_prompt=current_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if full_generation:
                    candidate = generated
                elif isinstance(generated, dict):
                    candidate = {**candidate, **generated}
                else:
                    candidate = generated

                _increment_soft_branch_metric(metrics, "validation_attempted")
                validated, valid, issue, repair_fields, normalized_changed = _validate_candidate(
                    candidate, required_key, flavor_mode=flavor_mode
                )
                if isinstance(validated, dict):
                    candidate = validated
                if valid:
                    res = validated
                    if attempt == 1:
                        _increment_soft_branch_metric(
                            metrics,
                            "normalized_success" if normalized_changed else "first_pass_success",
                        )
                    elif full_generation:
                        _increment_soft_branch_metric(metrics, "full_retry_success")
                    else:
                        _increment_soft_branch_metric(metrics, "targeted_repair_success")
                    break

                last_validation_issue = issue
                last_repair_fields = repair_fields
                if issue and issue not in validation_warning_history:
                    validation_warning_history.append(issue)
                _record_validation_failure(
                    metrics,
                    chapter_idx=chapter_idx,
                    gap_idx=gap_idx,
                    attempt=attempt,
                    branch_type=branch_type,
                    category=_validation_category(issue),
                    reason=issue,
                    repair_fields=repair_fields,
                )
            except Exception as e:
                _increment_soft_branch_metric(metrics, "generation_exceptions")
                last_validation_issue = str(e) or type(e).__name__
                last_repair_fields = []
                if full_generation:
                    candidate = None
                structured_log(
                    logger,
                    "soft_branch_generation_exception",
                    level="warning",
                    chapter=chapter_idx + 1,
                    gap=gap_idx + 1,
                    attempt=attempt,
                    branch_type=branch_type,
                    error_type=type(e).__name__,
                )
                print(
                    f"   ⚠️  第{chapter_idx+1}章第{gap_idx+1}个软分支生成异常"
                    f"（attempt={attempt}）：{e}"
                )

        if res is None:
            # The normal three attempts are followed by one deliberately
            # simplified request.  This is still scoped to the current
            # optional branch: it never re-runs the chapter, terminal-state
            # scan, or any other node.  If the simple candidate is invalid,
            # the branch is omitted and the original scene continues.
            _increment_soft_branch_metric(metrics, "simple_llm_fallback")
            print(
                f"   ⚠️  第{chapter_idx+1}章第{gap_idx+1}个软分支三次失败，"
                "调用质朴版 LLM 兜底..."
            )
            simple_prompt = (
                f"{narrative_pov_block}"
                f"{var_hint}"
                f"{required_effect_hint}"
                f"{protagonist_identity_rule}"
                f"{romance_lookup_text}"
                f"{romance_presence_hint}"
                f"{flavor_contract_hint}"
                f"{anchor_block_resolved}"
                f"## 前置剧情（上半场全文）\n{upper_full}\n\n"
                f"## 接续点\n> {lower_head}\n\n"
                "请为此场景生成一对简单、可直接衔接的软分支选项（A/B）。\n"
                "只保留必要内容，不追求复杂修辞，不新增主线事实。\n"
                "- choice_a / choice_b：4-15 字的第一人称短句，且彼此不同；\n"
                "- content_a / content_b：350-500 字，保持叙事视角一致，写到接续点之前；\n"
                "- bridge：60-100 字，写清 B 路的心理收束；\n"
                + (
                    "- effects_a / effects_b：必须都是 {}，不得改变数值。\n"
                    if flavor_mode
                    else "- effects_a / effects_b：围绕同一主导变量做正负对比，至少一项绝对值 ≥3。\n"
                )
                + "只输出严格 JSON，不要输出解释。"
            )
            try:
                fallback_candidate = client.chat_completion_json(
                    system_prompt=(
                        "你是视觉小说软分支修复编剧。只输出严格 JSON，"
                        "优先保证字段完整、叙事视角一致、主线事实不变和可衔接。"
                        + ("本次是普通趣味分支，effects 必须为空对象 {}。" if flavor_mode else "")
                    ),
                    user_prompt=simple_prompt,
                    temperature=0.25,
                    max_tokens=6000,
                )
                _increment_soft_branch_metric(metrics, "validation_attempted")
                fallback_candidate, fallback_valid, fallback_issue, _, _ = _validate_candidate(
                    fallback_candidate,
                    required_key,
                    flavor_mode=flavor_mode,
                )
                if fallback_valid:
                    res = fallback_candidate
                else:
                    if fallback_issue and fallback_issue not in validation_warning_history:
                        validation_warning_history.append(fallback_issue)
                    if classify_soft_branch_validation_issue(
                        fallback_issue,
                        candidate=fallback_candidate,
                        category=_validation_category(fallback_issue),
                    ) == "warning":
                        res = retain_soft_branch_candidate(
                            fallback_candidate,
                            validation_warning_history,
                        )
                        _increment_soft_branch_metric(metrics, "validation_warning_count")
                        print(
                            f"   ⚠️  质朴版 LLM 仍有校验问题（{fallback_issue}），"
                            "已保留当前软分支并记录 warning"
                        )
                    else:
                        print(
                            f"   ⚠️  质朴版 LLM 缺少可运行分支结构（{fallback_issue}），"
                            "省略当前软分支"
                        )
                        res = None
            except Exception as e:
                if classify_soft_branch_validation_issue(
                    last_validation_issue,
                    candidate=candidate,
                    category=_validation_category(last_validation_issue),
                ) == "warning":
                    res = retain_soft_branch_candidate(
                        candidate,
                        validation_warning_history or [last_validation_issue],
                    )
                    _increment_soft_branch_metric(metrics, "validation_warning_count")
                    print(
                        f"   ⚠️  质朴版 LLM 调用失败（{e}），"
                        "保留上一版可运行软分支并记录 warning"
                    )
                else:
                    print(f"   ⚠️  质朴版 LLM 调用失败（{e}），省略当前软分支")
                    res = None

        if res is None:
            _increment_soft_branch_metric(metrics, "validation_dropped")
            _increment_soft_branch_metric(metrics, "deleted_branch_count")
            print(
                f"   🗑️  第{chapter_idx+1}章第{gap_idx+1}个软分支重试后仍不合规，"
                "删除该组并恢复原著接续"
            )
            result_parts.append(scenes[gap_idx + 1])
            _persist_gap(gap_idx, omitted=True)
            continue
        else:
            # Romance direction was repaired before validation; keep this final
            # call as an idempotent guard for fallback-generated candidates.
            res = _fix_romance_effect_direction(res, romance_name_to_key)
            # 最终强校验：过滤非法 key、截断越界值，并消除假选择。
            if flavor_mode:
                effects_a, effects_b, effect_issues = {}, {}, []
            elif is_romance_oriented:
                effects_a, effects_b, effect_issues = sanitize_romance_effect_pair(
                    res.get("effects_a"),
                    res.get("effects_b"),
                    variable_defs,
                    per_branch_caps,
                    fallback_key=gap_fallback_key,
                    required_positive_key=required_key,
                )
            else:
                effects_a, effects_b, effect_issues = sanitize_effect_pair(
                    res.get("effects_a"),
                    res.get("effects_b"),
                    variable_defs,
                    per_branch_caps,
                    fallback_key=gap_fallback_key,
                    required_positive_key=required_key,
                )
            if effect_issues:
                print(
                    f"   ⚠️  第{chapter_idx+1}章第{gap_idx+1}个软分支 effects 已规范化："
                    + "；".join(effect_issues)
                )
            # 去重兜底：若 LLM 把接续点首句写进了 content_a 末尾，截掉
            content_a = res.get("content_a", "你决定主动说出心中所想。")
            if lower_head and lower_head.strip() in content_a:
                cut = content_a.find(lower_head.strip())
                content_a = content_a[:cut].rstrip()
            choice_a, choice_b = _distinct_pair(
                res.get("choice_a", "主动开口"), effects_a,
                res.get("choice_b", "保持沉默"), effects_b,
            )
            marker = _build_soft_branch_marker(
                choice_a=choice_a,
                content_a=content_a,
                effects_a=effects_a,
                choice_b=choice_b,
                content_b=res.get("content_b", "你选择沉默，将一切藏在心底。"),
                effects_b=effects_b,
                bridge=res.get("bridge", ""),
                legacy_slot=chapter_idx + gap_idx,
                clue_flags_a=res.get("clue_flags_a", {}) if isinstance(res.get("clue_flags_a", {}), dict) else {},
                clue_flags_b=res.get("clue_flags_b", {}) if isinstance(res.get("clue_flags_b", {}), dict) else {},
                branch_kind="flavor" if flavor_mode else "numeric",
                validation_warnings=res.get("validation_warnings", []),
            )
            if flavor_mode:
                _increment_soft_branch_metric(metrics, "flavor_branch_count")
            _increment_soft_branch_metric(metrics, "generated_branch_count")
            print(f"   ✅ 第{chapter_idx+1}章第{gap_idx+1}个软分支剧场生成完成")

        _persist_gap(gap_idx, marker)
        result_parts.append(marker)
        # 去重兜底：若 LLM 把下半场第一段抄进了 bridge，跳过重复首段
        next_scene = scenes[gap_idx + 1]
        next_head = next_scene.strip().split('\n\n')[0].strip()
        if next_head and next_head in marker:
            remainder = next_scene.strip()[len(next_head):].lstrip('\n')
            if remainder:
                result_parts.append(remainder)
        else:
            result_parts.append(next_scene)

    return "\n\n".join(result_parts)
