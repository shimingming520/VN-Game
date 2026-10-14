from common.story_tree_locale import (
    select_story_tree_for_consumer,
    story_tree_selection_metadata,
    update_story_tree_locale_fields,
)


def _bundle():
    return {
        "en-US": {
            "nodes": {"node_1": {"id": "node_1", "title": "English"}},
            "root_id": "node_1",
            "global_style": "shared style",
            "visual_style_context": {"schema_version": "visual-style-context-v1"},
            "player_language": "en-US",
        },
        "ja-JP": {
            "nodes": {"node_1": {"id": "node_1", "title": "日本語"}},
            "root_id": "node_1",
            "global_style": "shared style",
            "visual_style_context": {"schema_version": "visual-style-context-v1"},
            "player_language": "ja-JP",
        },
        "publication": {
            "run_id": "run-1",
            "run_started_at": "2026-01-01T00:00:00Z",
            "fingerprint": "a" * 64,
        },
    }


def test_bundle_selects_requested_locale_and_falls_back_to_english(monkeypatch):
    payload = _bundle()
    monkeypatch.setenv("STORY_TREE_PLAYER_LANGUAGE", "en-US")

    selected = select_story_tree_for_consumer(payload, "ja")
    assert selected["player_language"] == "ja-JP"
    assert selected["nodes"]["node_1"]["title"] == "日本語"
    assert "publication" not in selected

    fallback = select_story_tree_for_consumer(payload, "ko-KR")
    assert fallback["player_language"] == "en-US"
    assert fallback["nodes"]["node_1"]["id"] == "node_1"


def test_mixed_legacy_empty_nodes_does_not_hide_locale_bundle():
    payload = _bundle()
    payload["nodes"] = {}
    selected = select_story_tree_for_consumer(payload, "ja-JP")
    assert selected["player_language"] == "ja-JP"
    assert selected["nodes"]["node_1"]["title"] == "日本語"


def test_legacy_tree_is_returned_unchanged_and_is_deep_copied():
    legacy = {"nodes": {"node_1": {"id": "node_1"}}, "root_id": "node_1"}
    selected = select_story_tree_for_consumer(legacy, "ja-JP")
    assert selected == legacy
    assert selected is not legacy
    selected["nodes"]["node_1"]["id"] = "changed"
    assert legacy["nodes"]["node_1"]["id"] == "node_1"


def test_selection_metadata_keeps_publication_separate():
    metadata = story_tree_selection_metadata(_bundle(), "ja-JP")
    assert metadata["actual_language"] == "ja-JP"
    assert metadata["publication"]["run_id"] == "run-1"
    assert metadata["publication"]["fingerprint"] == "a" * 64


def test_invariant_updates_apply_to_every_locale_and_keep_publication():
    payload = _bundle()
    updated = update_story_tree_locale_fields(
        payload,
        {"global_style": "new style", "global_style_generation": {"version": 2}},
    )
    assert updated["en-US"]["global_style"] == "new style"
    assert updated["ja-JP"]["global_style"] == "new style"
    assert updated["en-US"]["global_style_generation"]["version"] == 2
    assert updated["ja-JP"]["global_style_generation"]["version"] == 2
    assert updated["publication"] == payload["publication"]
