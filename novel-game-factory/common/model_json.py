"""模型 JSON 输出的容错解析。

模型偶发把单个 token 写坏——见过 ``"bpm": fifty`` 这种数字没加引号的写法。
裸词不是合法 JSON（规范只允许 true/false/null 三个裸字面量），标准库直接停在
那里，导致整批提取结果因为一个字段全部作废。真实案例：task test_a31192b4 的
chapter_1 有 15 段 BGM，第 11 段一个 bpm 写成 fifty，15 段全丢。

这里统一先用标准库解析，失败再用 json_repair 兜一层；两者都不行才抛
JSONDecodeError，交给调用方原有的 tenacity 重试或降级分支处理。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

try:
    from json_repair import repair_json
except ImportError:  # 环境未装 json-repair 时退化为标准库行为
    repair_json = None


_DEFAULT_LOGGER = logging.getLogger(__name__)

# 英文数词还原表。模型把数值字段写成单词时用，只覆盖常见量级。
_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100,
}


def word_to_number(value: Any) -> Optional[int]:
    """把 "fifty" / "eighty-four" 这类英文数词转成整数；转不了返回 None。"""
    if not isinstance(value, str):
        return None
    text_value = value.strip().lower().replace("_", " ").replace("-", " ")
    if not text_value:
        return None
    current = 0
    matched = False
    for word in text_value.split():
        if word == "and":
            continue
        if word not in _NUMBER_WORDS:
            return None
        matched = True
        number = _NUMBER_WORDS[word]
        if number == 100:
            current = (current or 1) * 100
        else:
            current += number
    return current if matched else None


def coerce_int(value: Any) -> Optional[int]:
    """把被写成数词/数字字符串/浮点的数值还原成整数；不可靠就返回 None。

    布尔值一律拒绝：True 在 Python 里等于 1，但契约层明确视其为非法数值，
    这里不能悄悄替模型把它变成数字。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
        return word_to_number(stripped)
    return None


def coerce_numeric_fields(
    items: Any,
    fields: tuple,
    *,
    context: str = "",
    logger: Optional[logging.Logger] = None,
) -> Any:
    """把 items 里指定字段的数值还原成整数，就地修改并返回。

    只动 fields 列出的字段，避免误改 prompt 等自由文本。转换失败保持原样，
    交给下游契约判定并丢弃该条——不在这里吞掉问题。
    """
    log = logger or _DEFAULT_LOGGER
    if not isinstance(items, list):
        return items
    for item in items:
        if not isinstance(item, dict):
            continue
        for field in fields:
            if field not in item:
                continue
            raw_value = item[field]
            if isinstance(raw_value, int) and not isinstance(raw_value, bool):
                continue
            converted = coerce_int(raw_value)
            if converted is None:
                continue
            item[field] = converted
            log.warning(
                "coerced_numeric_field: context=%s field=%s raw=%r -> %s",
                context or "-", field, raw_value, converted,
            )
    return items


def parse_model_json(
    raw_content: Any,
    *,
    context: str = "",
    logger: Optional[logging.Logger] = None,
    expect: type = dict,
) -> Any:
    """解析模型输出。标准库失败时用 json_repair 兜一层。

    Args:
        raw_content: 模型返回的原始文本。
        context: 出问题时写进日志的定位信息（节点 ID、场景 ID 等）。
        expect: 期望的顶层类型，dict 或 list。修复结果类型不符视为修复失败。

    Raises:
        json.JSONDecodeError: 标准库和 json_repair 都读不出期望结构时抛出，
            交给调用方重试或降级——静默返回空结果会让缺数据变得难以排查。
    """
    log = logger or _DEFAULT_LOGGER
    text_value = raw_content if isinstance(raw_content, str) else str(raw_content or "")
    try:
        return json.loads(text_value)
    except json.JSONDecodeError as exc:
        if repair_json is None:
            raise
        try:
            repaired = repair_json(text_value, return_objects=True)
        except Exception:
            raise exc
        if isinstance(repaired, expect) and repaired:
            log.warning(
                "repaired_model_json: context=%s error=%s",
                context or "-", exc,
            )
            return repaired
        raise
