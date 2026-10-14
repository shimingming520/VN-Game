"""模板渲染器 (Template Renderer)

用于把模板字符串中的 ``{name}`` 占位符替换为真实值，同时保留文本中的
JSON 花括号不被误当作占位符处理。

背景：提示词模板里经常内嵌 JSON 示例（如 ``{"role": "..."}``）。若直接用
Python 内置的 ``str.format()``，这些 JSON 花括号会被当成占位符而报错。本模块
逐字符扫描模板，仅替换形如 ``{变量名}`` 的合法占位符，其余花括号一律原样保留。

替换规则：
    - ``{{``        -> 输出字面量 ``{``（转义，向后兼容）
    - ``}}``        -> 输出字面量 ``}``（转义，向后兼容）
    - ``{name}``    -> 当括号内是合法标识符时，替换为 variables 中对应的值
    - 其余 ``{...}`` -> 原样保留（例如 JSON 片段，内容非纯标识符）

注意：本模块只负责“渲染/填充”模板，不解析、不修复、不校验 JSON 结构。
它在调用 LLM“之前”组装提示词，而非在拿到 LLM 回复“之后”清洗输出。
"""

import re
from typing import Any, Mapping


_PLACEHOLDER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def render_prompt_template(
    template: str,
    variables: Mapping[str, Any],
    *,
    source: str = "prompt",
) -> str:
    """Render simple ``{name}`` placeholders while preserving JSON braces.

    Prompt files often contain JSON examples. Unlike ``str.format()``, this
    renderer treats braces as literal text unless they contain a simple named
    placeholder. Existing doubled braces remain supported for backward
    compatibility and are unescaped by one level.
    """
    if not isinstance(template, str):
        raise TypeError(f"{source} template must be a string")

    rendered = []
    index = 0

    while index < len(template):
        if template.startswith("{{", index):
            rendered.append("{")
            index += 2
            continue

        if template.startswith("}}", index):
            rendered.append("}")
            index += 2
            continue

        if template[index] == "{":
            closing_index = template.find("}", index + 1)
            if closing_index != -1:
                placeholder = template[index + 1:closing_index]
                if _PLACEHOLDER_NAME.fullmatch(placeholder):
                    if placeholder not in variables:
                        raise ValueError(
                            f"{source} is missing template variable: {placeholder}"
                        )
                    rendered.append(str(variables[placeholder]))
                    index = closing_index + 1
                    continue

        rendered.append(template[index])
        index += 1

    return "".join(rendered)
