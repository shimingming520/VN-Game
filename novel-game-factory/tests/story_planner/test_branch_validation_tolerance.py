from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.story_planner.core import branch_designer, soft_branch
from services.story_planner.core import chapter_branch_quality


def _candidate(**overrides):
    value = {
        "choice_a": "Speak first",
        "content_a": "I gather my thoughts and look at the other person.",
        "effects_a": {"courage": 3},
        "choice_b": "Stay silent",
        "content_b": "I swallow the words and let the tension settle.",
        "effects_b": {"courage": -3},
        "bridge": "I stop before the continuation point.",
    }
    value.update(overrides)
    return value


def test_quote_and_pov_validation_are_warning_class_after_retries():
    assert soft_branch.classify_soft_branch_validation_issue(
        "content_a: quote not closed", candidate=_candidate()
    ) == "warning"
    assert soft_branch.classify_soft_branch_validation_issue(
        "narrative POV drift", candidate=_candidate()
    ) == "warning"


def test_complete_candidate_is_retained_with_warning_metadata():
    retained = soft_branch.retain_soft_branch_candidate(
        _candidate(), ["narrative POV drift", "effects dominant axis differs"]
    )
    assert retained["validation_status"] == "warning"
    assert retained["validation_warnings"] == [
        "narrative POV drift",
        "effects dominant axis differs",
    ]


def test_soft_branch_keeps_candidate_after_optional_retries_are_exhausted():
    client = MagicMock()
    bad = _candidate(content_a='He says: "Do not turn back.')
    client.chat_completion_json.side_effect = [bad, bad, bad, bad]
    with patch.object(soft_branch, "LLMClient", return_value=client):
        content = soft_branch.inject_soft_branches(
            chapter_text="First scene.\n\nSecond scene.",
            chapter_idx=0,
            provider="openai",
            scenes_count=2,
            variable_defs=[{"key": "courage", "init": 50, "threshold": 70}],
            total_branches=1,
        )
    assert "validation_warnings=" in content


def test_effect_axis_mismatch_is_retained_as_warning_after_retries():
    client = MagicMock()
    candidate = _candidate(
        effects_a={"courage": 3},
        effects_b={"calmness": -3},
    )
    client.chat_completion_json.side_effect = [candidate, candidate, candidate, candidate]
    with patch.object(soft_branch, "LLMClient", return_value=client):
        content = soft_branch.inject_soft_branches(
            chapter_text="First scene.\n\nSecond scene.",
            chapter_idx=0,
            provider="openai",
            scenes_count=2,
            variable_defs=[
                {"key": "courage", "init": 50, "threshold": 70},
                {"key": "calmness", "init": 50, "threshold": 70},
            ],
            total_branches=1,
        )
    assert "validation_status=warning" in content


def test_modular_transition_warning_keeps_group_after_repair_failure():
    client = MagicMock()
    candidate = {
        "choice_a": "Take the old key",
        "content_a": "I take the old key away from the table and hold it in my palm.",
        "effects_a": {},
        "choice_b": "Observe the table",
        "content_b": "I study the table and note the dust around the old key.",
        "effects_b": {},
        "clue_flags_a": {},
        "clue_flags_b": {},
    }
    client.chat_completion_json.return_value = candidate
    context = SimpleNamespace(
        worldview={"characters": []},
        chapter_summaries=[],
    )
    with patch.object(soft_branch, "LLMClient", return_value=client):
        content = soft_branch.inject_soft_branches(
            chapter_text="A Mu arrives at the table and sees an old key.\n\n"
            "The old key is still on the table when everyone returns.",
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
        )
    assert "validation_warnings=" in content


def test_low_score_without_hard_conflict_requires_local_retry():
    assert not branch_designer.is_acceptable_whatif_transition_review(
        {"compatible": False, "hard_conflict": False, "score": 3, "issues": ["location difference"]}
    )


def test_whatif_transition_score_five_is_the_minimum_accepted_score():
    assert branch_designer.is_acceptable_whatif_transition_review(
        {"compatible": False, "hard_conflict": False, "score": 5, "issues": ["minor location difference"]}
    )


def test_whatif_transition_low_score_is_fatal_after_retries():
    assert branch_designer.classify_whatif_validation_issue(
        "实际衔接评分不足：4/10",
        content="usable generated prose",
        review={
            "compatible": False,
            "accepted_with_warning": False,
            "hard_conflict": False,
            "score": 4,
            "issues": ["minor bridge gap"],
        },
    ) == "fatal"


def test_whatif_transition_score_gate_retries_only_until_score_reaches_five():
    reviews = iter(
        [
            {
                "compatible": False,
                "hard_conflict": False,
                "score": 4,
                "issues": ["bridge needs one more action"],
            },
            {
                "compatible": False,
                "accepted_with_warning": True,
                "hard_conflict": False,
                "score": 5,
                "issues": ["minor bridge gap"],
            },
        ]
    )
    repairs = []
    result = branch_designer.repair_whatif_until_compatible(
        content="original prose",
        downstream_opening="downstream opening",
        sibling_states={},
        provider="openai",
        review_callback=lambda **_kwargs: next(reviews),
        repair_callback=lambda content, **_kwargs: repairs.append(content) or content + " repaired",
        max_attempts=2,
    )
    assert result["compatible"] is True
    assert result["attempts"] == 1
    assert len(repairs) == 1


def test_unavailable_transition_review_is_warning_without_prose_rewrite():
    repairs = []
    result = branch_designer.repair_whatif_until_compatible(
        content="original prose",
        downstream_opening="downstream opening",
        sibling_states={},
        provider="openai",
        review_callback=lambda **_kwargs: {
            "compatible": False,
            "accepted_with_warning": True,
            "hard_conflict": False,
            "review_error": True,
            "score": 0,
            "issues": ["review unavailable"],
        },
        repair_callback=lambda content, **_kwargs: repairs.append(content) or content + " repaired",
        max_attempts=2,
    )
    assert result["compatible"] is True
    assert result["attempts"] == 0
    assert repairs == []


def test_explicit_confirmed_death_revival_remains_blocking():
    assert not branch_designer.is_acceptable_whatif_transition_review(
        {"compatible": False, "hard_conflict": True, "score": 9, "issues": ["confirmed death revival"]}
    )


def test_whatif_quality_failures_with_content_are_warnings_not_omissions():
    assert branch_designer.classify_whatif_validation_issue(
        "underlong_repair", content="usable generated prose"
    ) == "warning"
    assert branch_designer.classify_whatif_validation_issue(
        "scene consistency review failed", content="usable generated prose"
    ) == "warning"


def test_whatif_empty_content_and_hard_conflicts_remain_fatal():
    assert branch_designer.classify_whatif_validation_issue(
        "underlong_repair", content=""
    ) == "fatal"
    assert branch_designer.classify_whatif_validation_issue(
        "confirmed death revival", content="usable generated prose"
    ) == "fatal"
    assert branch_designer.classify_whatif_validation_issue(
        "正文包含占位或内部指令文本", content="占位文本"
    ) == "fatal"


def test_soft_branch_missing_one_route_remains_fatal():
    assert soft_branch.classify_soft_branch_validation_issue(
        "content_b 为空",
        candidate=_candidate(content_b=""),
        category="content_contract",
    ) == "fatal"


def test_generated_content_length_and_delimiter_findings_are_warnings():
    assert branch_designer.classify_whatif_validation_issue(
        "正文长度不足：240 < 300", content="仍可阅读的正文。"
    ) == "warning"
    assert branch_designer.classify_whatif_validation_issue(
        "正文末句缺少完整结束标点", content="仍可阅读的正文"
    ) == "warning"


def test_node_validation_warnings_are_exposed_in_quality_report():
    tree = {
        "root_id": "root",
        "branch_type": "default",
        "nodes": {
            "root": {
                "id": "root",
                "title": "第一章",
                "summary": "正文",
                "content_file": "root.md",
                "children": [],
                "is_original": True,
                "generated_at": "2026-01-01T00:00:00",
                "terminal_states": {},
                "director_metadata": {
                    "validation_status": "warning",
                    "validation_warnings": ["POV drift retained"],
                },
            }
        },
        "quality_gate": {"passed": True},
    }
    report = chapter_branch_quality.evaluate_artifacts(tree, {"root": "# 第一章\n\n正文。"})
    assert any("POV drift retained" in item for item in report["warnings"])


def test_soft_branch_marker_warnings_are_exposed_in_quality_report():
    tree = {
        "root_id": "root",
        "branch_type": "default",
        "nodes": {
            "root": {
                "id": "root",
                "title": "第一章",
                "summary": "正文",
                "content_file": "root.md",
                "children": [],
                "is_original": True,
                "generated_at": "2026-01-01T00:00:00",
                "terminal_states": {},
            }
        },
        "quality_gate": {"passed": True},
    }
    md = {
        "root": (
            "# 第一章\n\n正文。\n<!-- effects_a={'courage': 3} "
            "effects_b={'courage': -3} validation_status=warning "
            "validation_warnings=['quote drift'] -->"
        )
    }
    report = chapter_branch_quality.evaluate_artifacts(tree, md)
    assert any("quote drift" in item for item in report["warnings"])
