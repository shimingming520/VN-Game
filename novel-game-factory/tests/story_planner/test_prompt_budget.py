import asyncio
import importlib
import importlib.util
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services.story_planner.core.llm_client import LLMClient


class PromptBudgetUnitTests(unittest.TestCase):
    def _module(self):
        module_name = "services.story_planner.core.prompt_budget"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "缺少 story_planner LLM 上下文预算模块",
        )
        return importlib.import_module(module_name)

    def test_default_budget_reserves_output_and_safety_tokens(self):
        module = self._module()

        settings = module.resolve_prompt_budget_settings({})
        input_budget = module.calculate_input_token_budget(
            max_output_tokens=16_000,
            settings=settings,
        )

        self.assertEqual(settings.context_window_tokens, 128_000)
        self.assertEqual(settings.max_input_tokens, 80_000)
        self.assertEqual(settings.safety_tokens, 8_000)
        self.assertEqual(input_budget, 80_000)
        self.assertLessEqual(
            input_budget + 16_000 + settings.safety_tokens,
            settings.context_window_tokens,
        )

    def test_budget_supports_environment_overrides(self):
        module = self._module()

        settings = module.resolve_prompt_budget_settings(
            {
                "STORY_PLANNER_LLM_CONTEXT_WINDOW_TOKENS": "100000",
                "STORY_PLANNER_LLM_MAX_INPUT_TOKENS": "60000",
                "STORY_PLANNER_LLM_PROMPT_SAFETY_TOKENS": "5000",
            }
        )

        self.assertEqual(settings.context_window_tokens, 100_000)
        self.assertEqual(settings.max_input_tokens, 60_000)
        self.assertEqual(settings.safety_tokens, 5_000)

    def test_estimator_reserves_extra_tokens_for_unicode_symbols(self):
        module = self._module()

        estimated = module.estimate_prompt_tokens("😀" * 10)

        self.assertGreaterEqual(estimated, 20)

    def test_default_budget_keeps_50000_chinese_character_worldview_segment(self):
        module = self._module()

        prepared = module.fit_prompts_to_budget(
            system_prompt="提取世界观",
            user_prompt="文" * 50_000,
            max_output_tokens=6_000,
            settings=module.resolve_prompt_budget_settings({}),
        )

        self.assertFalse(prepared.trimmed)
        self.assertLessEqual(
            prepared.estimated_input_tokens,
            prepared.input_token_budget,
        )

    def test_prompt_trimming_preserves_head_tail_and_budget(self):
        module = self._module()
        settings = module.PromptBudgetSettings(
            context_window_tokens=160,
            max_input_tokens=120,
            safety_tokens=10,
        )
        system_prompt = "SYSTEM_HEAD\n" + "系" * 160 + "\nSYSTEM_TAIL"
        user_prompt = "USER_HEAD\n" + "文" * 240 + "\nUSER_TAIL"

        prepared = module.fit_prompts_to_budget(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_output_tokens=30,
            settings=settings,
        )

        self.assertTrue(prepared.trimmed)
        self.assertLessEqual(prepared.estimated_input_tokens, 120)
        self.assertIn("SYSTEM_HEAD", prepared.system_prompt)
        self.assertIn("SYSTEM_TAIL", prepared.system_prompt)
        self.assertIn("USER_HEAD", prepared.user_prompt)
        self.assertIn("USER_TAIL", prepared.user_prompt)
        self.assertIn("上下文已按预算裁剪", prepared.system_prompt + prepared.user_prompt)

    def test_all_chinese_prompts_are_trimmed_by_estimated_tokens(self):
        module = self._module()
        settings = module.PromptBudgetSettings(
            context_window_tokens=160,
            max_input_tokens=120,
            safety_tokens=10,
        )

        prepared = module.fit_prompts_to_budget(
            system_prompt="系" * 200,
            user_prompt="文" * 300,
            max_output_tokens=30,
            settings=settings,
        )

        self.assertTrue(prepared.trimmed)
        self.assertLessEqual(
            prepared.estimated_input_tokens,
            prepared.input_token_budget,
        )

    def test_short_prompts_remain_unchanged(self):
        module = self._module()
        settings = module.PromptBudgetSettings(
            context_window_tokens=1000,
            max_input_tokens=800,
            safety_tokens=100,
        )

        prepared = module.fit_prompts_to_budget(
            system_prompt="system",
            user_prompt="user",
            max_output_tokens=100,
            settings=settings,
        )

        self.assertFalse(prepared.trimmed)
        self.assertEqual(prepared.system_prompt, "system")
        self.assertEqual(prepared.user_prompt, "user")

    def test_oversized_output_budget_is_capped_to_context_window(self):
        module = self._module()
        settings = module.PromptBudgetSettings(
            context_window_tokens=160,
            max_input_tokens=120,
            safety_tokens=10,
        )

        prepared = module.fit_prompts_to_budget(
            system_prompt="system",
            user_prompt="user",
            max_output_tokens=500,
            settings=settings,
        )

        self.assertTrue(
            hasattr(prepared, "max_output_tokens"),
            "预算结果必须包含收敛后的输出 Token 上限",
        )
        self.assertLess(prepared.max_output_tokens, 500)
        self.assertLessEqual(
            prepared.estimated_input_tokens
            + prepared.max_output_tokens
            + settings.safety_tokens,
            settings.context_window_tokens,
        )


class _FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=None,
        )


class _AsyncFakeCompletions(_FakeCompletions):
    async def create(self, **kwargs):
        return super().create(**kwargs)


class LLMClientPromptBudgetIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.prompt_budget_module = importlib.import_module(
            "services.story_planner.core.prompt_budget"
        )
        self.settings = self.prompt_budget_module.PromptBudgetSettings(
            context_window_tokens=160,
            max_input_tokens=120,
            safety_tokens=10,
        )
        self.system_prompt = "SYSTEM_HEAD\n" + "系" * 180 + "\nSYSTEM_TAIL"
        self.user_prompt = "USER_HEAD\n" + "文" * 260 + "\nUSER_TAIL"

    def _assert_request_is_bounded(self, completions):
        messages = completions.last_kwargs["messages"]
        prompt_tokens = sum(
            self.prompt_budget_module.estimate_prompt_tokens(message["content"])
            for message in messages
        )
        self.assertLessEqual(prompt_tokens, 120)
        self.assertEqual(completions.last_kwargs["max_tokens"], 30)
        self.assertIn(
            "上下文已按预算裁剪",
            "".join(message["content"] for message in messages),
        )

    def test_sync_text_call_applies_prompt_budget(self):
        completions = _FakeCompletions("ok")
        client = LLMClient()
        client._sync_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        with patch.object(
            self.prompt_budget_module,
            "PROMPT_BUDGET_SETTINGS",
            self.settings,
        ):
            result = client.chat_completion(
                self.system_prompt,
                self.user_prompt,
                max_tokens=30,
            )

        self.assertEqual(result, "ok")
        self._assert_request_is_bounded(completions)

    def test_sync_json_call_applies_prompt_budget(self):
        completions = _FakeCompletions('{"ok": true}')
        client = LLMClient()
        client._sync_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        with patch.object(
            self.prompt_budget_module,
            "PROMPT_BUDGET_SETTINGS",
            self.settings,
        ):
            result = client.chat_completion_json(
                self.system_prompt,
                self.user_prompt,
                max_tokens=30,
            )

        self.assertEqual(result, {"ok": True})
        self._assert_request_is_bounded(completions)

    def test_async_text_call_applies_prompt_budget(self):
        completions = _AsyncFakeCompletions("ok")
        client = LLMClient()
        client._async_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        async def run_call():
            with patch.object(
                self.prompt_budget_module,
                "PROMPT_BUDGET_SETTINGS",
                self.settings,
            ):
                return await client.async_chat_completion(
                    self.system_prompt,
                    self.user_prompt,
                    max_tokens=30,
                )

        self.assertEqual(asyncio.run(run_call()), "ok")
        self._assert_request_is_bounded(completions)

    def test_client_caps_oversized_output_budget(self):
        completions = _FakeCompletions("ok")
        client = LLMClient()
        client._sync_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        with patch.object(
            self.prompt_budget_module,
            "PROMPT_BUDGET_SETTINGS",
            self.settings,
        ):
            client.chat_completion(
                self.system_prompt,
                self.user_prompt,
                max_tokens=500,
            )

        prompt_tokens = sum(
            self.prompt_budget_module.estimate_prompt_tokens(message["content"])
            for message in completions.last_kwargs["messages"]
        )
        self.assertLess(completions.last_kwargs["max_tokens"], 500)
        self.assertLessEqual(
            prompt_tokens + completions.last_kwargs["max_tokens"] + 10,
            160,
        )


if __name__ == "__main__":
    unittest.main()
