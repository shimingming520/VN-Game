import copy
import hashlib
import importlib.util
import json
from pathlib import Path

from services.story_planner.table.story_tree_to_db import calculate_publication_fingerprint


def _load_warning_module():
    module_path = Path("services/story_planner/core/content_warnings.py")
    spec = importlib.util.spec_from_file_location(
        "content_warnings_under_test", module_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample_warning_tree():
    return {
        "root_id": "chapter_1",
        "story_mode": "chapter_branch",
        "nodes": {
            "chapter_1": {
                "id": "chapter_1",
                "content_file": "chapter_1.md",
                "children": [],
            }
        },
        "content_warnings": {
            "schema_version": 1,
            "work": {
                "status": "detected",
                "has_sensitive_content": True,
                "severity": "high",
                "categories": [
                    {
                        "code": "gore",
                        "presence": "depicted",
                        "severity": "high",
                        "confidence": 0.9,
                        "evidence_chapters": [1],
                    }
                ],
                "display_policy": {
                    "show_at_work_entry": True,
                    "requires_acknowledgement": True,
                    "dismissible": True,
                },
                "localized": {
                    "title": "内容提示",
                    "summary": "包含可能令人不适的内容。",
                    "items": [
                        {
                            "code": "gore",
                            "label": "血腥暴力",
                            "description": "作品包含血腥暴力场景。",
                        }
                    ],
                    "acknowledge_text": "我已了解，继续",
                    "return_text": "返回",
                },
                "detector": {
                    "version": "work-content-warning-v1",
                    "revision": "stable",
                },
            },
            "chapters": {},
        },
    }


def test_normalize_content_warnings_filters_invalid_categories_and_clamps_values():
    module = _load_warning_module()
    result = module.normalize_content_warnings(
        {
            "work": {
                "has_sensitive_content": True,
                "categories": [
                    {
                        "code": "gore",
                        "presence": "depicted",
                        "severity": "high",
                        "confidence": 2,
                        "evidence_chapters": [0, 2, "3", -1],
                    },
                    {"code": "not_allowed"},
                ]
            }
        },
        chapter_count=3,
    )

    work = result["work"]
    assert work["has_sensitive_content"] is True
    assert work["categories"] == [
        {
            "code": "gore",
            "presence": "depicted",
            "severity": "high",
            "confidence": 1.0,
            "evidence_chapters": [2, 3],
        }
    ]
    assert result["chapters"] == {}


def test_build_work_content_warnings_detects_strong_signals_and_evidence_chapters():
    module = _load_warning_module()
    chapters = [
        "主角在街上查看旧报纸。",
        "房间里出现碎尸和血腥残骸，随后有人发烧并被隔离。",
    ]
    result = module.build_work_content_warnings(
        "\n".join(chapters), chapters=chapters, provider=None
    )
    work = result["work"]
    codes = {item["code"] for item in work["categories"]}
    assert {"dismemberment", "gore", "infection_or_contagion"} <= codes
    assert work["has_sensitive_content"] is True
    assert work["status"] in {"detected", "review_required"}
    assert all(2 in item["evidence_chapters"] for item in work["categories"])


def test_build_work_content_warnings_without_signals_is_explicit_none():
    module = _load_warning_module()
    result = module.build_work_content_warnings(
        "他们在春天的校园里讨论毕业计划。", chapters=["他们在春天的校园里讨论毕业计划。"]
    )
    work = result["work"]
    assert work["status"] == "none"
    assert work["has_sensitive_content"] is False
    assert work["severity"] == "none"
    assert work["categories"] == []
    assert work["display_policy"]["show_at_work_entry"] is False


def test_build_work_content_warnings_detects_sensitive_content_after_translation():
    """A translated chapter must not erase work-level warning detection."""

    module = _load_warning_module()
    translated_chapter = "The victim was dismembered and blood covered the room."

    result = module.build_work_content_warnings(
        translated_chapter,
        chapters=[translated_chapter],
        provider=None,
    )

    work = result["work"]
    codes = {item["code"] for item in work["categories"]}
    assert {"dismemberment", "gore"} <= codes
    assert work["has_sensitive_content"] is True
    assert work["status"] in {"detected", "review_required"}


def test_build_work_content_warnings_can_use_canonical_source_after_translation():
    """Detection must be able to inspect the pre-translation source text."""

    module = _load_warning_module()
    translated_chapter = "The scene was rewritten for the selected player language."
    source_chapter = "房间里出现碎尸和血腥残骸。"

    result = module.build_work_content_warnings(
        translated_chapter,
        chapters=[translated_chapter],
        source_text=source_chapter,
        source_chapters=[source_chapter],
        provider=None,
    )

    codes = {item["code"] for item in result["work"]["categories"]}
    assert {"dismemberment", "gore"} <= codes


def test_english_non_sensitive_body_word_does_not_trigger_corpse_warning():
    """Avoid treating ordinary English uses of ``body`` as a corpse signal."""

    module = _load_warning_module()
    result = module.build_work_content_warnings(
        "The body of the report explains the body's structure and language.",
        chapters=["The body of the report explains the body's structure and language."],
        provider=None,
    )

    assert result["work"]["status"] == "none"
    assert result["work"]["categories"] == []


def test_build_work_content_warnings_degrades_to_review_required_on_detector_error(
    monkeypatch,
):
    """An optional warning detector failure must not abort the planner run."""

    module = _load_warning_module()

    def fail_detection(_chapters):
        raise RuntimeError("synthetic detector failure")

    monkeypatch.setattr(module, "_deterministic_categories", fail_detection)

    result = module.build_work_content_warnings(
        "普通正文。",
        chapters=["普通正文。"],
        provider=None,
    )

    work = result["work"]
    assert work["status"] == "review_required"
    assert work["has_sensitive_content"] is False
    assert work["display_policy"]["show_at_work_entry"] is True
    assert work["detector"]["review_required"] is True
    assert work["detector"]["verification_status"] == "failed"
    assert "synthetic detector failure" in work["detector"]["failure_reason"]


def test_build_work_content_warnings_records_llm_verification_failure_reason(
    monkeypatch,
):
    module = _load_warning_module()
    monkeypatch.setattr(
        module,
        "_llm_categories",
        lambda **_kwargs: (None, "HTTP 504 gateway timeout"),
    )

    result = module.build_work_content_warnings(
        "房间里出现碎尸。",
        chapters=["房间里出现碎尸。"],
        provider="openai",
    )

    detector = result["work"]["detector"]
    assert detector["verification_status"] == "failed"
    assert detector["failure_reason"] == "HTTP 504 gateway timeout"


def test_localize_content_warnings_uses_controlled_locale_copy_without_changing_codes():
    module = _load_warning_module()
    warnings = module.build_work_content_warnings(
        "发现碎尸和尸体。", chapters=["发现碎尸和尸体。"]
    )
    localized = module.localize_content_warnings(warnings, "ja-JP")
    work = localized["work"]
    assert work["localized"]["title"]
    assert work["localized"]["items"]
    assert {item["code"] for item in work["localized"]["items"]} == {
        item["code"] for item in work["categories"]
    }
    assert not any("碎尸" in str(value) for value in work["localized"].values())


def test_validate_content_warnings_schema_reports_missing_entry_policy():
    module = _load_warning_module()
    warnings = module.normalize_content_warnings({"work": {"has_sensitive_content": True}})
    warnings["work"]["display_policy"] = {}
    errors = module.validate_content_warnings_schema(warnings, require_localized=True)
    assert errors
    assert any("display_policy" in error for error in errors)


def test_validate_content_warnings_schema_requires_detector_identity_and_localized_item_shape():
    module = _load_warning_module()
    warnings = module.normalize_content_warnings(
        {
            "work": {
                "has_sensitive_content": True,
                "categories": ["gore"],
                "detector": {},
            }
        }
    )
    warnings["work"]["detector"].pop("version", None)
    warnings["work"]["localized"]["items"][0]["label"] = None

    errors = module.validate_content_warnings_schema(warnings, require_localized=True)

    assert any("detector.version" in error for error in errors)
    assert any("localized.items" in error for error in errors)


def test_validate_content_warnings_schema_never_throws_on_malformed_version():
    module = _load_warning_module()

    errors = module.validate_content_warnings_schema(
        {"schema_version": "not-a-number", "work": {}},
        require_localized=True,
    )

    assert errors
    assert any("schema_version" in error for error in errors)


def test_validate_content_warnings_schema_enforces_status_policy_and_evidence_shape():
    module = _load_warning_module()
    warnings = module.normalize_content_warnings(
        {
            "work": {
                "status": "none",
                "has_sensitive_content": False,
                "severity": "none",
                "categories": [
                    {
                        "code": "gore",
                        "presence": "depicted",
                        "severity": "high",
                        "confidence": 0.9,
                        "evidence_chapters": [1],
                    }
                ],
                "display_policy": {
                    "show_at_work_entry": True,
                    "requires_acknowledgement": False,
                    "dismissible": True,
                    "allow_continue": True,
                    "allow_return": True,
                },
            }
        }
    )
    warnings["work"]["categories"][0]["evidence_chapters"] = [1, 1, 0, "2"]
    warnings["work"]["status"] = "none"
    warnings["work"]["has_sensitive_content"] = False
    warnings["work"]["severity"] = "none"
    warnings["work"]["display_policy"]["requires_acknowledgement"] = False

    errors = module.validate_content_warnings_schema(warnings, require_localized=True)

    assert any("status=none" in error for error in errors)
    assert any("evidence_chapters" in error for error in errors)
    assert any("requires_acknowledgement" in error for error in errors)


def test_content_warning_checkpoint_requires_ruleset_identity():
    module = _load_warning_module()
    warnings = module.build_work_content_warnings(
        "发现碎尸和尸体。", chapters=["发现碎尸和尸体。"], provider=None
    )
    checkpoint = {
        "schema_version": 1,
        "input_digest": "input-digest",
        "output": warnings,
        "output_digest": hashlib.sha256(
            json.dumps(
                warnings, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest(),
        "detector_version": module.CONTENT_WARNING_DETECTOR_VERSION,
        "ruleset_digest": module.content_warning_ruleset_digest(),
    }

    assert module.is_reusable_content_warnings_checkpoint(
        checkpoint,
        input_digest="input-digest",
        detector_version=module.CONTENT_WARNING_DETECTOR_VERSION,
        ruleset_digest=module.content_warning_ruleset_digest(),
    )
    checkpoint["ruleset_digest"] = "old-rules"
    assert not module.is_reusable_content_warnings_checkpoint(
        checkpoint,
        input_digest="input-digest",
        detector_version=module.CONTENT_WARNING_DETECTOR_VERSION,
        ruleset_digest=module.content_warning_ruleset_digest(),
    )


def test_content_warning_checkpoint_reuse_requires_digest_and_detector_version():
    module = _load_warning_module()
    warnings = module.build_work_content_warnings(
        "发现碎尸和尸体。", chapters=["发现碎尸和尸体。"], provider=None
    )
    checkpoint = {
        "schema_version": 1,
        "input_digest": "input-digest",
        "output": warnings,
        "output_digest": hashlib.sha256(
            json.dumps(
                warnings, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest(),
        "detector_version": module.CONTENT_WARNING_DETECTOR_VERSION,
    }

    assert module.is_reusable_content_warnings_checkpoint(
        checkpoint,
        input_digest="input-digest",
        detector_version=module.CONTENT_WARNING_DETECTOR_VERSION,
    )

    tampered = copy.deepcopy(checkpoint)
    tampered["output"]["work"]["severity"] = "low"
    assert not module.is_reusable_content_warnings_checkpoint(
        tampered,
        input_digest="input-digest",
        detector_version=module.CONTENT_WARNING_DETECTOR_VERSION,
    )

    revision_tampered = copy.deepcopy(checkpoint)
    revision_tampered["output"]["work"]["detector"]["revision"] = "0" * 24
    revision_tampered["output_digest"] = hashlib.sha256(
        json.dumps(
            revision_tampered["output"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert not module.is_reusable_content_warnings_checkpoint(
        revision_tampered,
        input_digest="input-digest",
        detector_version=module.CONTENT_WARNING_DETECTOR_VERSION,
    )

    wrong_version = copy.deepcopy(checkpoint)
    wrong_version["detector_version"] = "old-detector"
    assert not module.is_reusable_content_warnings_checkpoint(
        wrong_version,
        input_digest="input-digest",
        detector_version=module.CONTENT_WARNING_DETECTOR_VERSION,
    )


def test_story_tree_localization_preserves_and_localizes_content_warnings():
    localization_path = Path(
        "services/story_planner/core/story_tree_player_localization.py"
    )
    spec = importlib.util.spec_from_file_location(
        "story_tree_localization_warning_test", localization_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tree = _sample_warning_tree()
    result = module.localize_story_tree_player_fields(
        tree,
        player_language="en-US",
        translator=lambda _payload: {},
    )
    assert result["content_warnings"]["work"]["localized"]["title"] == "Content Warning"
    assert result["content_warnings"]["work"]["categories"][0]["code"] == "gore"


def test_story_tree_localization_uses_japanese_controlled_warning_copy():
    localization_path = Path(
        "services/story_planner/core/story_tree_player_localization.py"
    )
    spec = importlib.util.spec_from_file_location(
        "story_tree_localization_warning_ja_test", localization_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.localize_story_tree_player_fields(
        _sample_warning_tree(),
        player_language="ja-JP",
        translator=lambda _payload: {},
    )
    localized = result["content_warnings"]["work"]["localized"]
    assert localized["title"] == "コンテンツに関する注意"
    assert not any("内容提示" in str(value) for value in localized.values())


def test_language_bundle_contains_warning_snapshot_for_each_locale():
    localization_path = Path(
        "services/story_planner/core/story_tree_player_localization.py"
    )
    spec = importlib.util.spec_from_file_location(
        "story_tree_bundle_warning_test", localization_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tree = _sample_warning_tree()
    bundle = module.build_story_tree_language_bundle(
        tree,
        languages=["en-US", "ja-JP"],
        translators={"en-US": lambda _payload: {}, "ja-JP": lambda _payload: {}},
    )
    assert set(bundle) == {"en-US", "ja-JP"}
    assert all("content_warnings" in value for value in bundle.values())
    assert all(
        value["content_warnings"]["work"]["categories"][0]["code"] == "gore"
        for value in bundle.values()
    )


def test_legacy_story_tree_without_warnings_remains_valid_for_localization():
    localization_path = Path(
        "services/story_planner/core/story_tree_player_localization.py"
    )
    spec = importlib.util.spec_from_file_location(
        "story_tree_legacy_warning_test", localization_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    legacy = {"root_id": "chapter_1", "nodes": {"chapter_1": {"children": []}}}
    result = module.localize_story_tree_player_fields(
        legacy, player_language="zh-CN", translator=lambda _payload: {}
    )
    assert "content_warnings" not in result


def test_publication_fingerprint_changes_when_content_warning_changes():
    module = _load_warning_module()
    tree = _sample_warning_tree()
    original = calculate_publication_fingerprint({"chapter_1": "正文。"}, tree)
    changed = copy.deepcopy(tree)
    changed["content_warnings"]["work"]["severity"] = "moderate"
    updated = calculate_publication_fingerprint({"chapter_1": "正文。"}, changed)
    assert original != updated


def test_chapter_branch_pipeline_attaches_work_content_warnings_before_release_gate():
    source = Path("services/story_planner/tasks.py").read_text(encoding="utf-8")
    marker = "def _parse_chapter_branch_mode_once"
    section = source[source.index(marker):]
    assert "build_work_content_warnings" in section
    assert '"content_warnings"' in section
    # Check the production call sites rather than the import block, which also
    # contains the helper names.
    warning_call = section.index("_content_warnings = build_work_content_warnings")
    integrity_call = section.index("release_ok, release_errors = validate_release_integrity")
    assert warning_call < integrity_call
    assert section.index('"content_warnings": _content_warnings') < integrity_call
    assert "source_text=_content_warning_source_text" in section
    assert "source_chapters=_content_warning_source_chapters" in section


def test_direct_chapter_branch_resume_can_restore_source_sidecar():
    source = Path("services/story_planner/tasks.py").read_text(encoding="utf-8")
    section = source[source.index("def _parse_chapter_branch_mode_once"):]
    assert "_load_content_warning_source(" in section
    assert "game_project," in section
