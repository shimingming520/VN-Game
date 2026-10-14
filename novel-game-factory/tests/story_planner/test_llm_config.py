import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


class LLMConfigTests(unittest.TestCase):
    def _load_module(self):
        module_path = (
            Path(__file__).resolve().parents[2]
            / "services"
            / "story_planner"
            / "core"
            / "llm_config.py"
        )
        self.assertTrue(
            module_path.exists(),
            "story_planner 统一 LLM 配置模块尚未实现",
        )
        spec = importlib.util.spec_from_file_location(
            "story_planner_llm_config_under_test",
            module_path,
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def _resolve_llm_settings(self, env):
        module = self._load_module()
        return module.resolve_llm_settings(env)

    def test_defaults_use_gpt56_sol_through_local_farmmx_gateway(self):
        settings = self._resolve_llm_settings({})

        self.assertEqual(settings.model, "gpt-5.6-sol")
        self.assertEqual(settings.base_url, "https://ai.farmmx.com/v1")
        self.assertEqual(settings.api_format, "openai")
        self.assertEqual(settings.api_key, "")
        self.assertEqual(settings.timeout, 1800)

    def test_generic_openai_environment_cannot_override_story_planner_gateway(self):
        settings = self._resolve_llm_settings(
            {
                "OPENAI_API_KEY": "official-key",
                "OPENAI_BASE_URL": "https://api.openai.com/v1",
                "OPENAI_TEXT_MODEL": "another-model",
                "OPENAI_TIMEOUT": "180",
            }
        )

        self.assertEqual(settings.api_key, "")
        self.assertEqual(settings.base_url, "https://ai.farmmx.com/v1")
        self.assertEqual(settings.model, "gpt-5.6-sol")
        self.assertEqual(settings.api_format, "openai")
        self.assertEqual(settings.timeout, 1800)

    def test_story_planner_specific_environment_has_highest_precedence(self):
        settings = self._resolve_llm_settings(
            {
                "STORY_PLANNER_LLM_API_KEY": "story-key",
                "STORY_PLANNER_LLM_BASE_URL": "https://story.example/v1",
                "STORY_PLANNER_LLM_MODEL": "qwen-max",
                "STORY_PLANNER_LLM_API_FORMAT": "dashscope",
                "STORY_PLANNER_LLM_TIMEOUT": "240",
                "OPENAI_API_KEY": "openai-key",
                "OPENAI_BASE_URL": "https://openai.example/v1",
                "OPENAI_TEXT_MODEL": "another-model",
                "OPENAI_TIMEOUT": "60",
            }
        )

        self.assertEqual(settings.api_key, "story-key")
        self.assertEqual(settings.base_url, "https://story.example/v1")
        self.assertEqual(settings.model, "gpt-5.6-sol")
        self.assertEqual(settings.api_format, "openai")
        self.assertEqual(settings.timeout, 240)

    def test_legacy_llm_environment_cannot_enable_an_official_provider(self):
        settings = self._resolve_llm_settings(
            {
                "LLM_API_KEY": "legacy-key",
                "LLM_BASE_URL": "https://dashscope.aliyuncs.com/api/v1",
                "LLM_MODEL": "qwen-max",
                "LLM_API_FORMAT": "dashscope",
                "DASHSCOPE_API_KEY": "official-key",
            }
        )

        self.assertEqual(settings.api_key, "")
        self.assertEqual(settings.base_url, "https://ai.farmmx.com/v1")
        self.assertEqual(settings.model, "gpt-5.6-sol")
        self.assertEqual(settings.api_format, "openai")

    def test_explicit_env_file_can_drive_local_story_planner_settings(self):
        module = self._load_module()
        self.assertTrue(
            hasattr(module, "load_llm_settings"),
            "尚未实现从本地 env 文件加载 story_planner LLM 配置",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env.local"
            env_path.write_text(
                "\n".join(
                    [
                        "STORY_PLANNER_LLM_API_KEY=test-key",
                        "STORY_PLANNER_LLM_BASE_URL=https://ai.farmmx.com/v1",
                        "STORY_PLANNER_LLM_MODEL=gpt-5.6-sol",
                        "STORY_PLANNER_LLM_API_FORMAT=openai",
                        "STORY_PLANNER_LLM_TIMEOUT=150",
                    ]
                ),
                encoding="utf-8",
            )

            settings = module.load_llm_settings(env_file=env_path, env={})

        self.assertEqual(settings.api_key, "test-key")
        self.assertEqual(settings.base_url, "https://ai.farmmx.com/v1")
        self.assertEqual(settings.model, "gpt-5.6-sol")
        self.assertEqual(settings.timeout, 150)


if __name__ == "__main__":
    unittest.main()
