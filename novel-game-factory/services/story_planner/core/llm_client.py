"""
LLM 客户端封装。

所有文本模型默认统一走 farmmx 提供的 OpenAI 兼容网关：
    base_url = https://ai.farmmx.com/v1
    model    = gpt-5.6-sol
    api_key  = 仅通过环境变量或 services/story_planner/.env.local 注入

为了不影响上层模块（world_analyst / story_generator / ... 仍可能传入 provider
兼容标签），保留 LLMProvider 枚举；默认协议为 OpenAI 兼容 chat/completions。

旧的 provider/model/api_format 参数仅为调用方兼容保留，实际统一固定为
farmmx OpenAI 兼容接口上的 gpt-5.6-sol。
"""
import os
import asyncio
import json
import logging
import random
import re
import time
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, Union
from enum import Enum
from json_repair import repair_json
from .llm_config import load_llm_settings
from . import prompt_budget
from . import llm_resilience


logger = logging.getLogger(__name__)

try:
    import sys as _sys
    _project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _project_root not in _sys.path:
        _sys.path.insert(0, _project_root)
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

try:
    from openai import OpenAI, AsyncOpenAI
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

# ──────────────────────────────────────────────────────────────
# 统一网关配置。所有凭证仅从环境变量读取，不在源码中提供默认值。
# ──────────────────────────────────────────────────────────────
_LLM_SETTINGS = load_llm_settings()
FARMMX_BASE_URL = _LLM_SETTINGS.base_url
FARMMX_API_KEY = _LLM_SETTINGS.api_key
DEFAULT_MODEL = _LLM_SETTINGS.model

# story_planner 只允许 OpenAI 兼容协议，避免旧环境变量绕回官方服务。
DEFAULT_API_FORMAT = _LLM_SETTINGS.api_format


class LLMProvider(Enum):
    """支持的 LLM 提供商（仅作为兼容标签，全部路由到同一个网关）"""
    OPENAI = "openai"
    CLAUDE = "claude"
    QWEN = "qwen"


class LLMClient:
    """
    LLM 客户端封装。

    默认通过 OpenAI 兼容接口访问配置的中转网关。provider 参数仅作兼容保留。

    推荐环境变量：
    - STORY_PLANNER_LLM_BASE_URL: 网关地址，默认 https://ai.farmmx.com/v1
    - STORY_PLANNER_LLM_API_KEY:  网关 API Key，无源码默认值
    - STORY_PLANNER_LLM_TIMEOUT:  请求超时秒数，默认 1800

    模型和协议固定为 gpt-5.6-sol / openai。STORY_PLANNER_LLM_MODEL、
    STORY_PLANNER_LLM_API_FORMAT、通用 OPENAI_* 及历史 LLM_* 不参与路由，
    防止共享环境配置把本模块切回官方服务。
    """

    def __init__(
        self,
        provider: Union[LLMProvider, str] = LLMProvider.OPENAI,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        api_format: Optional[str] = None,
    ):
        """
        Args:
            api_format: 兼容旧调用签名；传入值会被忽略，始终使用 openai。
        """
        if not HAS_DEPS:
            raise ImportError("请安装必要的依赖：pip install openai")

        if isinstance(provider, str):
            try:
                provider = LLMProvider(provider.lower())
            except ValueError:
                provider = LLMProvider.OPENAI

        self.provider = provider
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.model = DEFAULT_MODEL
        self.api_format = DEFAULT_API_FORMAT

        self._sync_client = None
        self._async_client = None

    @property
    def sync_client(self):
        if self._sync_client is None:
            self._sync_client = OpenAI(
                base_url=FARMMX_BASE_URL,
                api_key=FARMMX_API_KEY,
                timeout=_LLM_SETTINGS.timeout,
                max_retries=0,
            )
        return self._sync_client

    @property
    def async_client(self):
        if self._async_client is None:
            self._async_client = AsyncOpenAI(
                base_url=FARMMX_BASE_URL,
                api_key=FARMMX_API_KEY,
                timeout=_LLM_SETTINGS.timeout,
                max_retries=0,
            )
        return self._async_client

    def _is_retryable_exception(self, exc: Exception) -> bool:
        """判断异常是否可重试。"""
        return llm_resilience.classify_llm_error(exc).retryable

    def _is_retryable_status_code(self, status_code: int) -> bool:
        error = RuntimeError(f"HTTP {status_code}")
        error.status_code = status_code
        return llm_resilience.classify_llm_error(error).retryable

    @staticmethod
    def _response_field(response: Any, name: str, default: Any = None) -> Any:
        if isinstance(response, dict):
            return response.get(name, default)
        value = getattr(response, name, default)
        if value is not default:
            return value
        model_extra = getattr(response, "model_extra", None)
        if isinstance(model_extra, dict):
            return model_extra.get(name, default)
        return default

    @classmethod
    def _response_protocol_diagnostics(
        cls,
        response: Any,
        *,
        invalid_reason: str,
    ) -> Dict[str, Any]:
        if isinstance(response, dict):
            body_keys = sorted(str(key) for key in response.keys())
        else:
            fields_set = getattr(response, "model_fields_set", None)
            if fields_set:
                body_keys = sorted(str(key) for key in fields_set)
            else:
                model_extra = getattr(response, "model_extra", None)
                body_keys = (
                    sorted(str(key) for key in model_extra.keys())
                    if isinstance(model_extra, dict)
                    else []
                )

        choices = cls._response_field(response, "choices")
        try:
            choices_count = len(choices) if choices is not None else None
        except TypeError:
            choices_count = None

        message = None
        if isinstance(choices, (list, tuple)) and choices:
            message = cls._response_field(choices[0], "message")
        content = cls._response_field(message, "content") if message is not None else None

        gateway_error = cls._response_field(response, "error")
        error_type = cls._response_field(gateway_error, "type")
        error_code = cls._response_field(gateway_error, "code")
        error_message = cls._response_field(gateway_error, "message")

        request_id = (
            cls._response_field(response, "_request_id")
            or cls._response_field(response, "request_id")
            or cls._response_field(response, "id")
            or "missing"
        )
        return {
            "http_status": 200,
            "invalid_reason": invalid_reason,
            "body_keys": body_keys,
            "request_id": str(request_id),
            "choices_type": type(choices).__name__,
            "choices_count": choices_count,
            "message_present": message is not None,
            "content_type": type(content).__name__ if message is not None else "missing",
            "content_length": len(content) if isinstance(content, str) else 0,
            "error_type": str(error_type or "none"),
            "error_code": str(error_code) if error_code is not None else "none",
            "error_message": re.sub(r"\s+", " ", str(error_message or "none")).strip()[:500],
        }

    @staticmethod
    def _log_protocol_error(
        operation_name: str,
        exc: llm_resilience.LLMInvalidResponseError,
        *,
        attempt: int,
        max_attempts: int,
    ) -> None:
        details = getattr(exc, "diagnostics", {}) or {}
        logger.warning(
            "LLM响应协议异常: operation=%s http_status=%s invalid_reason=%s "
            "body_keys=%s request_id=%s choices_type=%s choices_count=%s "
            "message_present=%s content_type=%s content_length=%s "
            "error_type=%s error_code=%s error_message=%s attempt=%s/%s",
            operation_name,
            details.get("http_status", "unknown"),
            details.get("invalid_reason", "unknown"),
            details.get("body_keys", []),
            details.get("request_id", "missing"),
            details.get("choices_type", "unknown"),
            details.get("choices_count"),
            details.get("message_present", False),
            details.get("content_type", "unknown"),
            details.get("content_length", 0),
            details.get("error_type", "none"),
            details.get("error_code", "none"),
            details.get("error_message", "none"),
            attempt,
            max_attempts,
        )

    @classmethod
    def _extract_chat_content(cls, response: Any) -> str:
        """Validate an OpenAI-compatible response before leaving retry scope."""
        if response is None:
            raise llm_resilience.LLMInvalidResponseError(
                "LLM HTTP 响应为空",
                diagnostics={
                    "http_status": "unknown",
                    "invalid_reason": "response_none",
                    "body_keys": [],
                },
            )
        choices = cls._response_field(response, "choices")
        if choices is None:
            gateway_error = cls._response_field(response, "error")
            invalid_reason = (
                "http_200_error_without_choices"
                if gateway_error is not None
                else "choices_none_or_missing"
            )
            raise llm_resilience.LLMInvalidResponseError(
                "LLM HTTP 200 但 choices 为空",
                diagnostics=cls._response_protocol_diagnostics(
                    response,
                    invalid_reason=invalid_reason,
                ),
            )
        if not choices:
            raise llm_resilience.LLMInvalidResponseError(
                "LLM HTTP 200 但 choices 为空数组",
                diagnostics=cls._response_protocol_diagnostics(
                    response,
                    invalid_reason="choices_empty",
                ),
            )
        message = cls._response_field(choices[0], "message")
        if message is None:
            raise llm_resilience.LLMInvalidResponseError(
                "LLM HTTP 200 但 choices[0].message 为空",
                diagnostics=cls._response_protocol_diagnostics(
                    response,
                    invalid_reason="message_missing",
                ),
            )
        content = cls._response_field(message, "content")
        if not isinstance(content, str) or not content.strip():
            raise llm_resilience.LLMInvalidResponseError(
                "LLM HTTP 200 但 message.content 为空",
                diagnostics=cls._response_protocol_diagnostics(
                    response,
                    invalid_reason="content_empty_or_non_string",
                ),
            )
        return content.strip()

    def _execute_sync_with_resilience(self, operation, operation_name: str):
        settings = llm_resilience.RESILIENCE_SETTINGS
        for attempt in range(settings.max_attempts):
            try:
                permit = llm_resilience.acquire_llm_slot_sync()
                try:
                    return operation()
                finally:
                    llm_resilience.release_llm_slot_sync(permit)
            except Exception as exc:
                if isinstance(exc, llm_resilience.LLMInvalidResponseError):
                    self._log_protocol_error(
                        operation_name,
                        exc,
                        attempt=attempt + 1,
                        max_attempts=settings.max_attempts,
                    )
                classification = llm_resilience.classify_llm_error(exc)
                if not classification.retryable or attempt >= settings.max_attempts - 1:
                    logger.error(
                        "LLM 调用失败: operation=%s, category=%s, status=%s, "
                        "attempt=%s/%s, retryable=%s",
                        operation_name,
                        classification.category,
                        classification.status_code,
                        attempt + 1,
                        settings.max_attempts,
                        classification.retryable,
                    )
                    raise
                delay = llm_resilience.calculate_retry_delay(
                    attempt,
                    settings=settings,
                    retry_after_seconds=classification.retry_after_seconds,
                    jitter_sample=random.random(),
                )
                logger.warning(
                    "LLM 调用暂时失败，准备重试: operation=%s, category=%s, "
                    "status=%s, attempt=%s/%s, delay_seconds=%.3f",
                    operation_name,
                    classification.category,
                    classification.status_code,
                    attempt + 1,
                    settings.max_attempts,
                    delay,
                )
                time.sleep(delay)
        raise RuntimeError("LLM 同步重试循环异常结束")

    async def _execute_async_with_resilience(self, operation, operation_name: str):
        settings = llm_resilience.RESILIENCE_SETTINGS
        for attempt in range(settings.max_attempts):
            try:
                permit = await llm_resilience.acquire_llm_slot_async()
                try:
                    return await operation()
                finally:
                    await llm_resilience.release_llm_slot_async(permit)
            except Exception as exc:
                if isinstance(exc, llm_resilience.LLMInvalidResponseError):
                    self._log_protocol_error(
                        operation_name,
                        exc,
                        attempt=attempt + 1,
                        max_attempts=settings.max_attempts,
                    )
                classification = llm_resilience.classify_llm_error(exc)
                if not classification.retryable or attempt >= settings.max_attempts - 1:
                    logger.error(
                        "LLM 异步调用失败: operation=%s, category=%s, status=%s, "
                        "attempt=%s/%s, retryable=%s",
                        operation_name,
                        classification.category,
                        classification.status_code,
                        attempt + 1,
                        settings.max_attempts,
                        classification.retryable,
                    )
                    raise
                delay = llm_resilience.calculate_retry_delay(
                    attempt,
                    settings=settings,
                    retry_after_seconds=classification.retry_after_seconds,
                    jitter_sample=random.random(),
                )
                logger.warning(
                    "LLM 异步调用暂时失败，准备重试: operation=%s, category=%s, "
                    "status=%s, attempt=%s/%s, delay_seconds=%.3f",
                    operation_name,
                    classification.category,
                    classification.status_code,
                    attempt + 1,
                    settings.max_attempts,
                    delay,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("LLM 异步重试循环异常结束")

    def _fit_request_prompts(
        self,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> tuple[str, str, int]:
        prepared = prompt_budget.fit_prompts_to_budget(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_output_tokens=max_output_tokens,
            settings=prompt_budget.PROMPT_BUDGET_SETTINGS,
        )
        if prepared.trimmed:
            logger.warning(
                "LLM 提示词超过上下文预算，已裁剪: original_chars=%s, "
                "fitted_tokens=%s, input_budget=%s, output_tokens=%s",
                len(str(system_prompt or "")) + len(str(user_prompt or "")),
                prepared.estimated_input_tokens,
                prepared.input_token_budget,
                prepared.max_output_tokens,
            )
        if prepared.max_output_tokens != max_output_tokens:
            logger.warning(
                "LLM 输出 Token 额度超过上下文预算，已收紧: requested=%s, "
                "fitted=%s, context_window=%s, safety=%s",
                max_output_tokens,
                prepared.max_output_tokens,
                prompt_budget.PROMPT_BUDGET_SETTINGS.context_window_tokens,
                prompt_budget.PROMPT_BUDGET_SETTINGS.safety_tokens,
            )
        return (
            prepared.system_prompt,
            prepared.user_prompt,
            prepared.max_output_tokens,
        )

    def _call_gemini(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        timeout: int,
    ) -> str:
        """
        调用 farmmx 的 Gemini 原生路径 /v1beta/models/{model}:generateContent。
        farmmx 该路径只接 contents 格式，OpenAI 转换路径在该模型上不可用。

        返回纯文本（已合并所有 parts）。usage 同步到 _usage_tracker。
        重试逻辑与 OpenAI 路径一致。
        """
        # 把 base_url 末尾的 /v1 去掉，拼到 /v1beta/...
        api_base = FARMMX_BASE_URL.rstrip("/")
        if api_base.endswith("/v1"):
            api_base = api_base[:-3]
        url = f"{api_base}/v1beta/models/{self.model}:generateContent"

        # Gemini 没有独立的 system 槽位（farmmx 的 NewAPI 上常做 system_instruction，
        # 但兼容性差异大），最稳的做法是把 system 拼到 user 前面。
        merged_user = (
            f"{system_prompt}\n\n{user_prompt}"
            if system_prompt
            else user_prompt
        )
        body = {
            "contents": [
                {"role": "user", "parts": [{"text": merged_user}]}
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {FARMMX_API_KEY}",
            "Content-Type": "application/json",
        }

        max_retries = 5
        base_delay = 2
        max_delay = 60

        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(url, data=body_bytes, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                # 提取文本：candidates[0].content.parts[*].text
                candidates = data.get("candidates") or []
                if not candidates:
                    raise RuntimeError(
                        f"Gemini 响应缺少 candidates: {str(data)[:200]}"
                    )
                content_obj = candidates[0].get("content") or {}
                parts = content_obj.get("parts") or []
                text = "".join(p.get("text", "") for p in parts)

                # usage：farmmx 透传 Google 的 usageMetadata
                usage = data.get("usageMetadata") or {}
                if _usage_tracker and usage:
                    _usage_tracker.add_llm_usage(
                        usage, self.model, provider="gemini"
                    )

                if not text:
                    finish = candidates[0].get("finishReason")
                    raise RuntimeError(
                        f"Gemini 返回空文本 (finishReason={finish})"
                    )
                return text

            except urllib.error.HTTPError as e:
                err_body = ""
                try:
                    err_body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                if self._is_retryable_status_code(e.code) and attempt < max_retries - 1:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    print(f"⚠️  Gemini API 失败 (尝试 {attempt + 1}/{max_retries}): "
                          f"HTTP {e.code} {err_body[:200]}")
                    print(f"   等待 {delay:.1f} 秒后重试...")
                    time.sleep(delay)
                    continue
                raise RuntimeError(
                    f"Gemini API HTTP {e.code}: {err_body[:300]}"
                ) from e

            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < max_retries - 1:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    print(f"⚠️  Gemini 网络异常 (尝试 {attempt + 1}/{max_retries}): {e}")
                    print(f"   等待 {delay:.1f} 秒后重试...")
                    time.sleep(delay)
                    continue
                raise

            except Exception as e:
                if self._is_retryable_exception(e) and attempt < max_retries - 1:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    print(f"⚠️  Gemini 调用失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                    print(f"   等待 {delay:.1f} 秒后重试...")
                    time.sleep(delay)
                    continue
                raise

    def _clean_json_response(self, content: str) -> Dict[str, Any]:
        """清洗并解析 JSON 响应。支持 Markdown 代码块和多余尾部文本。"""
        def parse_json_string(text: str) -> Any:
            text = re.sub(r'^\s*\[\s*\]\s*,?\s*', '', text, flags=re.MULTILINE)

            try:
                return repair_json(text, return_objects=True)
            except Exception:
                pass

            json_match = re.search(r'```(?:json)?\s*(\[{.*?}\]|\{.*?\})\s*```', text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
                try:
                    return repair_json(json_str, return_objects=True)
                except json.JSONDecodeError:
                    pass

            start_obj = text.find('{')
            if start_obj != -1:
                brace_count = 0
                end_obj = -1
                in_string = False
                escape_next = False

                for i in range(start_obj, len(text)):
                    char = text[i]
                    if escape_next:
                        escape_next = False
                        continue
                    if char == '\\':
                        escape_next = True
                        continue
                    if char == '"':
                        in_string = not in_string
                        continue
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
                        print(f"JSON 解析失败: {e}")
                        print(f"尝试修复JSON...")
                        try:
                            for trim_len in range(50, min(5000, len(json_str)), 50):
                                trimmed = json_str[:-trim_len]
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

            start_arr = text.find('[')
            end_arr = text.rfind(']')
            if start_arr != -1 and end_arr != -1 and end_arr > start_arr:
                json_str = text[start_arr:end_arr + 1]
                if json_str.strip() == '[]':
                    raise ValueError(f"只找到空数组，未找到有效的 JSON 对象: {text[:200]}...")
                try:
                    return repair_json(json_str, return_objects=True)
                except json.JSONDecodeError as e:
                    print(f"JSON 清洗失败: {e}")
                    print(f"问题片段: {json_str[:200]}...")
                    raise ValueError(f"无法解析有效的 JSON: {text[:200]}...")

            raise ValueError(f"未找到有效的 JSON 结构: {text[:200]}...")

        parsed = parse_json_string(content)

        if isinstance(parsed, dict):
            return parsed
        elif isinstance(parsed, list):
            return {"data": parsed}
        else:
            return {"value": parsed}

    def chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
        **kwargs,
    ) -> str:
        """同步聊天补全。所有 provider 统一通过 farmmx OpenAI 兼容接口。"""
        temp = temperature if temperature is not None else self.temperature
        tokens = max_tokens if max_tokens is not None else self.max_tokens
        request_timeout = timeout if timeout is not None else _LLM_SETTINGS.timeout
        system_prompt, user_prompt, tokens = self._fit_request_prompts(
            system_prompt,
            user_prompt,
            tokens,
        )

        # api_format=="gemini" 走原生 generateContent 路径
        if self.api_format == "gemini":
            return self._call_gemini(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temp,
                max_tokens=tokens,
                timeout=request_timeout,
            )

        def _request():
            response = self.sync_client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temp,
                max_tokens=tokens,
                timeout=request_timeout,
                **kwargs,
            )
            return response, self._extract_chat_content(response)

        response, content = self._execute_sync_with_resilience(
            _request, "chat_completion"
        )
        if _usage_tracker and hasattr(response, 'usage') and response.usage:
            _usage_tracker.add_llm_usage(
                response.usage, self.model, provider="openai_compatible"
            )
        return content

    async def async_chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
        **kwargs,
    ) -> str:
        """异步聊天补全。"""
        temp = temperature if temperature is not None else self.temperature
        tokens = max_tokens if max_tokens is not None else self.max_tokens
        request_timeout = timeout if timeout is not None else _LLM_SETTINGS.timeout
        system_prompt, user_prompt, tokens = self._fit_request_prompts(
            system_prompt,
            user_prompt,
            tokens,
        )

        # api_format=="gemini" 没有官方异步路径，复用同步原生路径
        if self.api_format == "gemini":
            return await asyncio.to_thread(
                self._call_gemini,
                system_prompt,
                user_prompt,
                temp,
                tokens,
                request_timeout,
            )

        async def _request():
            response = await self.async_client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temp,
                max_tokens=tokens,
                timeout=request_timeout,
                **kwargs,
            )
            return response, self._extract_chat_content(response)

        response, content = await self._execute_async_with_resilience(
            _request,
            "async_chat_completion",
        )
        if _usage_tracker and hasattr(response, 'usage') and response.usage:
            _usage_tracker.add_llm_usage(
                response.usage, self.model, provider="openai_compatible"
            )
        return content

    def chat_completion_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """同步聊天补全，返回 JSON。"""
        json_system = system_prompt + "\n\n请返回有效的 JSON 格式。"
        json_user = user_prompt + "\n\n请确保输出是有效的 JSON。"
        request_timeout = timeout if timeout is not None else _LLM_SETTINGS.timeout
        tokens = max_tokens if max_tokens is not None else self.max_tokens
        json_system, json_user, tokens = self._fit_request_prompts(
            json_system,
            json_user,
            tokens,
        )

        # api_format=="gemini" 走原生路径
        if self.api_format == "gemini":
            content = self._call_gemini(
                system_prompt=json_system,
                user_prompt=json_user,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=tokens,
                timeout=request_timeout,
            )
            return self._clean_json_response(content)

        def _request():
            response = self.sync_client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": json_system},
                    {"role": "user", "content": json_user},
                ],
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=tokens,
                timeout=request_timeout,
                **kwargs,
            )
            return response, self._extract_chat_content(response)

        response, content = self._execute_sync_with_resilience(
            _request,
            "chat_completion_json",
        )
        if _usage_tracker and hasattr(response, 'usage') and response.usage:
            _usage_tracker.add_llm_usage(
                response.usage, self.model, provider="openai_compatible"
            )
        return self._clean_json_response(content)


def create_llm_client(
    provider: str = "openai",
    model: Optional[str] = None,
    api_format: Optional[str] = None,
    **kwargs,
) -> LLMClient:
    """
    创建 LLM 客户端（provider 参数仅作兼容标签，实际全部走 farmmx 网关）。

    Args:
        api_format: 兼容旧调用签名；实际固定使用 openai。
    """
    try:
        provider_enum = LLMProvider(provider)
    except ValueError:
        provider_enum = LLMProvider.OPENAI
    return LLMClient(
        provider=provider_enum,
        model=model,
        api_format=api_format,
        **kwargs,
    )


# ──────────────────────────────────────────────────────────────
# 语言指令工具（供所有生成模块共用）
# ──────────────────────────────────────────────────────────────

_LANG_INSTRUCTIONS: dict = {
    "en-US": "IMPORTANT: Write ALL output in English. Every line of dialogue, narration, and description must be in English.",
    "en-GB": "IMPORTANT: Write ALL output in English. Every line of dialogue, narration, and description must be in English.",
    "ja-JP": "IMPORTANT: Write ALL output in Japanese. Every line of dialogue, narration, and description must be in Japanese.",
    "zh-TW": "IMPORTANT: Write ALL output in Traditional Chinese. Every line of dialogue, narration, and description must be in Traditional Chinese.",
    "ko-KR": "IMPORTANT: Write ALL output in Korean. Every line of dialogue, narration, and description must be in Korean.",
    "fr-FR": "IMPORTANT: Write ALL output in French. Every line of dialogue, narration, and description must be in French.",
    "de-DE": "IMPORTANT: Write ALL output in German. Every line of dialogue, narration, and description must be in German.",
    "es-ES": "IMPORTANT: Write ALL output in Spanish. Every line of dialogue, narration, and description must be in Spanish.",
}


def get_lang_instruction(output_language: Optional[str]) -> str:
    """返回追加到 system_prompt 末尾的语言指令字符串（zh-CN 返回空串）。"""
    if not output_language:
        return ""
    return _LANG_INSTRUCTIONS.get(output_language, "")


print("LLM 客户端模块已加载（统一网关：farmmx OpenAI 兼容接口）")
