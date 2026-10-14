"""角色图片提示词安全修复。

只在图片服务明确返回 ``moderation_blocked`` 后使用。模块负责：

- 让 LLM 对整段正向提示词做更健康、中性的重述；
- 锁定年龄字段，不允许改写；
- 校验角色身份、客观外貌、服装配饰、画风构图和气质方向未丢失；
- 可选地把修复后的角色 ``positive_prompt`` 精确写回 ``character_prompts``；
- 为文生图和图生图提供统一的“修复后立即重试”执行入口。

``negative_prompt`` 不会传给修复模型，也不会在数据库更新中被修改。
"""

from __future__ import annotations

import copy
import difflib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from sqlalchemy import text

from common.platform_db import platform_engine


logger = logging.getLogger(__name__)
T = TypeVar("T")


class PromptRepairError(RuntimeError):
    """提示词无法安全修复或无法写回时抛出。"""


class PromptRepairLimitReached(PromptRepairError):
    """同一提示词已达到自动修复次数上限。"""


@dataclass(frozen=True)
class PromptPersistenceContext:
    """需要把修复结果写回角色提示词表时提供的定位信息。"""

    task_id: str
    speaker_id: str
    combination: str
    immutable_prefix: str = ""


@dataclass
class PromptRepairResult:
    request_prompt: str
    stored_prompt: str
    repair_count: int = 0
    persisted: bool = False
    change_summary: List[str] = field(default_factory=list)


_AGE_FIELD_RE = re.compile(
    r"main subject of the image is a\s+[^,]+,\s*"
    r"(?:male|female|character),\s*([^,]+),",
    re.IGNORECASE,
)


# 来自 docs/慕声文生图提示词修改记录.md。年龄字段不在这里处理，始终由程序锁定。
# 这些是“允许且应当修改”的主观风险表达，不属于必须逐字保留的客观角色特征。
_KNOWN_RISK_GUIDANCE = (
    (
        "乖巧无害",
        "保持温和方向，改写为内敛沉静、安静克制等健康表达，不要改变为强势或冷酷",
    ),
    (
        "病娇",
        "保持阴郁、偏执方向，改写为偏执深沉等中性表达，并保留原有阴郁冷酷气质",
    ),
    (
        "介于少年和少女之间的青春昳丽",
        "必须删除少年/少女/青春之间的对照；保留清秀、柔和、精致、俊俏的审美方向，用不涉及年龄和性别对照的表达重述",
    ),
    (
        "androgynous quality between boy and girl",
        "删除这段直译，用不含 boy、girl、androgynous 的清秀柔和审美表达替代",
    ),
    (
        "fully clothed, modest attire",
        "删除额外审核声明；原有服装和配饰描述已经足够，不能借此修改服装",
    ),
    (
        "modest and non-revealing",
        "删除额外审核声明；不要在提示词中讨论暴露程度",
    ),
    (
        "wholesome, non-sexualized, strictly SFW",
        "删除直接讨论审核或性内容的声明，改用自然、正常的角色描述",
    ),
    (
        "non-sexualized",
        "删除直接讨论性内容的审核声明",
    ),
    (
        "strictly SFW",
        "删除直接讨论审核等级的声明",
    ),
)


# 文档明确要求完整保留的慕声客观特征。仅当原提示词中实际存在时才执行逐字校验，
# 因而不会给其它角色强加任何新特征。
_KNOWN_PROTECTED_PHRASES = (
    "极黑",
    "漆黑",
    "黑润透亮",
    "黑亮深邃",
    "含水色",
    "漂亮柔和的美人尖",
    "长睫根根分明",
    "面容精致俊俏",
    "下颌线清晰",
    "下颌线条凌厉",
    "身形修长轻巧",
    "修长轻巧",
    "修长宽肩窄腰",
    "白皙如玉",
)


_FORBIDDEN_COMPLIANCE_SLOGANS = (
    "sexual",
    "sfw",
    "fully clothed",
    "modest attire",
    "non-revealing",
    "wholesome",
)


_LEGACY_TOP_LEVEL_REPAIR_KEYS = (
    "original_positive_prompt",
    "prompt_repair_count",
    "prompt_repair_policy_version",
    "prompt_repair_reason",
    "prompt_repair_stage",
    "prompt_repair_mode",
    "prompt_repair_model",
    "prompt_repaired_at",
)


_LEGACY_HISTORY_REPAIR_KEYS = (
    "mode",
    "model",
    "stage",
    "repaired_at",
    "policy_version",
)


_REWRITE_SYSTEM_PROMPT = """
你是角色图片正向提示词的安全编辑器。图片平台刚刚把一条本来用于正常角色立绘的提示词误判为不安全内容。

你的任务是重写整段 positive_prompt，使表达更绿色、健康、克制、明确，同时完整保留原提示词的信息量和角色辨识度。

必须遵守：
1. 年龄字段完全锁定，逐字保留，不得替换、删除、弱化、扩大、翻译或新增其它年龄描述。
2. 保留角色身份、性别、种族、职业、发型、发色、眼睛、五官、肤色、体型、服装、材质、配饰和其它客观特征。除明确列出的风险表达外，其它内容尽量逐字保留，不要随意润色或替换近义词。
3. 保留画风、光照、背景、构图、姿势、全身取景和单人限制。
4. 气质语义方向保持一致：温和仍然温和，阴郁仍然阴郁，冷酷仍然冷酷；只把可能误判的表达改得健康、中性。
5. 明确列出的风险表达必须被替换或删除，不能为了“保留细节”又写回原句；它们属于主观审美或气质措辞，不属于必须保留的客观特征。
6. 优先同义重述，不要为了通过审核而删掉客观细节。只有确实无法健康重述的主观敏感短语才允许删除。
7. 不得添加性感、裸露、诱惑、恋童、暴力或其它原文不存在的内容，也不得添加 sexual、non-sexualized、SFW、fully clothed、non-revealing 等审核声明。不要在正向提示词里解释为什么内容安全。
8. 如果给出了不可变前缀，输出必须仍以该前缀逐字开头。
9. 只处理 positive_prompt；不要输出或修改 negative_prompt。

返回 JSON：
{
  "positive_prompt": "完整重写后的整段正向提示词",
  "change_summary": ["简要说明修改了哪些可能误判的表达"]
}
""".strip()


_AUDIT_SYSTEM_PROMPT = """
你是严格的角色提示词一致性审核员。比较原始提示词和重写提示词，判断重写是否只做了健康化表达调整，且没有牺牲角色描述。

用户会提供“允许且应当修改的风险表达”。这些表达被替换或删除是正确修复，不得把它们报告为缺失的客观细节。你只需要检查风险表达之外的身份、客观外貌、服装配饰、画风构图和气质方向是否保留。若用户提供了“必须逐字保留的客观短语”，任一短语缺失或被近义替换都应判定失败。

返回 JSON，并且所有字段都必须严格依据文本判断：
{
  "age_field_unchanged": true,
  "identity_preserved": true,
  "objective_appearance_preserved": true,
  "clothing_accessories_preserved": true,
  "art_style_composition_preserved": true,
  "temperament_direction_preserved": true,
  "only_safety_wording_changed": true,
  "missing_details": []
}
""".strip()


def extract_age_field(prompt: str) -> str:
    """从当前角色提示词固定结构中提取年龄字段原文。"""
    match = _AGE_FIELD_RE.search(prompt or "")
    return match.group(1).strip() if match else ""


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        logger.warning("环境变量 %s 无效，使用默认值 %s", name, default)
        return default


def _create_llm_client():
    try:
        from services.char_designer.utils.llm_client import LLMClient
    except ImportError:
        from char_designer.utils.llm_client import LLMClient  # type: ignore

    provider = os.environ.get(
        "CHAR_PROMPT_REPAIR_LLM_PROVIDER",
        os.environ.get("LLM_PROVIDER", "qwen"),
    )
    model = os.environ.get("CHAR_PROMPT_REPAIR_LLM_MODEL") or None
    return LLMClient(
        provider=provider,
        model=model,
        temperature=0.2,
        max_tokens=2400,
    )


def _is_moderation_blocked(exc: Exception) -> bool:
    """兼容同一模块以 services.char_designer / char_designer 两种路径加载。"""
    return getattr(exc, "code", "") == "moderation_blocked"


def _contains_casefold(text_value: str, phrase: str) -> bool:
    return phrase.casefold() in text_value.casefold()


def _risk_guidance_for_prompt(prompt: str) -> List[Tuple[str, str]]:
    return [
        (phrase, guidance)
        for phrase, guidance in _KNOWN_RISK_GUIDANCE
        if _contains_casefold(prompt, phrase)
    ]


def _protected_phrases_for_prompt(prompt: str) -> List[str]:
    return [phrase for phrase in _KNOWN_PROTECTED_PHRASES if phrase in prompt]


def _format_risk_guidance(risk_guidance: List[Tuple[str, str]]) -> str:
    if not risk_guidance:
        return "未命中已知案例；请只识别并健康化改写真正可能误判的主观表达。"
    return "\n".join(
        f"- 原文 `{phrase}`：{guidance}" for phrase, guidance in risk_guidance
    )


def _effective_repair_count(item: Dict[str, Any]) -> int:
    """从历史数组计算已经完成的修复次数。"""
    history = item.get("prompt_repair_history")
    if not isinstance(history, list):
        return 0
    return sum(1 for entry in history if isinstance(entry, dict))


def _validate_programmatically(
    original_prompt: str,
    rewritten_prompt: str,
    *,
    locked_age_field: str,
    immutable_prefix: str,
    risk_guidance: List[Tuple[str, str]],
    protected_phrases: List[str],
) -> None:
    if not rewritten_prompt.strip():
        raise PromptRepairError("模型返回了空提示词")
    if rewritten_prompt.strip() == original_prompt.strip():
        raise PromptRepairError("模型没有修改提示词")

    if immutable_prefix and not rewritten_prompt.startswith(immutable_prefix):
        raise PromptRepairError("重写结果修改或移除了图生图不可变前缀")

    if locked_age_field:
        rewritten_age = extract_age_field(rewritten_prompt)
        if rewritten_age != locked_age_field:
            raise PromptRepairError(
                f"年龄字段发生变化: {locked_age_field!r} -> {rewritten_age!r}"
            )
        if rewritten_prompt.count(locked_age_field) != original_prompt.count(locked_age_field):
            raise PromptRepairError("年龄字段出现次数发生变化")

    remaining_risks = [
        phrase
        for phrase, _ in risk_guidance
        if _contains_casefold(rewritten_prompt, phrase)
    ]
    if remaining_risks:
        raise PromptRepairError(f"已知风险表达仍未被中性化处理: {remaining_risks}")

    missing_protected = [
        phrase
        for phrase in protected_phrases
        if rewritten_prompt.count(phrase) != original_prompt.count(phrase)
    ]
    if missing_protected:
        raise PromptRepairError(f"客观辨识特征被修改或删除: {missing_protected}")

    added_slogans = [
        phrase
        for phrase in _FORBIDDEN_COMPLIANCE_SLOGANS
        if _contains_casefold(rewritten_prompt, phrase)
    ]
    if added_slogans:
        raise PromptRepairError(f"重写结果包含不应加入的审核声明: {added_slogans}")

    original_len = max(1, len(original_prompt.strip()))
    length_ratio = len(rewritten_prompt.strip()) / original_len
    if length_ratio < float(os.environ.get("CHAR_PROMPT_REPAIR_MIN_LENGTH_RATIO", "0.72")):
        raise PromptRepairError(f"重写后信息量下降过多，长度比例={length_ratio:.2f}")
    if length_ratio > float(os.environ.get("CHAR_PROMPT_REPAIR_MAX_LENGTH_RATIO", "1.50")):
        raise PromptRepairError(f"重写后新增内容过多，长度比例={length_ratio:.2f}")

    similarity = difflib.SequenceMatcher(
        None, original_prompt.strip(), rewritten_prompt.strip()
    ).ratio()
    if similarity < float(os.environ.get("CHAR_PROMPT_REPAIR_MIN_SIMILARITY", "0.35")):
        raise PromptRepairError(f"重写与原提示词差异过大，相似度={similarity:.2f}")


def _audit_rewrite(
    llm_client: Any,
    original_prompt: str,
    rewritten_prompt: str,
    *,
    locked_age_field: str,
    risk_guidance: List[Tuple[str, str]],
    protected_phrases: List[str],
) -> None:
    audit = llm_client.chat_completion_json(
        _AUDIT_SYSTEM_PROMPT,
        (
            f"锁定年龄字段：{locked_age_field or '(无年龄字段)'}\n\n"
            f"允许且应当修改的风险表达：\n{_format_risk_guidance(risk_guidance)}\n\n"
            f"必须逐字保留的客观短语：{protected_phrases or '(无额外短语)'}\n\n"
            f"原始 positive_prompt：\n{original_prompt}\n\n"
            f"重写 positive_prompt：\n{rewritten_prompt}"
        ),
        temperature=0.0,
        max_tokens=1000,
        timeout=120,
    )
    required_true = (
        "age_field_unchanged",
        "identity_preserved",
        "objective_appearance_preserved",
        "clothing_accessories_preserved",
        "art_style_composition_preserved",
        "temperament_direction_preserved",
        "only_safety_wording_changed",
    )
    failed = [key for key in required_true if audit.get(key) is not True]
    missing = audit.get("missing_details") or []
    if failed or missing:
        raise PromptRepairError(
            f"重写一致性审核未通过: failed={failed}, missing_details={missing}"
        )


def rewrite_full_prompt(
    original_prompt: str,
    *,
    rejection_reason: str,
    generation_mode: str,
    stage: str,
    immutable_prefix: str = "",
    locked_age_field: Optional[str] = None,
    llm_client: Any = None,
) -> PromptRepairResult:
    """让 LLM 整段重写提示词，并进行程序校验与第二次 LLM 一致性审核。"""
    if not original_prompt or not original_prompt.strip():
        raise PromptRepairError("原始提示词为空，无法修复")

    client = llm_client or _create_llm_client()
    age_field = extract_age_field(original_prompt) if locked_age_field is None else locked_age_field
    risk_guidance = _risk_guidance_for_prompt(original_prompt)
    protected_phrases = _protected_phrases_for_prompt(original_prompt)
    attempts = max(1, _positive_int_env("CHAR_PROMPT_REPAIR_REWRITE_ATTEMPTS", 2))
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        retry_note = f"\n上一次结果未通过校验：{last_error}" if last_error else ""
        response = client.chat_completion_json(
            _REWRITE_SYSTEM_PROMPT,
            (
                f"生图类型：{generation_mode}\n"
                f"生成阶段：{stage}\n"
                f"审核拒绝原因：{rejection_reason[:500]}\n"
                f"锁定年龄字段（必须逐字保留）：{age_field or '(无年龄字段)'}\n"
                f"不可变前缀（必须逐字保留在开头）：{immutable_prefix or '(无)'}"
                f"\n本次命中的已知风险表达及修改方向：\n{_format_risk_guidance(risk_guidance)}"
                f"\n必须逐字保留的客观短语：{protected_phrases or '(无额外短语)'}"
                f"{retry_note}\n\n"
                f"原始 positive_prompt：\n{original_prompt}"
            ),
            temperature=0.2,
            max_tokens=2400,
            timeout=120,
        )
        rewritten = str(response.get("positive_prompt") or "").strip()
        summary = response.get("change_summary") or []
        if not isinstance(summary, list):
            summary = [str(summary)]

        try:
            _validate_programmatically(
                original_prompt,
                rewritten,
                locked_age_field=age_field,
                immutable_prefix=immutable_prefix,
                risk_guidance=risk_guidance,
                protected_phrases=protected_phrases,
            )
            _audit_rewrite(
                client,
                original_prompt,
                rewritten,
                locked_age_field=age_field,
                risk_guidance=risk_guidance,
                protected_phrases=protected_phrases,
            )
            stored_prompt = (
                rewritten[len(immutable_prefix):]
                if immutable_prefix
                else rewritten
            )
            return PromptRepairResult(
                request_prompt=rewritten,
                stored_prompt=stored_prompt,
                change_summary=[str(item) for item in summary if str(item).strip()],
            )
        except PromptRepairError as exc:
            last_error = exc
            logger.warning("提示词整段重写第 %s/%s 次未通过校验: %s", attempt, attempts, exc)

    raise PromptRepairError(f"提示词整段重写未通过校验: {last_error}")


def _decode_prompt_items(raw_data: Any) -> List[Dict[str, Any]]:
    data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    if not isinstance(data, list):
        raise PromptRepairError("character_prompts.data 不是数组")
    return data


def _find_prompt_item(
    items: List[Dict[str, Any]], speaker_id: str, combination: str
) -> Optional[Dict[str, Any]]:
    for item in items:
        if (
            item.get("speaker_id") == speaker_id
            and item.get("combination") == combination
        ):
            return item
    return None


def update_prompt_record(
    items: List[Dict[str, Any]],
    *,
    speaker_id: str,
    combination: str,
    expected_prompt: str,
    repaired_prompt: str,
    rejection_reason: str,
    change_summary: List[str],
) -> Tuple[List[Dict[str, Any]], int]:
    """纯函数式更新一条角色提示词记录；其它记录和 negative_prompt 保持不变。"""
    updated_items = copy.deepcopy(items)
    item = _find_prompt_item(updated_items, speaker_id, combination)
    if item is None:
        raise PromptRepairError(
            f"未找到提示词记录: speaker_id={speaker_id}, combination={combination}"
        )
    current_prompt = str(item.get("positive_prompt") or "")
    if current_prompt != expected_prompt:
        raise PromptRepairError("character_prompts 中的提示词已被其它进程修改")

    policy_count = _effective_repair_count(item) + 1
    item["positive_prompt"] = repaired_prompt
    for key in _LEGACY_TOP_LEVEL_REPAIR_KEYS:
        item.pop(key, None)

    history = item.get("prompt_repair_history")
    if not isinstance(history, list):
        history = []
    cleaned_history = []
    sequence_counts = []
    for raw_entry in history:
        if not isinstance(raw_entry, dict):
            continue
        entry = copy.deepcopy(raw_entry)
        for key in _LEGACY_HISTORY_REPAIR_KEYS:
            entry.pop(key, None)
        cleaned_history.append(entry)
        try:
            sequence_counts.append(int(entry.get("count") or 0))
        except (TypeError, ValueError):
            continue
    sequence_count = max(sequence_counts, default=0) + 1
    cleaned_history.append({
        "count": sequence_count,
        "previous_positive_prompt": current_prompt,
        "positive_prompt": repaired_prompt,
        "reason": rejection_reason[:1000],
        "change_summary": change_summary,
    })
    item["prompt_repair_history"] = cleaned_history[-10:]
    return updated_items, policy_count


def _load_persisted_prompt(engine, context: PromptPersistenceContext) -> Tuple[str, int]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM character_prompts WHERE task_id = :tid"),
            {"tid": context.task_id},
        ).fetchone()
    if not row:
        raise PromptRepairError(f"未找到 character_prompts: task_id={context.task_id}")
    item = _find_prompt_item(
        _decode_prompt_items(row[0]), context.speaker_id, context.combination
    )
    if item is None:
        raise PromptRepairError(
            f"未找到提示词记录: speaker_id={context.speaker_id}, combination={context.combination}"
        )
    return str(item.get("positive_prompt") or ""), _effective_repair_count(item)


def _persist_repaired_prompt(
    engine,
    context: PromptPersistenceContext,
    *,
    expected_prompt: str,
    repair: PromptRepairResult,
    rejection_reason: str,
) -> PromptRepairResult:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT data FROM character_prompts WHERE task_id = :tid FOR UPDATE"),
            {"tid": context.task_id},
        ).fetchone()
        if not row:
            raise PromptRepairError(f"未找到 character_prompts: task_id={context.task_id}")

        items = _decode_prompt_items(row[0])
        current_item = _find_prompt_item(items, context.speaker_id, context.combination)
        if current_item is None:
            raise PromptRepairError(
                f"未找到提示词记录: speaker_id={context.speaker_id}, combination={context.combination}"
            )
        current_prompt = str(current_item.get("positive_prompt") or "")
        if current_prompt != expected_prompt:
            return PromptRepairResult(
                request_prompt=context.immutable_prefix + current_prompt,
                stored_prompt=current_prompt,
                repair_count=_effective_repair_count(current_item),
                persisted=False,
            )

        updated_items, count = update_prompt_record(
            items,
            speaker_id=context.speaker_id,
            combination=context.combination,
            expected_prompt=expected_prompt,
            repaired_prompt=repair.stored_prompt,
            rejection_reason=rejection_reason,
            change_summary=repair.change_summary,
        )
        conn.execute(
            text(
                """
                UPDATE character_prompts
                SET data = cast(:data AS jsonb), update_time = CURRENT_TIMESTAMP
                WHERE task_id = :tid
                """
            ),
            {
                "data": json.dumps(updated_items, ensure_ascii=False),
                "tid": context.task_id,
            },
        )

    repair.repair_count = count
    repair.persisted = True
    return repair


def repair_character_prompt(
    request_prompt: str,
    *,
    context: PromptPersistenceContext,
    rejection_reason: str,
    generation_mode: str,
    stage: str,
    llm_client: Any = None,
    engine: Any = None,
) -> PromptRepairResult:
    """整段修复并事务性覆盖指定角色组合的 positive_prompt。"""
    prompt_engine = engine or platform_engine
    stored_prompt, current_count = _load_persisted_prompt(prompt_engine, context)
    expected_request_prompt = context.immutable_prefix + stored_prompt

    if expected_request_prompt != request_prompt:
        logger.info(
            "提示词已被其它进程更新，先使用数据库中的新版本重试: %s/%s",
            context.speaker_id,
            context.combination,
        )
        return PromptRepairResult(
            request_prompt=expected_request_prompt,
            stored_prompt=stored_prompt,
            repair_count=current_count,
            persisted=False,
        )

    max_repairs = _positive_int_env("CHAR_PROMPT_REPAIR_MAX_ATTEMPTS", 2)
    if current_count >= max_repairs:
        raise PromptRepairLimitReached(
            f"{context.combination} 已达到自动修复上限 {max_repairs} 次"
        )

    repair = rewrite_full_prompt(
        request_prompt,
        rejection_reason=rejection_reason,
        generation_mode=generation_mode,
        stage=stage,
        immutable_prefix=context.immutable_prefix,
        llm_client=llm_client,
    )
    return _persist_repaired_prompt(
        prompt_engine,
        context,
        expected_prompt=stored_prompt,
        repair=repair,
        rejection_reason=rejection_reason,
    )


def execute_with_prompt_repair(
    operation: Callable[[str], T],
    request_prompt: str,
    *,
    generation_mode: str,
    stage: str,
    persistence: Optional[PromptPersistenceContext] = None,
    immutable_prefix: str = "",
    llm_client: Any = None,
) -> Tuple[T, str]:
    """执行一次生图；命中 moderation_blocked 时整段修复并立即重试。"""
    current_prompt = request_prompt
    seen_prompts = {current_prompt}
    local_limit = _positive_int_env("CHAR_PROMPT_REPAIR_MAX_ATTEMPTS", 2)
    repair_events = 0

    while True:
        try:
            return operation(current_prompt), current_prompt
        except Exception as exc:
            if not _is_moderation_blocked(exc):
                raise
            if repair_events >= local_limit:
                raise PromptRepairLimitReached(
                    f"{stage} 已达到本次自动修复上限 {local_limit} 次"
                ) from exc
            repair_events += 1
            rejection_reason = str(getattr(exc, "reason", "") or str(exc))

            if persistence is not None:
                repair = repair_character_prompt(
                    current_prompt,
                    context=persistence,
                    rejection_reason=rejection_reason,
                    generation_mode=generation_mode,
                    stage=stage,
                    llm_client=llm_client,
                )
            else:
                repair = rewrite_full_prompt(
                    current_prompt,
                    rejection_reason=rejection_reason,
                    generation_mode=generation_mode,
                    stage=stage,
                    immutable_prefix=immutable_prefix,
                    llm_client=llm_client,
                )

            if repair.request_prompt in seen_prompts:
                raise PromptRepairError("修复后提示词没有形成新版本，停止重试") from exc
            seen_prompts.add(repair.request_prompt)
            current_prompt = repair.request_prompt
            logger.warning(
                "检测到 moderation_blocked，已整段修复提示词并立即重试: "
                "stage=%s, persisted=%s, count=%s, summary=%s",
                stage,
                repair.persisted,
                repair.repair_count,
                repair.change_summary,
            )
