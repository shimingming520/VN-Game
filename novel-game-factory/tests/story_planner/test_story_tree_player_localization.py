import copy
import importlib.util
import re
from pathlib import Path
from unittest.mock import MagicMock, patch


def _sample_tree():
    return {
        "root_id": "chapter_1",
        "book_language": "zh-CN",
        "book_title": "江湖诡案录",
        "book_summary": "三位侠客误入杀人机关屋，真正的杀局才刚刚开始。",
        "nodes": {
            "chapter_1": {
                "id": "chapter_1",
                "depth": 1,
                "title": "竹林迷雾中的越狱疑云 chapter_1",
                "summary": "主角在竹林中发现一场离奇越狱。",
                "children": [
                    {
                        "child_id": "node_deadbeef",
                        "choice_text": "继续",
                        "branch_variable": "courage",
                        "branch_threshold": 70,
                    }
                ],
            },
            "node_deadbeef": {
                "id": "node_deadbeef",
                "depth": 2,
                "title": "铁笼毒雾中的致命伏击 node_deadbeef",
                "summary": "铁笼与毒雾把调查者逼入绝境。",
                "children": [
                    {
                        "child_id": "ending_a",
                        "choice_text": "",
                        "branch_variable": "courage",
                        "branch_threshold": 70,
                    }
                ],
            },
            "ending_a": {
                "id": "ending_a",
                "depth": 3,
                "title": "结局·胆魄 ending_a",
                "summary": "主角以胆魄面对最后的机关。",
                "children": [],
            },
        },
        "player_variables": {"courage": 50},
        "branch_thresholds": {
            "ending_a": {"variable": "courage", "threshold": 70}
        },
    }


def _visible_texts(tree):
    values = [tree.get("book_title", ""), tree.get("book_summary", "")]
    for node in tree.get("nodes", {}).values():
        values.append(node.get("title", ""))
        values.append(node.get("summary", ""))
        values.extend(choice.get("choice_text", "") for choice in node.get("children", []))
    return values


def _load_localization_module():
    module_path = Path(
        "services/story_planner/core/story_tree_player_localization.py"
    )
    spec = importlib.util.spec_from_file_location(
        "story_tree_player_localization_under_test",
        module_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_english_localization_changes_only_player_visible_story_tree_fields():
    localize_story_tree_player_fields = (
        _load_localization_module().localize_story_tree_player_fields
    )

    original = _sample_tree()

    def translator(_payload):
        return {
            "book_title": "Chronicles of Jianghu Mysteries",
            "book_summary": "Three wanderers enter a deadly house of traps.",
            "node_titles": {
                "chapter_1": "Prison Break in the Misty Bamboo Forest",
                "node_deadbeef": "Ambush in the Iron Cage and Poison Fog",
                "ending_a": "Ending: Courage",
            },
            "node_summaries": {
                "chapter_1": "The protagonist discovers a strange prison break in the bamboo forest.",
                "node_deadbeef": "An iron cage and poison fog drive the investigators into danger.",
                "ending_a": "The protagonist faces the final trap with courage.",
            },
            "choice_texts": {
                "chapter_1:0": "Enter the Iron Cage",
            },
        }

    result = localize_story_tree_player_fields(
        original,
        player_language="en-US",
        source_text="中文小说正文",
        translator=translator,
    )

    # book_language controls source-text/speaker-name semantics downstream and is
    # not a player-visible label. English UI localization must not overwrite it.
    assert result["book_language"] == "zh-CN"
    assert result["player_language"] == "en-US"
    assert result["source_language"] == "zh-CN"
    assert result["book_title"] == "Chronicles of Jianghu Mysteries"
    assert result["book_summary"] == "Three wanderers enter a deadly house of traps."
    assert result["nodes"]["chapter_1"]["title"] == (
        "Prison Break in the Misty Bamboo Forest"
    )
    assert result["nodes"]["chapter_1"]["summary"] == (
        "The protagonist discovers a strange prison break in the bamboo forest."
    )
    assert result["nodes"]["chapter_1"]["children"][0]["choice_text"] == (
        "Enter the Iron Cage"
    )
    assert result["nodes"]["node_deadbeef"]["children"][0]["choice_text"] == ""

    # 内部生成、路由和数值字段必须原样保留；原对象不能被修改。
    assert original["nodes"]["chapter_1"]["summary"] == "主角在竹林中发现一场离奇越狱。"
    assert result["nodes"]["chapter_1"]["children"][0]["child_id"] == "node_deadbeef"
    assert result["nodes"]["chapter_1"]["children"][0]["branch_variable"] == "courage"
    assert result["branch_thresholds"] == original["branch_thresholds"]
    assert original["book_title"] == "江湖诡案录"


def test_english_localization_uses_safe_english_fallback_when_llm_fails():
    localize_story_tree_player_fields = (
        _load_localization_module().localize_story_tree_player_fields
    )

    def broken_translator(_payload):
        raise RuntimeError("gateway unavailable")

    result = localize_story_tree_player_fields(
        _sample_tree(),
        player_language="en-US",
        translator=broken_translator,
    )

    assert result["book_title"] == "Visual Novel"
    assert result["nodes"]["chapter_1"]["children"][0]["choice_text"] == "Continue"
    assert result["nodes"]["node_deadbeef"]["children"][0]["choice_text"] == ""
    assert not any(re.search(r"[\u4e00-\u9fff]", text or "") for text in _visible_texts(result))


def test_fallback_choices_remain_distinct_when_one_node_has_multiple_choices():
    localize_story_tree_player_fields = (
        _load_localization_module().localize_story_tree_player_fields
    )
    tree = _sample_tree()
    tree["nodes"]["chapter_1"]["children"] = [
        {"child_id": "node_deadbeef", "choice_text": "继续"},
        {"child_id": "ending_a", "choice_text": "换一条路"},
        {"child_id": "chapter_1", "choice_text": "回头调查"},
    ]

    result = localize_story_tree_player_fields(
        tree,
        player_language="en-US",
        translator=lambda _payload: {
            "choice_texts": {
                "chapter_1:0": "Continue",
                "chapter_1:1": "Continue",
                "chapter_1:2": "Continue",
            }
        },
    )

    choice_texts = [
        child["choice_text"]
        for child in result["nodes"]["chapter_1"]["children"]
    ]
    assert choice_texts == ["Continue", "", "Choose Path 3"]
    assert len({text for text in choice_texts if text}) == 2


def test_non_english_player_language_does_not_localize_story_tree():
    localize_story_tree_player_fields = (
        _load_localization_module().localize_story_tree_player_fields
    )

    original = _sample_tree()
    calls = []

    def translator(payload):
        calls.append(payload)
        return {}

    result = localize_story_tree_player_fields(
        original,
        player_language="zh-CN",
        translator=translator,
    )

    assert result == original
    assert result is not original
    assert calls == []


def test_all_story_planner_publication_modes_use_player_localization():
    source = Path("services/story_planner/tasks.py").read_text(encoding="utf-8")
    assert source.count("build_story_tree_language_bundle(") >= 3
    assert source.count("wrap_story_tree_language_bundles(") >= 3
    assert source.count("selected_language=") >= 3


def test_story_tree_player_language_defaults_to_english(monkeypatch):
    module = _load_localization_module()
    monkeypatch.delenv("STORY_PLANNER_STORY_TREE_PLAYER_LANGUAGE", raising=False)
    assert module.get_story_tree_player_language() == "en-US"


def test_english_localization_forces_terminal_routes_to_empty_choice_text():
    localize_story_tree_player_fields = (
        _load_localization_module().localize_story_tree_player_fields
    )
    tree = _sample_tree()
    tree["nodes"]["chapter_1"]["children"][0]["child_id"] = "ending_a"
    tree["nodes"]["chapter_1"]["children"][0]["choice_text"] = "Continue to ending"
    tree["nodes"]["chapter_1"]["children"][0]["branch_variable"] = "courage"

    result = localize_story_tree_player_fields(
        tree,
        player_language="en-US",
        translator=lambda _payload: {"choice_texts": {"chapter_1:0": "Enter the ending"}},
    )

    assert result["nodes"]["chapter_1"]["children"][0]["choice_text"] == ""


def test_english_localization_removes_internal_id_tokens_from_titles():
    localize_story_tree_player_fields = (
        _load_localization_module().localize_story_tree_player_fields
    )

    result = localize_story_tree_player_fields(
        _sample_tree(),
        player_language="en-US",
        translator=lambda _payload: {
            "node_titles": {"chapter_1": "Misty Forest chapter_1"}
        },
    )

    assert result["nodes"]["chapter_1"]["title"] == "Misty Forest"


def test_english_localization_removes_internal_id_even_when_not_a_suffix():
    localize_story_tree_player_fields = (
        _load_localization_module().localize_story_tree_player_fields
    )

    result = localize_story_tree_player_fields(
        _sample_tree(),
        player_language="en-US",
        translator=lambda _payload: {
            "node_titles": {"chapter_1": "chapter_1 - Misty Forest"}
        },
    )

    assert "chapter_1" not in result["nodes"]["chapter_1"]["title"]


def test_localization_prompt_requires_faithful_book_title_translation():
    module = _load_localization_module()
    client = MagicMock()
    client.chat_completion_json.return_value = {}

    with patch(
        "services.story_planner.core.llm_client.LLMClient",
        return_value=client,
    ):
        module._llm_translate({"book_title": "别再争抢我了！"}, provider="openai")

    prompt = client.chat_completion_json.call_args.kwargs["system_prompt"]
    assert "Translate book_title faithfully" in prompt
    assert "do not invent a different title" in prompt


def test_story_tree_language_bundle_contains_english_and_japanese_variants():
    module = _load_localization_module()
    bundle = module.build_story_tree_language_bundle(
        _sample_tree(),
        languages=["en-US", "ja-JP"],
        translators={
            "en-US": lambda _payload: {
                "book_title": "Chronicles of Jianghu Mysteries",
                "book_summary": "Three wanderers enter a deadly house of traps.",
                "node_titles": {
                    "chapter_1": "Prison Break in the Misty Bamboo Forest",
                    "node_deadbeef": "Ambush in the Iron Cage and Poison Fog",
                    "ending_a": "Ending: Courage",
                },
                "choice_texts": {"chapter_1:0": "Enter the Iron Cage"},
            },
            "ja-JP": lambda _payload: {
                "book_title": "江湖怪事件録",
                "book_summary": "三人の旅人は恐ろしい罠の屋敷に迷い込む。",
                "node_titles": {
                    "chapter_1": "竹林の霧に隠れた脱獄の謎",
                    "node_deadbeef": "鉄格子と毒霧の待ち伏せ",
                    "ending_a": "結末：勇気",
                },
                "choice_texts": {"chapter_1:0": "鉄格子へ進む"},
            },
        },
    )

    assert set(bundle) == {"en-US", "ja-JP"}
    assert bundle["en-US"]["player_language"] == "en-US"
    assert bundle["ja-JP"]["player_language"] == "ja-JP"
    assert bundle["en-US"]["nodes"]["chapter_1"]["title"] == (
        "Prison Break in the Misty Bamboo Forest"
    )
    assert bundle["ja-JP"]["nodes"]["chapter_1"]["title"] == (
        "竹林の霧に隠れた脱獄の謎"
    )
    assert bundle["ja-JP"]["nodes"]["chapter_1"]["children"][0]["choice_text"] == (
        "鉄格子へ進む"
    )


def test_story_tree_language_bundle_preserves_internal_routing_and_localizes_summaries():
    module = _load_localization_module()
    original = _sample_tree()
    bundle = module.build_story_tree_language_bundle(
        original,
        languages=["en-US", "ja-JP"],
        translators={
            "en-US": lambda _payload: {
                "node_summaries": {
                    "chapter_1": "The opening summary",
                    "node_deadbeef": "The middle summary",
                    "ending_a": "The ending summary",
                }
            },
            "ja-JP": lambda _payload: {
                "node_summaries": {
                    "chapter_1": "冒頭のあらすじ",
                    "node_deadbeef": "中盤のあらすじ",
                    "ending_a": "結末のあらすじ",
                }
            },
        },
    )

    assert bundle["en-US"]["nodes"]["chapter_1"]["summary"] == "The opening summary"
    assert bundle["ja-JP"]["nodes"]["chapter_1"]["summary"] == "冒頭のあらすじ"
    for language_tree in bundle.values():
        assert language_tree["nodes"]["chapter_1"]["children"][0]["child_id"] == (
            "node_deadbeef"
        )
        assert language_tree["branch_thresholds"] == original["branch_thresholds"]


def test_story_tree_language_bundle_keeps_book_metadata_in_every_locale():
    module = _load_localization_module()
    original = _sample_tree()

    bundle = module.build_story_tree_language_bundle(
        original,
        languages=["en-US", "ja-JP", "zh-CN"],
        translators={
            "en-US": lambda _payload: {
                "book_title": "Chronicles of Jianghu Mysteries",
                "book_summary": "Three wanderers enter a deadly house of traps.",
            },
            "ja-JP": lambda _payload: {
                "book_title": "江湖怪事件録",
                "book_summary": "三人の旅人は恐ろしい罠の屋敷に迷い込む。",
            },
        },
    )

    assert set(bundle) == {"en-US", "ja-JP", "zh-CN"}
    assert bundle["en-US"]["book_title"] == "Chronicles of Jianghu Mysteries"
    assert bundle["en-US"]["book_summary"] == (
        "Three wanderers enter a deadly house of traps."
    )
    assert bundle["ja-JP"]["book_title"] == "江湖怪事件録"
    assert bundle["ja-JP"]["book_summary"] == (
        "三人の旅人は恐ろしい罠の屋敷に迷い込む。"
    )
    assert bundle["zh-CN"]["book_title"] == original["book_title"]
    assert bundle["zh-CN"]["book_summary"] == original["book_summary"]


def test_story_tree_player_languages_default_to_english_and_japanese(monkeypatch):
    module = _load_localization_module()
    monkeypatch.delenv("STORY_PLANNER_STORY_TREE_LANGUAGES", raising=False)
    monkeypatch.delenv("STORY_PLANNER_STORY_TREE_PLAYER_LANGUAGE", raising=False)
    assert module.get_story_tree_player_languages() == ["en-US", "ja-JP"]


def test_story_tree_player_languages_include_selected_language_and_allow_future_tags(
    monkeypatch,
):
    module = _load_localization_module()
    monkeypatch.setenv("STORY_PLANNER_STORY_TREE_LANGUAGES", "en-US,ja-JP,ko-KR")
    assert module.get_story_tree_player_languages(["ru-RU"]) == [
        "en-US",
        "ja-JP",
        "ko-KR",
        "ru-RU",
    ]


def test_story_tree_language_bundle_can_select_a_locale_for_downstream_consumers():
    module = _load_localization_module()
    bundle = {
        "en-US": {"nodes": {"chapter_1": {"title": "English"}}},
        "ja-JP": {"nodes": {"chapter_1": {"title": "日本語"}}},
    }

    assert module.select_story_tree_language(bundle, "ja-JP")["nodes"]["chapter_1"][
        "title"
    ] == "日本語"
    assert module.select_story_tree_language(bundle, "ko-KR") == bundle["en-US"]
    selected = module.select_story_tree_language(bundle, "en-US")
    assert selected == bundle["en-US"]
    assert selected is not bundle["en-US"]


def test_localization_payload_and_prompt_request_node_summaries():
    module = _load_localization_module()
    payload = module.build_player_localization_payload(_sample_tree())
    assert payload["nodes"][0]["summary"] == "主角在竹林中发现一场离奇越狱。"

    captured = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def chat_completion_json(self, **kwargs):
            captured.update(kwargs)
            return {}

    with patch("services.story_planner.core.llm_client.LLMClient", FakeClient):
        module._llm_translate(payload, provider="openai", target_language="en-US")
    assert "node_summaries" in captured["system_prompt"]


def test_publication_metadata_stays_at_bundle_root():
    module = _load_localization_module()
    tree = _sample_tree()
    tree["publication"] = {"run_id": "run-1", "fingerprint": "abc"}
    localized = module.localize_story_tree_player_fields(
        tree,
        player_language="en-US",
        translator=lambda _payload: {},
    )
    wrapped = module.wrap_story_tree_language_bundles(
        localized,
        {"en-US": localized},
    )
    assert "publication" not in wrapped["en-US"]


def test_book_summary_uses_legacy_summary_when_book_summary_is_missing():
    module = _load_localization_module()
    tree = _sample_tree()
    tree.pop("book_summary", None)
    tree["summary"] = "Legacy work summary"
    result = module.localize_story_tree_player_fields(
        tree,
        player_language="en-US",
        translator=lambda _payload: {},
    )
    assert result["book_summary"] == "Legacy work summary"
