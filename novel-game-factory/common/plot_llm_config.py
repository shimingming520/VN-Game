"""Resolve the LLM endpoint used by plot extraction.

Plot extraction historically had its own cluster-only default and a hard-coded
model.  Keep explicit plot overrides available, but make local deployments
inherit the same OpenAI-compatible text endpoint as the other services.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping


@dataclass(frozen=True)
class PlotLlmConfig:
    api_key: str
    base_url: str
    model: str


def _normalize_base_url(value: str) -> str:
    normalized = value.rstrip("/")
    return normalized if normalized.endswith("/v1") else f"{normalized}/v1"


def resolve_plot_llm_config(
    environ: Mapping[str, str] | None = None,
) -> PlotLlmConfig:
    """Resolve plot extraction credentials and model from deployment settings."""

    env = os.environ if environ is None else environ
    api_key = (
        env.get("PLOT_API_KEY")
        or env.get("GPT_IMAGE_API_KEY")
        or env.get("OPENAI_API_KEY")
        or env.get("DASHSCOPE_API_KEY")
        or ""
    )
    base_url = (
        env.get("PLOT_BASE_URL")
        or env.get("STORY_PLANNER_LLM_BASE_URL")
        or env.get("GPT_IMAGE_BASE_URL")
        or "http://sefutech-api.kube-test.svc:3000/v1"
    )
    model = (
        env.get("PLOT_MODEL")
        or env.get("LLM_MODEL")
        or env.get("QWEN_MODEL")
        or "qwen3.7-max"
    )
    return PlotLlmConfig(
        api_key=api_key,
        base_url=_normalize_base_url(base_url),
        model=model,
    )
