from services.story_planner.core.llm_resilience import DEFAULT_GLOBAL_KEY_PREFIX


def test_resilience_namespace_is_scoped_to_gpt56_sol():
    assert DEFAULT_GLOBAL_KEY_PREFIX == "story_planner:llm:gpt-5.6-sol"
