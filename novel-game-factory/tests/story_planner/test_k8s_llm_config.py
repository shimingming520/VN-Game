from pathlib import Path
import unittest


class StoryPlannerK8sLLMConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.deployment_path = (
            Path(__file__).resolve().parents[2]
            / "k8s"
            / "story-planner-deployment.yaml"
        )
        cls.content = cls.deployment_path.read_text(encoding="utf-8")

    def test_deployment_uses_story_planner_api_key_secret(self):
        self.assertIn("name: STORY_PLANNER_LLM_API_KEY", self.content)
        self.assertIn("key: STORY_PLANNER_LLM_API_KEY", self.content)
        self.assertNotIn("name: DASHSCOPE_API_KEY", self.content)

    def test_deployment_targets_k8s_gpt56_sol_openai_gateway(self):
        self.assertIn("name: LLM_PROVIDER", self.content)
        self.assertIn("name: STORY_PLANNER_LLM_BASE_URL", self.content)
        self.assertIn(
            'value: "http://172.16.30.31:32691/v1"',
            self.content,
        )
        self.assertIn("name: STORY_PLANNER_LLM_MODEL", self.content)
        self.assertIn('value: "gpt-5.6-sol"', self.content)
        self.assertIn("name: STORY_PLANNER_LLM_API_FORMAT", self.content)
        self.assertIn('value: "openai"', self.content)

    def test_deployment_uses_large_novel_timeout_budget(self):
        expected_values = (
            ("STORY_PLANNER_LLM_TIMEOUT", "1800"),
            ("STORY_PLANNER_TASK_SOFT_TIME_LIMIT", "136800"),
            ("STORY_PLANNER_TASK_TIME_LIMIT", "172800"),
            ("STORY_PLANNER_CONFIRMATION_TIMEOUT", "21600"),
        )
        for name, value in expected_values:
            self.assertIn(f"name: {name}", self.content)
            self.assertIn(f'value: "{value}"', self.content)

    def test_deployment_has_large_novel_preflight_limits(self):
        expected_values = (
            ("STORY_PLANNER_MAX_NOVEL_CHARS", "1500000"),
            ("STORY_PLANNER_MAX_CHAPTERS", "3000"),
            ("STORY_PLANNER_ESTIMATED_LLM_SECONDS_PER_CALL", "30"),
        )
        for name, value in expected_values:
            self.assertIn(f"name: {name}", self.content)
            self.assertIn(f'value: "{value}"', self.content)

    def test_deployment_has_hierarchical_summary_limits(self):
        expected_values = (
            ("STORY_PLANNER_ANALYSIS_SEGMENT_CHARS", "13000"),
            ("STORY_PLANNER_SUMMARY_MERGE_BATCH_SIZE", "8"),
            ("STORY_PLANNER_WORLDVIEW_SEGMENT_CHARS", "40000"),
            ("STORY_PLANNER_WORLDVIEW_MERGE_BATCH_SIZE", "5"),
        )
        for name, value in expected_values:
            self.assertIn(f"name: {name}", self.content)
            self.assertIn(f'value: "{value}"', self.content)

    def test_deployment_has_llm_prompt_budget(self):
        expected_values = (
            ("STORY_PLANNER_LLM_CONTEXT_WINDOW_TOKENS", "128000"),
            ("STORY_PLANNER_LLM_MAX_INPUT_TOKENS", "80000"),
            ("STORY_PLANNER_LLM_PROMPT_SAFETY_TOKENS", "8000"),
        )
        for name, value in expected_values:
            self.assertIn(f"name: {name}", self.content)
            self.assertIn(f'value: "{value}"', self.content)

    def test_deployment_has_llm_resilience_settings(self):
        expected_values = (
            ("STORY_PLANNER_LLM_MAX_ATTEMPTS", "5"),
            ("STORY_PLANNER_LLM_BACKOFF_BASE_SECONDS", "2"),
            ("STORY_PLANNER_LLM_BACKOFF_MAX_SECONDS", "60"),
            ("STORY_PLANNER_LLM_BACKOFF_JITTER_RATIO", "0.2"),
            ("STORY_PLANNER_LLM_REQUESTS_PER_MINUTE", "0"),
        )
        for name, value in expected_values:
            self.assertIn(f"name: {name}", self.content)
            self.assertIn(f'value: "{value}"', self.content)

    def test_deployment_disables_cluster_llm_backpressure(self):
        expected_values = (
            ("STORY_PLANNER_LLM_GLOBAL_LIMIT_ENABLED", "0"),
            ("STORY_PLANNER_LLM_GLOBAL_REQUESTS_PER_MINUTE", "60"),
            ("STORY_PLANNER_LLM_GLOBAL_MAX_CONCURRENCY", "4"),
            ("STORY_PLANNER_LLM_BACKPRESSURE_MAX_WAIT_SECONDS", "900"),
            ("STORY_PLANNER_LLM_BACKPRESSURE_POLL_SECONDS", "0.5"),
            ("STORY_PLANNER_LLM_CONCURRENCY_LEASE_SECONDS", "1800"),
        )
        for name, value in expected_values:
            self.assertIn(f"name: {name}", self.content)
            self.assertIn(f'value: "{value}"', self.content)

        self.assertIn('"--concurrency=1"', self.content)
        self.assertIn('"--prefetch-multiplier=1"', self.content)

    def test_deployment_uses_versioned_cache_on_shared_storage(self):
        expected_values = (
            (
                "STORY_PLANNER_CACHE_DIR",
                "/storage/story_planner/cache/chapter_branch",
            ),
            ("STORY_PLANNER_CACHE_VERSION", "chapter-branch-v1"),
            ("STORY_PLANNER_CACHE_MAX_AGE_DAYS", "30"),
        )
        for name, value in expected_values:
            self.assertIn(f"name: {name}", self.content)
            self.assertIn(f'value: "{value}"', self.content)

        self.assertIn("mountPath: /storage", self.content)

    def test_deployment_has_release_quality_gate_thresholds(self):
        expected_values = (
            ("STORY_PLANNER_RELEASE_MIN_GENERATED_CHARS", "300"),
            ("STORY_PLANNER_RELEASE_MIN_ENDINGS", "1"),
            ("STORY_PLANNER_RELEASE_MIN_SCORE", "30"),
            ("STORY_PLANNER_SOFT_BRANCH_TRANSITION_DROP_LIMIT", "0.65"),
            ("STORY_PLANNER_NODE_SELF_HEAL_MAX_ATTEMPTS", "3"),
            ("STORY_PLANNER_RELEASE_REPAIR_MAX_ROUNDS", "2"),
            ("STORY_PLANNER_WHATIF_TRANSITION_REPAIR_ATTEMPTS", "1"),
            ("STORY_PLANNER_TRANSITION_MIN_SCORE", "5"),
        )
        for name, value in expected_values:
            self.assertIn(f"name: {name}", self.content)
            self.assertIn(f'value: "{value}"', self.content)

    def test_deployment_has_runtime_observability_thresholds(self):
        expected_values = (
            ("STORY_PLANNER_HEARTBEAT_INTERVAL_SECONDS", "60"),
            ("STORY_PLANNER_NODE_STALL_TIMEOUT_SECONDS", "1800"),
        )
        for name, value in expected_values:
            self.assertIn(f"name: {name}", self.content)
            self.assertIn(f'value: "{value}"', self.content)

    def test_shared_test_secret_declares_story_planner_key_without_legacy_example(self):
        k8s_dir = Path(__file__).resolve().parents[2] / "k8s"
        secret_test_path = k8s_dir / "secret-test.yaml"
        secret_example_path = (
            k8s_dir / "story-planner-secret.example.yaml"
        )
        self.assertTrue(
            secret_test_path.exists(),
            "缺少集群测试环境共用 Secret",
        )
        content = secret_test_path.read_text(encoding="utf-8")
        self.assertIn("STORY_PLANNER_LLM_API_KEY:", content)
        self.assertFalse(
            secret_example_path.exists(),
            "废弃的独立 story_planner Secret 示例不应恢复",
        )


if __name__ == "__main__":
    unittest.main()
