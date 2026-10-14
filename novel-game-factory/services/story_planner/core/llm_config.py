"""Centralized environment-driven LLM configuration for story_planner."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping, Optional

from dotenv import dotenv_values


DEFAULT_BASE_URL = "https://ai.farmmx.com/v1"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_API_FORMAT = "openai"
STORY_PLANNER_GATEWAY_PROVIDER = "openai"
DEFAULT_TIMEOUT = 1800
DEFAULT_LOCAL_ENV_PATH = Path(__file__).resolve().parents[1] / ".env.local"


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    base_url: str
    model: str
    api_format: str
    timeout: int


def _first_value(env: Mapping[str, str], *keys: str) -> str:
    for key in keys:
        value = env.get(key, "")
        if value and value.strip():
            return value.strip()
    return ""


def _parse_timeout(value: str) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return timeout if timeout > 0 else DEFAULT_TIMEOUT


def resolve_llm_settings(
    env: Optional[Mapping[str, str]] = None,
) -> LLMSettings:
    """Resolve the dedicated GPT-5.6-SOL proxy settings for story_planner.

    Generic OPENAI_*/LLM_* variables are intentionally ignored here. They may
    belong to other services or contain credentials for official providers;
    story_planner must only consume its own proxy credential namespace.
    """
    source = os.environ if env is None else env

    api_key = _first_value(source, "STORY_PLANNER_LLM_API_KEY")
    base_url = (
        _first_value(source, "STORY_PLANNER_LLM_BASE_URL")
        or DEFAULT_BASE_URL
    )
    model = DEFAULT_MODEL
    api_format = DEFAULT_API_FORMAT
    timeout = _parse_timeout(
        _first_value(source, "STORY_PLANNER_LLM_TIMEOUT")
    )

    return LLMSettings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        api_format=api_format,
        timeout=timeout,
    )


def load_llm_settings(
    env_file: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> LLMSettings:
    """Load an optional env file, while keeping process env at highest priority."""
    process_env = dict(os.environ if env is None else env)
    configured_path = process_env.get("STORY_PLANNER_ENV_FILE", "").strip()
    selected_path = Path(env_file) if env_file else (
        Path(configured_path) if configured_path else DEFAULT_LOCAL_ENV_PATH
    )

    merged_env = {}
    if selected_path.exists():
        merged_env.update(
            {
                key: value
                for key, value in dotenv_values(selected_path).items()
                if value is not None
            }
        )
    merged_env.update(process_env)
    return resolve_llm_settings(merged_env)
