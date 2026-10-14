"""CG 提示词的最小化内容审核修复。

CG 提示词承载了剧情中的镜头、人物关系和单一事件，不能复用角色/场景的
“整段重写”策略。本模块只接受一个明确的短语替换列表，并在本地应用替换：

* 原始提示词永远保留；
* 未被替换的字符保持不变；
* 重要人物身份、实体名称和场景定位短语不可被替换；
* 替换数量、长度变化和文本相似度均有上限/下限；
* 任何不符合合同的 LLM 返回都直接失败，不生成猜测性的替代提示词。

模块不负责调用图片 Provider，也不修改数据库中的 cg 数据。
"""

from __future__ import annotations

import difflib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

logger = logging.getLogger(__name__)


class CGPromptRepairError(RuntimeError):
    """CG 提示词无法进行安全的最小修复。"""


@dataclass(frozen=True)
class CGPromptReplacement:
    source: str
    target: str
    reason: str = ""


@dataclass(frozen=True)
class CGPromptRepairResult:
    original_prompt: str
    repaired_prompt: str
    replacements: List[CGPromptReplacement] = field(default_factory=list)
    repair_count: int = 1
    rejection_reason: str = ""


_SYSTEM_PROMPT = """
You are a strict minimal patch editor for a cinematic visual-novel CG prompt.
The image provider rejected the prompt for content moderation. Do NOT rewrite
the prompt. Return only a JSON object with a short list of exact substring
replacements:

{
  "replacements": [
    {"from": "exact original phrase", "to": "neutral equivalent", "reason": "brief reason"}
  ]
}

Rules:
1. Return only phrases that are likely to trigger moderation. If unsure, return
   an empty list rather than changing the story.
2. Preserve the original event, characters, identities, actions, relationships,
   camera, composition, location, lighting, mood, time and visual style.
3. Never add or remove a character, event, action, camera instruction or scene.
4. Never change an immutable identity/location phrase supplied by the user;
   semantic phrases may only reflect the exact short replacement you return.
5. Keep each replacement short and as close in meaning and length as possible.
6. Do not return a full rewritten prompt and do not add safety disclaimers.
""".strip()


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def is_moderation_blocked(exc: BaseException) -> bool:
    """只接受 Provider 明确标记的 moderation_blocked，避免误判普通错误。"""

    return str(getattr(exc, "code", "") or "").strip().lower() == "moderation_blocked"


def _dedupe_phrases(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        phrase = str(value or "").strip()
        if len(phrase) < 2 or phrase in seen:
            continue
        seen.add(phrase)
        result.append(phrase)
    return result


def _parse_replacements(raw: Any) -> List[CGPromptReplacement]:
    if not isinstance(raw, list):
        raise CGPromptRepairError("LLM 返回缺少 replacements 数组")
    replacements: List[CGPromptReplacement] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise CGPromptRepairError(f"replacements[{index}] 不是对象")
        source = str(item.get("from") or "")
        target = str(item.get("to") or "")
        reason = str(item.get("reason") or "")
        if not source.strip() or not target.strip():
            raise CGPromptRepairError(
                f"replacements[{index}] 必须同时提供非空 from/to；不允许静默删除原文"
            )
        if source == target:
            raise CGPromptRepairError(f"replacements[{index}] from/to 相同")
        if (
            len(source) > 80
            or len(target) > 120
            or len(source.split()) > 8
            or len(target.split()) > 12
        ):
            raise CGPromptRepairError(f"replacements[{index}] 单个替换过长")
        replacements.append(CGPromptReplacement(source, target, reason))
    max_replacements = _positive_int_env("CG_PROMPT_REPAIR_MAX_REPLACEMENTS", 4)
    if not replacements:
        raise CGPromptRepairError("LLM 未提出可执行的最小替换")
    if len(replacements) > max_replacements:
        raise CGPromptRepairError(
            f"替换数量 {len(replacements)} 超过上限 {max_replacements}"
        )
    sources = [item.source for item in replacements]
    if len(set(sources)) != len(sources):
        raise CGPromptRepairError("replacements 中存在重复 from 短语")
    for index, item in enumerate(replacements):
        if any(
            other.source != item.source
            and (other.source in item.target or item.source in other.target)
            for other in replacements[index + 1 :]
        ):
            raise CGPromptRepairError("replacements 存在相互覆盖的 from/to 短语")
    return replacements


def apply_replacements(
    original_prompt: str,
    replacements: Iterable[CGPromptReplacement],
    *,
    protected_phrases: Iterable[str] = (),
    immutable_phrases: Iterable[str] = (),
) -> str:
    """按精确子串应用替换，并拒绝覆盖不可变语义短语。"""

    prompt = original_prompt
    protected = _dedupe_phrases(protected_phrases)
    immutable = _dedupe_phrases(immutable_phrases)
    for replacement in replacements:
        source = replacement.source
        target = replacement.target
        occurrences = prompt.count(source)
        if occurrences == 0:
            raise CGPromptRepairError(f"原提示词中不存在待替换短语: {source!r}")
        if occurrences > 1:
            raise CGPromptRepairError(
                f"待替换短语出现 {occurrences} 次，无法安全定位: {source!r}"
            )
        if any(source in phrase or phrase in source for phrase in immutable):
            raise CGPromptRepairError(f"替换短语覆盖不可变身份/场景: {source!r}")
        if any(source == phrase or phrase in source for phrase in protected):
            raise CGPromptRepairError(f"替换短语覆盖不可变语义: {source!r}")
        prompt = prompt.replace(source, target, 1)
    return prompt


def validate_minimal_repair(
    original_prompt: str,
    repaired_prompt: str,
    *,
    protected_phrases: Iterable[str] = (),
    immutable_phrases: Iterable[str] = (),
    replacements: Iterable[CGPromptReplacement] = (),
) -> None:
    if not original_prompt.strip() or not repaired_prompt.strip():
        raise CGPromptRepairError("原始或修复后的 CG 提示词为空")
    if original_prompt == repaired_prompt:
        raise CGPromptRepairError("修复没有改变提示词")

    protected = _dedupe_phrases(protected_phrases)
    immutable = _dedupe_phrases(immutable_phrases)
    replacement_list = list(replacements)
    missing_immutable = [
        phrase
        for phrase in immutable
        if repaired_prompt.count(phrase) != original_prompt.count(phrase)
    ]
    if missing_immutable:
        raise CGPromptRepairError(
            f"修复改变了不可变身份/场景短语: {missing_immutable}"
        )
    missing = []
    for phrase in protected:
        expected_phrase = phrase
        for replacement in replacement_list:
            expected_phrase = expected_phrase.replace(
                replacement.source, replacement.target
            )
        if repaired_prompt.count(expected_phrase) < original_prompt.count(phrase):
            missing.append(phrase)
    if missing:
        raise CGPromptRepairError(f"修复改变了不可变语义短语: {missing}")

    original_len = max(1, len(original_prompt))
    length_ratio = len(repaired_prompt) / original_len
    min_ratio = float(os.environ.get("CG_PROMPT_REPAIR_MIN_LENGTH_RATIO", "0.75"))
    max_ratio = float(os.environ.get("CG_PROMPT_REPAIR_MAX_LENGTH_RATIO", "1.40"))
    if not min_ratio <= length_ratio <= max_ratio:
        raise CGPromptRepairError(
            f"修复后长度变化过大: ratio={length_ratio:.3f}, "
            f"allowed=[{min_ratio:.2f}, {max_ratio:.2f}]"
        )

    similarity = difflib.SequenceMatcher(
        None, original_prompt, repaired_prompt
    ).ratio()
    min_similarity = float(os.environ.get("CG_PROMPT_REPAIR_MIN_SIMILARITY", "0.80"))
    if similarity < min_similarity:
        raise CGPromptRepairError(
            f"修复与原提示词相似度过低: {similarity:.3f} < {min_similarity:.2f}"
        )


def _default_llm_client() -> Any:
    """在 scene_painter 镜像内使用已有的 OpenAI 兼容依赖。"""

    from openai import OpenAI

    provider = os.environ.get(
        "CG_PROMPT_REPAIR_LLM_PROVIDER",
        os.environ.get("LLM_PROVIDER", "qwen"),
    ).strip().lower()
    default_model = "gpt-4o" if provider == "openai" else "qwen3-max"
    model = os.environ.get("CG_PROMPT_REPAIR_LLM_MODEL") or os.environ.get(
        "LLM_MODEL", default_model
    )
    explicit_key = os.environ.get("CG_PROMPT_REPAIR_API_KEY", "").strip()
    gpt_key = (
        os.environ.get("GPT_IMAGE_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    api_key = explicit_key or gpt_key or dashscope_key
    if not api_key:
        raise CGPromptRepairError(
            "CG 提示词修复缺少 CG_PROMPT_REPAIR_API_KEY / GPT_IMAGE_API_KEY / OPENAI_API_KEY"
        )

    base_url = os.environ.get("CG_PROMPT_REPAIR_BASE_URL", "").strip()
    if not base_url and provider in {"qwen", "openai_compatible", "openai-compatible"}:
        if explicit_key or gpt_key:
            base_url = os.environ.get(
                "GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"
            ).strip()
        else:
            base_url = os.environ.get(
                "DASHSCOPE_COMPATIBLE_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ).strip()
    if base_url and not base_url.rstrip("/").endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"

    client_kwargs = {
        "api_key": api_key,
        "max_retries": max(
            0, int(os.environ.get("CG_PROMPT_REPAIR_LLM_MAX_RETRIES", "2"))
        ),
    }
    if base_url:
        client_kwargs["base_url"] = base_url
    return _OpenAIJsonClient(
        OpenAI(**client_kwargs),
        model=model,
    )


class _OpenAIJsonClient:
    """只封装 CG 修复所需的一个结构化 JSON 请求。"""

    def __init__(self, client: Any, *, model: str):
        self.client = client
        self.model = model

    def chat_completion_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float,
        max_tokens: int,
        timeout: int,
    ) -> Dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            timeout=timeout,
        )
        content = str(response.choices[0].message.content or "").strip()
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            start = content.find("{")
            end = content.rfind("}")
            if start < 0 or end <= start:
                raise CGPromptRepairError(
                    f"CG 提示词修复 LLM 返回的 JSON 无法解析: {content[:300]}"
                )
            try:
                parsed = json.loads(content[start : end + 1])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CGPromptRepairError(
                    f"CG 提示词修复 LLM 返回的 JSON 无法解析: {content[:300]}"
                ) from exc
        if not isinstance(parsed, Mapping):
            raise CGPromptRepairError(
                "CG 提示词修复 LLM 返回的 JSON 不是 object"
            )
        return dict(parsed)


def repair_cg_prompt(
    original_prompt: str,
    *,
    rejection_reason: str,
    protected_phrases: Iterable[str] = (),
    immutable_phrases: Iterable[str] = (),
    llm_client: Optional[Any] = None,
) -> CGPromptRepairResult:
    """请求一次最小替换并进行严格的本地校验。"""

    if not original_prompt or not original_prompt.strip():
        raise CGPromptRepairError("原始 CG 提示词为空")

    protected = _dedupe_phrases(protected_phrases)
    immutable = _dedupe_phrases(immutable_phrases)
    client = llm_client or _default_llm_client()
    user_prompt = (
        f"审核拒绝原因：{str(rejection_reason or '')[:800]}\n\n"
        f"不可修改的身份/场景短语：{immutable or '(无)'}\n"
        f"只能在明确替换后保持等义的语义短语：{protected or '(无)'}\n\n"
        f"原始 CG 提示词：\n{original_prompt}"
    )
    raw = client.chat_completion_json(
        _SYSTEM_PROMPT,
        user_prompt,
        temperature=0.0,
        max_tokens=1200,
        timeout=int(os.environ.get("CG_PROMPT_REPAIR_TIMEOUT_SECONDS", "120")),
    )
    replacements = _parse_replacements(raw.get("replacements"))
    repaired = apply_replacements(
        original_prompt,
        replacements,
        protected_phrases=protected,
        immutable_phrases=immutable,
    )
    validate_minimal_repair(
        original_prompt,
        repaired,
        protected_phrases=protected,
        immutable_phrases=immutable,
        replacements=replacements,
    )
    return CGPromptRepairResult(
        original_prompt=original_prompt,
        repaired_prompt=repaired,
        replacements=replacements,
        rejection_reason=str(rejection_reason or "")[:1000],
    )


def replacement_audit(result: CGPromptRepairResult) -> List[Dict[str, str]]:
    """转换为可写入 visual_debug extra 的稳定 JSON 结构。"""

    return [
        {
            "from": item.source,
            "to": item.target,
            "reason": item.reason,
        }
        for item in result.replacements
    ]
