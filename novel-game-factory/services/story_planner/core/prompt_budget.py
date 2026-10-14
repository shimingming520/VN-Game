"""Conservative token budgeting and prompt trimming for story_planner LLM calls."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Mapping, Optional


DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
DEFAULT_MAX_INPUT_TOKENS = 80_000
DEFAULT_PROMPT_SAFETY_TOKENS = 8_000
SYSTEM_PROMPT_BUDGET_RATIO = 0.4
TRIM_MARKER = "\n...[上下文已按预算裁剪]...\n"


@dataclass(frozen=True)
class PromptBudgetSettings:
    context_window_tokens: int
    max_input_tokens: int
    safety_tokens: int


@dataclass(frozen=True)
class PreparedPrompts:
    system_prompt: str
    user_prompt: str
    input_token_budget: int
    estimated_input_tokens: int
    max_output_tokens: int
    trimmed: bool


def _positive_int(
    source: Mapping[str, str],
    key: str,
    default: int,
    minimum: int,
) -> int:
    try:
        value = int(source.get(key, ""))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def resolve_prompt_budget_settings(
    env: Optional[Mapping[str, str]] = None,
) -> PromptBudgetSettings:
    source = os.environ if env is None else env
    context_window_tokens = _positive_int(
        source,
        "STORY_PLANNER_LLM_CONTEXT_WINDOW_TOKENS",
        DEFAULT_CONTEXT_WINDOW_TOKENS,
        minimum=16_000,
    )
    safety_tokens = _positive_int(
        source,
        "STORY_PLANNER_LLM_PROMPT_SAFETY_TOKENS",
        DEFAULT_PROMPT_SAFETY_TOKENS,
        minimum=512,
    )
    configured_max_input = _positive_int(
        source,
        "STORY_PLANNER_LLM_MAX_INPUT_TOKENS",
        DEFAULT_MAX_INPUT_TOKENS,
        minimum=4_000,
    )
    usable_input = max(1_000, context_window_tokens - safety_tokens - 1_000)
    return PromptBudgetSettings(
        context_window_tokens=context_window_tokens,
        max_input_tokens=min(configured_max_input, usable_input),
        safety_tokens=safety_tokens,
    )


PROMPT_BUDGET_SETTINGS = resolve_prompt_budget_settings()


def estimate_prompt_tokens(text: str) -> int:
    """Estimate tokens without a model tokenizer, biased toward safe headroom."""
    estimate = 0.0
    for char in str(text or ""):
        codepoint = ord(char)
        if char.isspace():
            estimate += 0.25
        elif codepoint < 128 and char.isalnum():
            estimate += 0.35
        elif codepoint < 128:
            estimate += 1.0
        elif (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        ):
            estimate += 1.2
        else:
            estimate += 2.0
    return math.ceil(estimate)


def calculate_input_token_budget(
    max_output_tokens: int,
    settings: PromptBudgetSettings = PROMPT_BUDGET_SETTINGS,
) -> int:
    context_remaining = (
        settings.context_window_tokens
        - max(0, int(max_output_tokens))
        - settings.safety_tokens
    )
    return max(1, min(settings.max_input_tokens, context_remaining))


def calculate_output_token_budget(
    requested_output_tokens: int,
    settings: PromptBudgetSettings = PROMPT_BUDGET_SETTINGS,
) -> int:
    maximum_output = max(
        1,
        settings.context_window_tokens - settings.safety_tokens - 1,
    )
    return max(1, min(maximum_output, int(requested_output_tokens)))


def trim_text_to_budget(text: str, max_tokens: int) -> str:
    value = str(text or "")
    if estimate_prompt_tokens(value) <= max_tokens:
        return value
    if max_tokens <= 0:
        return ""

    def _take_prefix(token_budget: int) -> str:
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            if estimate_prompt_tokens(value[:middle]) <= token_budget:
                low = middle
            else:
                high = middle - 1
        return value[:low]

    def _take_suffix(token_budget: int) -> str:
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            if estimate_prompt_tokens(value[len(value) - middle :]) <= token_budget:
                low = middle
            else:
                high = middle - 1
        return value[len(value) - low :] if low else ""

    marker_tokens = estimate_prompt_tokens(TRIM_MARKER)
    if max_tokens <= marker_tokens:
        return _take_prefix(max_tokens)

    content_budget = max_tokens - marker_tokens
    head_budget = (content_budget + 1) // 2
    tail_budget = content_budget - head_budget
    return _take_prefix(head_budget) + TRIM_MARKER + _take_suffix(tail_budget)


def fit_prompts_to_budget(
    system_prompt: str,
    user_prompt: str,
    max_output_tokens: int,
    settings: PromptBudgetSettings = PROMPT_BUDGET_SETTINGS,
) -> PreparedPrompts:
    system_value = str(system_prompt or "")
    user_value = str(user_prompt or "")
    fitted_output_tokens = calculate_output_token_budget(max_output_tokens, settings)
    input_budget = calculate_input_token_budget(fitted_output_tokens, settings)
    estimated_tokens = estimate_prompt_tokens(system_value) + estimate_prompt_tokens(
        user_value
    )
    if estimated_tokens <= input_budget:
        return PreparedPrompts(
            system_prompt=system_value,
            user_prompt=user_value,
            input_token_budget=input_budget,
            estimated_input_tokens=estimated_tokens,
            max_output_tokens=fitted_output_tokens,
            trimmed=False,
        )

    system_tokens = estimate_prompt_tokens(system_value)
    user_tokens = estimate_prompt_tokens(user_value)
    system_budget = min(
        system_tokens,
        int(input_budget * SYSTEM_PROMPT_BUDGET_RATIO),
    )
    user_budget = min(user_tokens, input_budget - system_budget)
    remaining = input_budget - system_budget - user_budget

    if remaining and system_tokens > system_budget:
        extra = min(remaining, system_tokens - system_budget)
        system_budget += extra
        remaining -= extra
    if remaining and user_tokens > user_budget:
        user_budget += min(remaining, user_tokens - user_budget)

    fitted_system = trim_text_to_budget(system_value, system_budget)
    fitted_user = trim_text_to_budget(user_value, user_budget)
    fitted_tokens = estimate_prompt_tokens(fitted_system) + estimate_prompt_tokens(
        fitted_user
    )
    return PreparedPrompts(
        system_prompt=fitted_system,
        user_prompt=fitted_user,
        input_token_budget=input_budget,
        estimated_input_tokens=fitted_tokens,
        max_output_tokens=fitted_output_tokens,
        trimmed=True,
    )
