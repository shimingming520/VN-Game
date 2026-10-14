from types import SimpleNamespace

import pytest

from services.scene_painter.core.cg_prompt_repair import (
    CGPromptRepairError,
    apply_replacements,
    repair_cg_prompt,
    validate_minimal_repair,
)
from services.scene_painter.image_providers.gpt_image import (
    GPTImageModerationError,
    GPTImageTextProvider,
    _moderation_error_from_response,
)


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat_completion_json(self, system_prompt, user_prompt, **kwargs):
        self.calls.append((system_prompt, user_prompt, kwargs))
        return self.payload


def test_minimal_repair_only_replaces_explicit_phrase():
    original = (
        "A child stares at the monster in a sauce courtyard, direct eye contact, "
        "single decisive moment, cinematic medium-wide composition"
    )
    llm = _FakeLLM(
        {
            "replacements": [
                {
                    "from": "monster",
                    "to": "eerie humanoid figure",
                    "reason": "neutral wording",
                }
            ]
        }
    )

    result = repair_cg_prompt(
        original,
        rejection_reason="moderation_blocked",
        protected_phrases=["single decisive moment", "direct eye contact"],
        llm_client=llm,
    )

    assert result.repaired_prompt == original.replace(
        "monster", "eerie humanoid figure"
    )
    assert result.original_prompt == original
    assert len(result.replacements) == 1
    assert llm.calls


def test_minimal_repair_rejects_replacement_covering_protected_semantics():
    with pytest.raises(CGPromptRepairError, match="不可变语义"):
        apply_replacements(
            "child stares at monster in one moment",
            [
                # This would change the event rather than one moderation phrase.
                SimpleNamespace(
                    source="child stares at monster in one moment",
                    target="different scene",
                )
            ],
            protected_phrases=["child stares at monster in one moment"],
        )


def test_minimal_repair_rejects_large_semantic_drift():
    with pytest.raises(CGPromptRepairError, match="相似度|长度"):
        validate_minimal_repair(
            "child stares at monster in a courtyard, direct eye contact",
            "an unrelated landscape",
            protected_phrases=[],
        )


def test_provider_parses_only_structured_moderation_block():
    response = SimpleNamespace(
        status_code=400,
        text=(
            '{"error":{"code":"moderation_blocked",'
            '"type":"safety","message":"blocked safety_violations=[monster]"}}'
        ),
    )
    response.json = lambda: {
        "error": {
            "code": "moderation_blocked",
            "type": "safety",
            "message": "blocked safety_violations=[monster]",
        }
    }

    error = _moderation_error_from_response(response)

    assert isinstance(error, GPTImageModerationError)
    assert error.code == "moderation_blocked"
    assert error.violations == ["monster"]


def test_provider_does_not_classify_generic_http_error_as_moderation():
    response = SimpleNamespace(
        status_code=500,
        text='{"error":{"code":"server_error","message":"try again"}}',
    )
    response.json = lambda: {
        "error": {"code": "server_error", "message": "try again"}
    }

    assert _moderation_error_from_response(response) is None


def test_provider_stops_same_prompt_retry_on_moderation(monkeypatch):
    calls = []

    class _Response:
        ok = False
        status_code = 400
        text = '{"error":{"code":"moderation_blocked","message":"blocked"}}'

        @staticmethod
        def json():
            return {
                "error": {
                    "code": "moderation_blocked",
                    "message": "blocked",
                }
            }

    def _post(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response()

    monkeypatch.setattr(
        "services.scene_painter.image_providers.gpt_image.requests.post", _post
    )
    provider = GPTImageTextProvider(
        api_key="test-key",
        base_url="https://example.invalid",
        max_retries=3,
        retry_delay=0,
    )

    with pytest.raises(GPTImageModerationError):
        provider.generate("a short CG prompt")

    assert len(calls) == 1
