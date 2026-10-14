from pathlib import Path
import re
import unittest

from services.story_planner.core.llm_client import LLMClient


class StoryPlannerLLMProviderDefaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service_dir = (
            Path(__file__).resolve().parents[2]
            / "services"
            / "story_planner"
        )

    def test_python_entry_points_do_not_default_to_qwen(self):
        forbidden_patterns = (
            re.compile(r'os\.getenv\(\s*["\']LLM_PROVIDER["\']\s*,\s*["\']qwen["\']\s*\)'),
            re.compile(r'provider\s*:\s*str\s*=\s*["\']qwen["\']'),
            re.compile(r'default\s*=\s*["\']qwen["\']'),
        )
        violations = []
        for path in self.service_dir.rglob("*.py"):
            content = path.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                if pattern.search(content):
                    violations.append(f"{path.relative_to(self.service_dir)}: {pattern.pattern}")

        self.assertEqual(
            violations,
            [],
            "story_planner 入口仍默认选择 qwen：\n" + "\n".join(violations),
        )

    def test_main_does_not_require_dashscope_key_for_default_run(self):
        content = (self.service_dir / "main.py").read_text(encoding="utf-8")

        self.assertNotIn('provider == "qwen"', content)
        self.assertNotIn("未找到 DASHSCOPE_API_KEY", content)

    def test_explicit_legacy_provider_is_normalized_to_gpt56_sol_proxy(self):
        client = LLMClient(
            provider="qwen",
            model="qwen-max",
            api_format="dashscope",
        )

        self.assertEqual(client.model, "gpt-5.6-sol")
        self.assertEqual(client.api_format, "openai")

    def test_story_planner_source_has_no_official_text_provider_credentials(self):
        forbidden_tokens = (
            "DASHSCOPE_API_KEY",
            "DEEPSEEK_API_KEY",
            "ANTHROPIC_API_KEY",
            "dashscope.Generation",
            "_DashScopeGeneration",
        )
        violations = []
        for path in self.service_dir.rglob("*.py"):
            content = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                if token in content:
                    violations.append(f"{path.relative_to(self.service_dir)}: {token}")

        self.assertEqual(
            violations,
            [],
            "story_planner 仍直接依赖官方文本模型凭证或 SDK：\n"
            + "\n".join(violations),
        )

    def test_original_and_multi_task_paths_ignore_legacy_provider_switch(self):
        tasks_source = (self.service_dir / "tasks.py").read_text(encoding="utf-8")
        required_functions = (
            "_strip_front_matter",
            "_parse_multi_mode",
            "_generate_summary_only_owned",
            "_generate_project_name_from_text",
            "_generate_summary_from_text",
        )
        for function_name in required_functions:
            match = re.search(
                rf"^def {function_name}\b.*?(?=^def |^@celery_app\.task|\Z)",
                tasks_source,
                flags=re.MULTILINE | re.DOTALL,
            )
            self.assertIsNotNone(match, function_name)
            function_source = match.group(0)
            self.assertNotIn("LLM_PROVIDER", function_source, function_name)
            self.assertIn("STORY_PLANNER_GATEWAY_PROVIDER", function_source, function_name)


if __name__ == "__main__":
    unittest.main()
