"""LLM 主备模型切换。

主备模型走同一个中转站（同 base_url / api_key）、同一套 OpenAI Chat Completions
参数，切换时只替换 ``model`` 字符串，调用方的 messages/temperature 等原样透传。

用途是应对单个模型突然不稳定：主模型报错时立刻用备选模型重发同一个请求，
连续失败到阈值后本进程后续请求直接走备选。切走之后不再回头探测主模型——
备选模型能正常出活就一直用它，避免为了切回去而周期性地重新踩坏掉的主模型。
两个模型都失败才抛出异常，交给调用方原有的 tenacity 重试或 except 分支处理。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, List, Optional, Tuple


# 默认备选模型。主模型本身就是它时，用 ALTERNATE_FALLBACK_MODEL 作为备选。
DEFAULT_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "gpt-5.6-sol")
ALTERNATE_FALLBACK_MODEL = os.getenv("LLM_ALTERNATE_FALLBACK_MODEL", "qwen3.7-max")

# 主模型连续失败多少次后，本进程后续请求直接走备选。
# 设为 1 会让一次网络抖动就切走，设得太大则坏模型要被反复撞很多次。
DEFAULT_STICK_AFTER = int(os.getenv("LLM_FALLBACK_STICK_AFTER", "3"))


def resolve_fallback_model(primary: str) -> str:
    """主模型对应的备选模型名。"""
    if primary == DEFAULT_FALLBACK_MODEL:
        return ALTERNATE_FALLBACK_MODEL
    return DEFAULT_FALLBACK_MODEL


class ModelFallback:
    """按 主模型 -> 备选模型 的顺序发同一个请求。

    实例是进程级共享状态（记录主模型连续失败次数），各模块在导入时创建一个即可。
    """

    def __init__(
        self,
        primary: str,
        fallback: Optional[str] = None,
        *,
        name: str = "",
        logger: Optional[logging.Logger] = None,
        stick_after: int = DEFAULT_STICK_AFTER,
    ) -> None:
        self.primary = primary
        self.fallback = fallback if fallback is not None else resolve_fallback_model(primary)
        self.name = name or "llm"
        self.logger = logger or logging.getLogger(__name__)
        self.stick_after = max(1, stick_after)
        self._lock = threading.Lock()
        self._primary_failures = 0
        self._switched = False

    # ---------------- 内部状态 ----------------

    def _model_order(self) -> List[str]:
        """本次请求要依次尝试的模型名，已去重。

        已切到备选后主模型不再排进来：备选正常工作时就一直用备选。
        """
        with self._lock:
            switched = self._switched
        order = [self.fallback] if switched else [self.primary, self.fallback]
        return [m for i, m in enumerate(order) if m and m not in order[:i]]

    def _note_failure(self, model: str) -> None:
        if model != self.primary:
            return
        with self._lock:
            self._primary_failures += 1
            failures = self._primary_failures
            newly_switched = not self._switched and failures >= self.stick_after
            if newly_switched:
                self._switched = True
        if newly_switched:
            self.logger.warning(
                f"⚠️ [{self.name}] 主模型 {self.primary} 连续失败 {failures} 次，"
                f"本进程后续请求固定使用备选模型 {self.fallback}"
            )

    def _log_switch(self, failed_model: str, next_model: str, exc: Exception) -> None:
        self.logger.warning(
            f"⚠️ [{self.name}] 模型 {failed_model} 调用失败，改用 {next_model} 重发: {exc}"
        )

    # ---------------- 调用入口 ----------------

    def chat(self, client: Any, **kwargs: Any) -> Tuple[Any, str]:
        """同步调用，返回 (response, 实际使用的模型名)。"""
        models = self._model_order()
        last_exc: Optional[Exception] = None
        for index, model in enumerate(models):
            try:
                response = client.chat.completions.create(model=model, **kwargs)
            except Exception as exc:
                last_exc = exc
                self._note_failure(model)
                if index + 1 < len(models):
                    self._log_switch(model, models[index + 1], exc)
                continue
            return response, model
        raise last_exc  # type: ignore[misc]

    async def achat(self, client: Any, **kwargs: Any) -> Tuple[Any, str]:
        """异步调用，返回 (response, 实际使用的模型名)。"""
        models = self._model_order()
        last_exc: Optional[Exception] = None
        for index, model in enumerate(models):
            try:
                response = await client.chat.completions.create(model=model, **kwargs)
            except Exception as exc:
                last_exc = exc
                self._note_failure(model)
                if index + 1 < len(models):
                    self._log_switch(model, models[index + 1], exc)
                continue
            return response, model
        raise last_exc  # type: ignore[misc]
