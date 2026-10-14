from pathlib import Path
import unittest


class StoryPlannerEnvTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service_dir = (
            Path(__file__).resolve().parents[2]
            / "services"
            / "story_planner"
        )

    def _read_template(self, name):
        path = self.service_dir / name
        self.assertTrue(path.exists(), f"缺少环境变量模板: {name}")
        return path.read_text(encoding="utf-8")

    def test_local_template_targets_public_farmmx_gateway(self):
        content = self._read_template(".env.local.example")

        self.assertIn("https://ai.farmmx.com/v1", content)
        self.assertIn("gpt-5.6-sol", content)
        self.assertIn("STORY_PLANNER_LLM_API_KEY=", content)
        self.assertNotIn("STORY_PLANNER_LLM_API_KEY=sk-", content)

    def test_k8s_template_targets_cluster_gateway(self):
        content = self._read_template(".env.k8s.example")

        self.assertIn("http://172.16.30.31:32691/v1", content)
        self.assertIn("gpt-5.6-sol", content)
        self.assertIn("STORY_PLANNER_LLM_API_KEY=", content)
        self.assertNotIn("STORY_PLANNER_LLM_API_KEY=sk-", content)

    def test_templates_include_openai_proxy_test_aliases(self):
        for name in (".env.local.example", ".env.k8s.example"):
            with self.subTest(template=name):
                content = self._read_template(name)
                self.assertIn("OPENAI_API_KEY=${STORY_PLANNER_LLM_API_KEY}", content)
                self.assertIn("OPENAI_BASE_URL=${STORY_PLANNER_LLM_BASE_URL}", content)
                self.assertIn("OPENAI_TEXT_MODEL=${STORY_PLANNER_LLM_MODEL}", content)

    def test_templates_include_llm_prompt_budget(self):
        expected_values = (
            "STORY_PLANNER_LLM_CONTEXT_WINDOW_TOKENS=128000",
            "STORY_PLANNER_LLM_MAX_INPUT_TOKENS=80000",
            "STORY_PLANNER_LLM_PROMPT_SAFETY_TOKENS=8000",
        )
        for name in (".env.local.example", ".env.k8s.example"):
            with self.subTest(template=name):
                content = self._read_template(name)
                for expected in expected_values:
                    self.assertIn(expected, content)

    def test_templates_include_llm_resilience_settings(self):
        expected_values = (
            "STORY_PLANNER_LLM_MAX_ATTEMPTS=5",
            "STORY_PLANNER_LLM_BACKOFF_BASE_SECONDS=2",
            "STORY_PLANNER_LLM_BACKOFF_MAX_SECONDS=60",
            "STORY_PLANNER_LLM_BACKOFF_JITTER_RATIO=0.2",
            "STORY_PLANNER_LLM_REQUESTS_PER_MINUTE=60",
        )
        for name in (".env.local.example", ".env.k8s.example"):
            with self.subTest(template=name):
                content = self._read_template(name)
                for expected in expected_values:
                    self.assertIn(expected, content)

    def test_templates_include_cluster_llm_backpressure(self):
        local_content = self._read_template(".env.local.example")
        k8s_content = self._read_template(".env.k8s.example")
        shared_values = (
            "STORY_PLANNER_LLM_GLOBAL_REQUESTS_PER_MINUTE=60",
            "STORY_PLANNER_LLM_GLOBAL_MAX_CONCURRENCY=4",
            "STORY_PLANNER_LLM_BACKPRESSURE_MAX_WAIT_SECONDS=900",
            "STORY_PLANNER_LLM_BACKPRESSURE_POLL_SECONDS=0.5",
            "STORY_PLANNER_LLM_CONCURRENCY_LEASE_SECONDS=1800",
        )

        self.assertIn("STORY_PLANNER_LLM_GLOBAL_LIMIT_ENABLED=0", local_content)
        self.assertIn("STORY_PLANNER_LLM_GLOBAL_LIMIT_ENABLED=1", k8s_content)
        for content in (local_content, k8s_content):
            for expected in shared_values:
                self.assertIn(expected, content)

    def test_templates_include_versioned_cache_policy(self):
        local_content = self._read_template(".env.local.example")
        k8s_content = self._read_template(".env.k8s.example")

        self.assertIn(
            "STORY_PLANNER_CACHE_DIR=services/story_planner/.cache/chapter_branch",
            local_content,
        )
        self.assertIn(
            "STORY_PLANNER_CACHE_DIR=/storage/story_planner/cache/chapter_branch",
            k8s_content,
        )
        for content in (local_content, k8s_content):
            self.assertIn("STORY_PLANNER_CACHE_VERSION=chapter-branch-v1", content)
            self.assertIn("STORY_PLANNER_CACHE_MAX_AGE_DAYS=30", content)

    def test_templates_include_release_quality_gate_thresholds(self):
        expected_values = (
            "STORY_PLANNER_RELEASE_MIN_GENERATED_CHARS=300",
            "STORY_PLANNER_RELEASE_MIN_ENDINGS=1",
            "STORY_PLANNER_RELEASE_MIN_SCORE=30",
            "STORY_PLANNER_SOFT_BRANCH_TRANSITION_DROP_LIMIT=0.65",
            "STORY_PLANNER_NODE_SELF_HEAL_MAX_ATTEMPTS=3",
            "STORY_PLANNER_RELEASE_REPAIR_MAX_ROUNDS=2",
            "STORY_PLANNER_WHATIF_TRANSITION_REPAIR_ATTEMPTS=1",
            "STORY_PLANNER_TRANSITION_MIN_SCORE=5",
        )
        for name in (".env.local.example", ".env.k8s.example"):
            with self.subTest(template=name):
                content = self._read_template(name)
                for expected in expected_values:
                    self.assertIn(expected, content)

    def test_templates_include_runtime_observability_thresholds(self):
        expected_values = (
            "STORY_PLANNER_HEARTBEAT_INTERVAL_SECONDS=60",
            "STORY_PLANNER_NODE_STALL_TIMEOUT_SECONDS=1800",
        )
        for name in (".env.local.example", ".env.k8s.example"):
            with self.subTest(template=name):
                content = self._read_template(name)
                for expected in expected_values:
                    self.assertIn(expected, content)

    def test_templates_keep_large_novel_tasks_within_38_to_48_hour_window(self):
        expected_values = (
            "STORY_PLANNER_TASK_SOFT_TIME_LIMIT=136800",
            "STORY_PLANNER_TASK_TIME_LIMIT=172800",
        )
        for name in (".env.local.example", ".env.k8s.example"):
            with self.subTest(template=name):
                content = self._read_template(name)
                for expected in expected_values:
                    self.assertIn(expected, content)


if __name__ == "__main__":
    unittest.main()
