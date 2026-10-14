import unittest

from common import usage_tracker


class _Connection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params):
        self.calls.append((str(statement), params))


class _Engine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return self.connection


class UsageTrackerTests(unittest.TestCase):
    def test_llm_upsert_initializes_music_seconds(self):
        connection = _Connection()
        original_engine = usage_tracker._engine
        original_init = usage_tracker.init_usage_table
        original_budget_check = usage_tracker.enforce_task_usage_budget
        try:
            usage_tracker._engine = _Engine(connection)
            usage_tracker.init_usage_table = lambda: True
            usage_tracker.enforce_task_usage_budget = lambda _task_id: None

            tracker = usage_tracker.UsageTracker()
            context = usage_tracker.UsageContext("task-a", "story_planner", "run-a")
            usage = usage_tracker.NormalizedLLMUsage(
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
            )

            self.assertTrue(tracker._upsert_llm(context, "model-a", "provider-a", usage))
            sql, params = connection.calls[0]
            self.assertIn("music_seconds", sql)
            self.assertEqual(0, params["music_seconds"])
        finally:
            usage_tracker._engine = original_engine
            usage_tracker.init_usage_table = original_init
            usage_tracker.enforce_task_usage_budget = original_budget_check


if __name__ == "__main__":
    unittest.main()
