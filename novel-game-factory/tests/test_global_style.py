import json

import pytest
from sqlalchemy import create_engine, text

from common.global_style import (
    GLOBAL_STYLE_META_KEY,
    GLOBAL_STYLE_MODEL,
    GLOBAL_STYLE_PROMPT_VERSION,
    GlobalStyleMissingError,
    _global_style_input_digest,
    ensure_global_style,
    ensure_global_style_generated,
    get_current_global_style,
    get_global_style,
    prepend_global_style,
)


STYLE = "gouache illustration, muted earth palette, diffuse overcast light"


def test_global_style_uses_gpt56_sol():
    assert GLOBAL_STYLE_MODEL == "gpt-5.6-sol"


def _engine_with_story_tree(data):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE story_tree_json (task_id TEXT, data TEXT)"))
        conn.execute(
            text("INSERT INTO story_tree_json (task_id, data) VALUES (:tid, :data)"),
            {"tid": "task-1", "data": json.dumps(data)},
        )
    return engine


def test_get_global_style_reads_story_tree_field_only():
    engine = _engine_with_story_tree(
        {"global_style": STYLE, "art_style": "cyberpunk"}
    )

    assert get_global_style("task-1", db_engine=engine) == STYLE


def test_get_global_style_does_not_fall_back_to_art_style():
    engine = _engine_with_story_tree({"art_style": "ink_chinese"})

    with pytest.raises(GlobalStyleMissingError, match="缺少.*global_style"):
        get_global_style("task-1", db_engine=engine)


def test_prepend_global_style_is_idempotent():
    prompt = "an empty station platform at dawn"
    expected = f"{STYLE}, {prompt}"

    assert prepend_global_style(STYLE, prompt) == expected
    assert prepend_global_style(STYLE, expected) == expected


def test_ensure_global_style_uses_task_contract(monkeypatch):
    monkeypatch.setattr("common.global_style.get_global_style", lambda task_id: STYLE)

    assert ensure_global_style("task-1", "a full-body character") == (
        f"{STYLE}, a full-body character"
    )


def test_prepend_global_style_rejects_empty_style():
    with pytest.raises(GlobalStyleMissingError, match="不能为空"):
        prepend_global_style("", "a scene")


def _visual_context(excerpt="old excerpt"):
    return {
        "schema_version": "visual-style-context-v1",
        "representative_excerpts": [excerpt],
        "worldview": "old town",
    }


def test_existing_global_style_reuses_matching_whole_book_digest(monkeypatch):
    story = {
        "global_style": STYLE,
        "visual_style_context": _visual_context(),
    }
    digest = _global_style_input_digest(story, [])
    story[GLOBAL_STYLE_META_KEY] = {
        "input_digest": digest,
        "prompt_version": GLOBAL_STYLE_PROMPT_VERSION,
        "model": GLOBAL_STYLE_MODEL,
    }
    engine = _engine_with_story_tree(story)
    monkeypatch.setattr(
        "common.global_style._write_story_tree_file", lambda *args, **kwargs: None
    )

    assert ensure_global_style_generated("task-1", db_engine=engine) == STYLE
    assert get_current_global_style("task-1", db_engine=engine) == STYLE


def test_current_global_style_rejects_stale_metadata():
    story = {
        "global_style": STYLE,
        "visual_style_context": _visual_context(),
        GLOBAL_STYLE_META_KEY: {
            "input_digest": "stale",
            "prompt_version": GLOBAL_STYLE_PROMPT_VERSION,
            "model": GLOBAL_STYLE_MODEL,
        },
    }
    engine = _engine_with_story_tree(story)

    assert get_current_global_style("task-1", db_engine=engine) == ""


def test_changed_whole_book_context_invalidates_existing_style(monkeypatch):
    old_story = {
        "global_style": STYLE,
        "visual_style_context": _visual_context("old excerpt"),
    }
    old_story[GLOBAL_STYLE_META_KEY] = {
        "input_digest": _global_style_input_digest(old_story, []),
        "prompt_version": GLOBAL_STYLE_PROMPT_VERSION,
        "model": GLOBAL_STYLE_MODEL,
    }
    old_story["visual_style_context"] = _visual_context("changed excerpt")
    engine = _engine_with_story_tree(old_story)
    monkeypatch.setattr(
        "common.global_style._generate_style_with_llm",
        lambda context: "new coherent visual style",
    )
    monkeypatch.setattr(
        "common.global_style._write_story_tree_file", lambda *args, **kwargs: None
    )

    class _Lock:
        def acquire(self, blocking=True):
            return True

        def release(self):
            return None

    class _Redis:
        def lock(self, *args, **kwargs):
            return _Lock()

    monkeypatch.setattr("redis.Redis.from_url", lambda *args, **kwargs: _Redis())

    assert ensure_global_style_generated("task-1", db_engine=engine) == (
        "new coherent visual style"
    )
    with engine.connect() as connection:
        stored = json.loads(
            connection.execute(
                text("SELECT data FROM story_tree_json WHERE task_id='task-1'")
            ).scalar_one()
        )
    assert stored["global_style"] == "new coherent visual style"
    assert stored[GLOBAL_STYLE_META_KEY]["input_digest"] == _global_style_input_digest(
        stored, []
    )
