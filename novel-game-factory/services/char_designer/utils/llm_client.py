"""
LLM 客户端封装，支持 OpenAI、Claude 和千问 (DashScope) API。
"""
import os
import json
import re
import time
from typing import Dict, Any, Optional, Union
from enum import Enum
from json_repair import repair_json

try:
    import sys as _sys
    _project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if _project_root not in _sys.path:
        _sys.path.insert(0, _project_root)
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

try:
    from openai import OpenAI
    from anthropic import Anthropic
    import dashscope
    from dashscope import Generation
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

# 尝试导入 requests 异常用于网络错误处理
try:
    import requests.exceptions
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# 尝试导入 OpenAI 和 Anthropic 的特定异常
try:
    from openai import APIError, APIConnectionError, APITimeoutError
    HAS_OPENAI_EXCEPTIONS = True
except ImportError:
    HAS_OPENAI_EXCEPTIONS = False

try:
    from anthropic import APIError as AnthropicAPIError, APIConnectionError as AnthropicAPIConnectionError, APITimeoutError as AnthropicAPITimeoutError
    HAS_ANTHROPIC_EXCEPTIONS = True
except ImportError:
    HAS_ANTHROPIC_EXCEPTIONS = False

# 尝试导入 DashScope 异常
try:
    from dashscope.common.error import InvalidTask, UnsupportedModel, AuthenticationError, ServiceUnavailableError
    HAS_DASHSCOPE_EXCEPTIONS = True
except ImportError:
    HAS_DASHSCOPE_EXCEPTIONS = False


class LLMProvider(Enum):
    """支持的 LLM 提供商"""
    OPENAI = "openai"
    CLAUDE = "claude"
    QWEN = "qwen"
    # 可扩展：其他提供商如 Gemini, DeepSeek 等


class LLMClient:
    """
    LLM 客户端封装，统一调用 OpenAI、Claude 或千问 API。

    环境变量：
    - OPENAI_API_KEY: OpenAI API 密钥
    - ANTHROPIC_API_KEY: Claude API 密钥
    - DASHSCOPE_API_KEY: 千问 (DashScope) API 密钥
    """

    def __init__(
        self,
        provider: Union[LLMProvider, str] = LLMProvider.OPENAI,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        enable_thinking: bool = False,
    ):
        """
        初始化 LLM 客户端。

        Args:
            provider: LLM 提供商，默认为 OpenAI
            model: 模型名称，默认根据提供商选择
            temperature: 温度参数
            max_tokens: 最大生成 token 数
        """
        if not HAS_DEPS:
            raise ImportError(
                "请安装必要的依赖：pip install openai anthropic dashscope"
            )

        if isinstance(provider, str):
            provider = LLMProvider(provider.lower())

        self.provider = provider
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.enable_thinking = enable_thinking

        # 设置默认模型
        if model is None:
            if provider == LLMProvider.OPENAI:
                model = "gpt-4o"
            elif provider == LLMProvider.CLAUDE:
                model = "claude-3-5-sonnet-20241022"
            elif provider == LLMProvider.QWEN:
                model = "qwen3.7-max"  # 使用 qwen3-max 模型
            else:
                raise ValueError(f"未知提供商: {provider}")
        self.model = model

        # 初始化客户端
        self._sync_client = None

    @property
    def sync_client(self):
        """获取同步客户端"""
        if self._sync_client is None:
            if self.provider == LLMProvider.OPENAI:
                api_key = os.getenv("OPENAI_API_KEY")
                if not api_key:
                    raise ValueError("未设置 OPENAI_API_KEY 环境变量")
                self._sync_client = OpenAI(api_key=api_key)
            elif self.provider == LLMProvider.CLAUDE:
                api_key = os.getenv("ANTHROPIC_API_KEY")
                if not api_key:
                    raise ValueError("未设置 ANTHROPIC_API_KEY 环境变量")
                self._sync_client = Anthropic(api_key=api_key)
            elif self.provider == LLMProvider.QWEN:
                # 千问走中转站的 OpenAI 兼容接口（与 GPT 生图同一 key/中转站），
                # 不再直连 DashScope 官方，避免 DASHSCOPE_API_KEY 被封导致整条流水线中断。
                api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
                if not api_key:
                    raise ValueError("未设置 GPT_IMAGE_API_KEY（或 OPENAI_API_KEY）环境变量")
                base_url = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com").rstrip("/")
                self._sync_client = OpenAI(api_key=api_key, base_url=f"{base_url}/v1")
            else:
                raise ValueError(f"不支持的提供商: {self.provider}")
        return self._sync_client

    def _is_retryable_exception(self, exc: Exception) -> bool:
        """
        判断异常是否可重试。

        Args:
            exc: 异常对象

        Returns:
            如果是可重试的异常返回 True
        """
        # 检查 requests 异常
        if HAS_REQUESTS:
            # 首先检查具体的网络异常
            if isinstance(exc, (requests.exceptions.ProxyError,
                                requests.exceptions.SSLError,
                                requests.exceptions.ChunkedEncodingError,
                                requests.exceptions.ConnectionError,
                                requests.exceptions.Timeout)):
                return True
            # 然后检查通用的 RequestException（包含其他异常）
            if isinstance(exc, requests.exceptions.RequestException):
                return True

        # 检查 OpenAI 异常
        if HAS_OPENAI_EXCEPTIONS:
            if isinstance(exc, (APIConnectionError, APITimeoutError)):
                return True
            if isinstance(exc, APIError):
                # OpenAI APIError 可能包含状态码，如果是 5xx 错误则可重试
                if hasattr(exc, 'status_code') and exc.status_code is not None:
                    return 500 <= exc.status_code < 600 or exc.status_code == 429
                return False

        # 检查 Anthropic 异常
        if HAS_ANTHROPIC_EXCEPTIONS:
            if isinstance(exc, (AnthropicAPIConnectionError, AnthropicAPITimeoutError)):
                return True
            if isinstance(exc, AnthropicAPIError):
                if hasattr(exc, 'status_code') and exc.status_code is not None:
                    return 500 <= exc.status_code < 600 or exc.status_code == 429
                return False

        # 检查 DashScope 异常
        if HAS_DASHSCOPE_EXCEPTIONS:
            if isinstance(exc, ServiceUnavailableError):
                return True

        # 检查通用网络异常
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return True

        # 检查异常消息中是否包含可重试的关键词
        error_msg = str(exc).lower()
        retryable_keywords = [
            'connection', 'timeout', 'temporarily', 'busy', 'overloaded',
            'server error', 'internal error', 'service unavailable',
            'too many requests', 'rate limit', 'quota', 'retry'
        ]
        for keyword in retryable_keywords:
            if keyword in error_msg:
                return True

        return False

    def _is_retryable_status_code(self, status_code: int) -> bool:
        """
        判断 HTTP 状态码是否可重试。

        Args:
            status_code: HTTP 状态码

        Returns:
            如果是可重试的状态码返回 True
        """
        # 5xx 服务器错误和 429（限流）可重试
        return 500 <= status_code < 600 or status_code == 429

    def _clean_json_response(self, content: str) -> Dict[str, Any]:
        """
        清洗并解析 JSON 响应，处理 Markdown 代码块和多余的尾部文本。
        支持 JSON 对象和数组。

        Args:
            content: LLM 返回的原始文本

        Returns:
            JSON 解析后的字典（如果是数组，则包装在 {"data": [...]} 中）

        Raises:
            ValueError: 无法解析有效的 JSON
        """
        def parse_json_string(text: str) -> Any:
            """尝试解析 JSON 字符串，支持多种清理策略"""
            # 移除可能的空数组前缀（如 "[], {..." 或 "[]\n{..."）
            text = re.sub(r'^\s*\[\s*\]\s*,?\s*', '', text, flags=re.MULTILINE)

            # 首先尝试直接解析（使用 json_repair 自动修复格式错误）
            try:
                return repair_json(text, return_objects=True)
            except Exception:
                pass

            # 尝试提取 Markdown 代码块中的 JSON（对象或数组）
            # 匹配 ```json {...} ``` 或 ```json [...] ```
            json_match = re.search(r'```(?:json)?\s*(\[{.*?}\]|\{.*?\})\s*```', text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
                try:
                    return repair_json(json_str, return_objects=True)
                except json.JSONDecodeError:
                    pass

            # 暴力提取：寻找最外层的 {} 使用智能括号匹配
            start_obj = text.find('{')
            if start_obj != -1:
                # 从起始位置开始，找到匹配的结束括号
                brace_count = 0
                end_obj = -1
                in_string = False
                escape_next = False

                for i in range(start_obj, len(text)):
                    char = text[i]

                    # 处理转义字符
                    if escape_next:
                        escape_next = False
                        continue
                    if char == '\\':
                        escape_next = True
                        continue

                    # 处理字符串
                    if char == '"':
                        in_string = not in_string
                        continue

                    # 只在非字符串内计数括号
                    if not in_string:
                        if char == '{':
                            brace_count += 1
                        elif char == '}':
                            brace_count -= 1
                            if brace_count == 0:
                                end_obj = i
                                break

                if end_obj != -1:
                    json_str = text[start_obj:end_obj + 1]
                    try:
                        return repair_json(json_str, return_objects=True)
                    except Exception as e:
                        # 尝试修复常见问题
                        print(f"JSON 解析失败: {e}")
                        print(f"尝试修复JSON...")

                        # 尝试在最后一个有效位置截断
                        try:
                            # 尝试逐步缩短JSON直到能解析
                            for trim_len in range(50, min(5000, len(json_str)), 50):
                                trimmed = json_str[:-trim_len]
                                # 尝试补全结束括号
                                for ending in ['"}}}}}', '"}}}}', '"}}}', '"]}}', '"}}', '}}}}', '}}}', '}}', '}']:
                                    try:
                                        test_json = trimmed + ending
                                        result = repair_json(test_json, return_objects=True)
                                        print(f"✓ JSON修复成功（截断了{trim_len}字符）")
                                        return result
                                    except:
                                        continue
                        except:
                            pass

                        print(f"问题片段: {json_str[:500]}...")
                        raise ValueError(f"无法解析有效的 JSON: {text[:200]}...")

            # 再尝试数组
            start_arr = text.find('[')
            end_arr = text.rfind(']')
            if start_arr != -1 and end_arr != -1 and end_arr > start_arr:
                json_str = text[start_arr:end_arr + 1]
                # 跳过空数组
                if json_str.strip() == '[]':
                    raise ValueError(f"只找到空数组，未找到有效的 JSON 对象: {text[:200]}...")
                try:
                    return repair_json(json_str, return_objects=True)
                except json.JSONDecodeError as e:
                    # 如果还是失败，记录错误信息
                    print(f"JSON 清洗失败: {e}")
                    print(f"问题片段: {json_str[:200]}...")
                    raise ValueError(f"无法解析有效的 JSON: {text[:200]}...")

            raise ValueError(f"未找到有效的 JSON 结构: {text[:200]}...")

        # 解析内容
        parsed = parse_json_string(content)

        # 确保返回的是字典
        if isinstance(parsed, dict):
            return parsed
        elif isinstance(parsed, list):
            # 将数组包装在字典中
            return {"data": parsed}
        else:
            # 其他类型（如字符串、数字等）也包装在字典中
            return {"value": parsed}

    def chat_completion_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: int = 120,  # P1-12: 添加超时参数
        **kwargs,
    ) -> Dict[str, Any]:
        """
        同步聊天补全，返回 JSON 格式。

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            temperature: 温度参数，默认使用初始化值
            max_tokens: 最大 token 数，默认使用初始化值
            timeout: 超时时间（秒），默认120秒（P1-12）

        Returns:
            JSON 解析后的字典
        """
        # 为 JSON 响应添加指令
        json_system = system_prompt + "\n\n请返回有效的 JSON 格式。"
        json_user = user_prompt + "\n\n请确保输出是有效的 JSON。"

        # P1-11: 指数退避重试机制
        max_retries = 5
        base_delay = 2
        max_delay = 60

        # 千问经中转站走 OpenAI 兼容接口，与 OpenAI 分支共用同一套调用/重试逻辑。
        if self.provider in (LLMProvider.OPENAI, LLMProvider.QWEN):
            for attempt in range(max_retries):
                try:
                    response = self.sync_client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": json_system},
                            {"role": "user", "content": json_user},
                        ],
                        temperature=temperature if temperature is not None else self.temperature,
                        max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
                        response_format={"type": "json_object"},
                        timeout=timeout,  # P1-12: 添加超时
                        **kwargs,
                    )
                    content = response.choices[0].message.content
                    if _usage_tracker and hasattr(response, 'usage') and response.usage:
                        _usage_tracker.add_llm(response.usage.prompt_tokens, response.usage.completion_tokens, self.model)
                    return self._clean_json_response(content)
                except Exception as e:
                    # P1-11: 指数退避重试
                    if self._is_retryable_exception(e) and attempt < max_retries - 1:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        print(f"⚠️  API调用失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                        print(f"   等待 {delay:.1f} 秒后重试...")
                        time.sleep(delay)
                        continue
                    else:
                        raise

        elif self.provider == LLMProvider.CLAUDE:
            for attempt in range(max_retries):
                try:
                    _claude_kwargs = dict(kwargs)
                    _temp = temperature if temperature is not None else self.temperature
                    if self.enable_thinking:
                        _claude_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8000}
                        _temp = 1  # extended thinking 要求 temperature=1
                    response = self.sync_client.messages.create(
                        model=self.model,
                        max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
                        temperature=_temp,
                        system=json_system,
                        messages=[
                            {"role": "user", "content": json_user}
                        ],
                        **_claude_kwargs,
                    )
                    # thinking 模式下 content 可能有多个 block，取最后一个 text block
                    content = next(
                        (b.text for b in reversed(response.content) if hasattr(b, "text")),
                        response.content[0].text,
                    )
                    if _usage_tracker and hasattr(response, 'usage') and response.usage:
                        _usage_tracker.add_llm(response.usage.input_tokens, response.usage.output_tokens, self.model)
                    # 使用清洗函数解析 JSON
                    return self._clean_json_response(content)
                except Exception as e:
                    # P1-11: 指数退避重试
                    if self._is_retryable_exception(e) and attempt < max_retries - 1:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        print(f"⚠️  API调用失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                        print(f"   等待 {delay:.1f} 秒后重试...")
                        time.sleep(delay)
                        continue
                    else:
                        raise

        else:
            raise ValueError(f"不支持的提供商: {self.provider}")


if __name__ == "__main__":
    # 简单测试
    import sys
    if not HAS_DEPS:
        print("请安装依赖: pip install openai anthropic")
        sys.exit(1)

    # 检查环境变量
    if not os.getenv("OPENAI_API_KEY") and not os.getenv("ANTHROPIC_API_KEY"):
        print("警告: 未设置 API 密钥环境变量")
        print("设置 OPENAI_API_KEY 或 ANTHROPIC_API_KEY")

    print("LLM 客户端模块已加载")