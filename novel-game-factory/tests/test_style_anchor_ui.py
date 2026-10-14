import io

from fastapi.testclient import TestClient
from PIL import Image

from common.visual_style import ASSET_CHARACTER, ASSET_SCENE
from experiments.style_anchor_ui.app import app


def _png_bytes(color):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_uploaded_anchors_require_separate_confirmation(monkeypatch, tmp_path):
    monkeypatch.setattr("common.visual_style.OUTPUTS_PATH", str(tmp_path))
    monkeypatch.setattr("common.visual_style.fetch_output_file", lambda *args, **kwargs: None)
    monkeypatch.setattr("common.visual_style.mirror_outputs", lambda *args, **kwargs: 0)
    client = TestClient(app)
    task_id = "style-ui-test"

    response = client.put(
        f"/api/tasks/{task_id}/style",
        json={
            "mode": "custom",
            "character_global_style": "rough ink linework",
            "scene_global_style": "soft gouache atmosphere",
        },
    )
    assert response.status_code == 200
    assert response.json()["ready_to_start"] is False

    for kind, color in ((ASSET_CHARACTER, "red"), (ASSET_SCENE, "blue")):
        response = client.post(
            f"/api/tasks/{task_id}/anchors/{kind}/upload",
            files={"image": (f"{kind}.png", _png_bytes(color), "image/png")},
        )
        assert response.status_code == 200
        assert response.json()["sha256"]

    response = client.get(f"/api/tasks/{task_id}/style")
    assert response.json()["ready_to_start"] is False

    response = client.post(
        f"/api/tasks/{task_id}/anchors/{ASSET_CHARACTER}/confirm"
    )
    assert response.status_code == 200
    assert response.json()[ASSET_CHARACTER]["confirmed"] is True
    assert response.json()["ready_to_start"] is False

    response = client.post(f"/api/tasks/{task_id}/anchors/{ASSET_SCENE}/confirm")
    assert response.status_code == 200
    body = response.json()
    assert body[ASSET_SCENE]["confirmed"] is True
    assert body["ready_to_start"] is True


def test_changing_uploaded_anchor_notes_requires_reconfirmation(monkeypatch, tmp_path):
    monkeypatch.setattr("common.visual_style.OUTPUTS_PATH", str(tmp_path))
    monkeypatch.setattr("common.visual_style.fetch_output_file", lambda *args, **kwargs: None)
    monkeypatch.setattr("common.visual_style.mirror_outputs", lambda *args, **kwargs: 0)
    client = TestClient(app)
    task_id = "style-ui-reconfirm"

    client.put(
        f"/api/tasks/{task_id}/style",
        json={"mode": "custom", "character_global_style": "ink", "scene_global_style": "gouache"},
    )
    for kind in (ASSET_CHARACTER, ASSET_SCENE):
        client.post(
            f"/api/tasks/{task_id}/anchors/{kind}/upload",
            files={"image": (f"{kind}.png", _png_bytes("green"), "image/png")},
        )
        client.post(f"/api/tasks/{task_id}/anchors/{kind}/confirm")

    response = client.put(
        f"/api/tasks/{task_id}/style",
        json={
            "mode": "custom",
            "character_global_style": "ink with colder shadows",
            "scene_global_style": "gouache",
        },
    )
    body = response.json()
    assert body[ASSET_CHARACTER]["exists"] is True
    assert body[ASSET_CHARACTER]["confirmed"] is False
    assert body[ASSET_CHARACTER]["stale"] is False
    assert body[ASSET_SCENE]["confirmed"] is True
    assert body["ready_to_start"] is False


def test_reuse_preflight_endpoint_exposes_visual_only_scope(monkeypatch):
    monkeypatch.setattr(
        "experiments.style_anchor_ui.app.preflight_source_task",
        lambda source_task_id: {
            "source_task_id": source_task_id,
            "eligible": True,
            "errors": [],
            "warnings": [],
            "tables": {"character_prompts": 1, "scene_prompts": 1},
            "source_task": {"project_name": "source"},
            "pipeline_scope": {
                "reused": ["story_planner", "info_extractor"],
                "will_run": ["char_designer", "scene_painter"],
                "will_not_run": [
                    "audio_producer",
                    "sound_arranger",
                    "game_builder",
                ],
            },
        },
    )
    client = TestClient(app)

    response = client.post(
        "/api/reuse/preflight",
        json={"source_task_id": "source-task"},
    )

    assert response.status_code == 200
    assert response.json()["pipeline_scope"]["will_run"] == [
        "char_designer",
        "scene_painter",
    ]


def test_start_visual_only_endpoint_does_not_expand_pipeline(monkeypatch):
    monkeypatch.setattr(
        "experiments.style_anchor_ui.app.start_visual_only_pipeline",
        lambda task_id: {
            "success": True,
            "task_id": task_id,
            "pipeline": ["char_designer", "scene_painter"],
            "excluded": ["audio_producer", "sound_arranger", "game_builder"],
        },
    )
    client = TestClient(app)

    response = client.post("/api/tasks/derived-task/start-visual-only")

    assert response.status_code == 200
    assert response.json()["pipeline"] == ["char_designer", "scene_painter"]
    assert "audio_producer" in response.json()["excluded"]


def test_style_endpoint_persists_character_matting_switch(monkeypatch, tmp_path):
    monkeypatch.setattr("common.visual_style.OUTPUTS_PATH", str(tmp_path))
    monkeypatch.setattr("common.visual_style.fetch_output_file", lambda *args, **kwargs: None)
    monkeypatch.setattr("common.visual_style.mirror_outputs", lambda *args, **kwargs: 0)
    client = TestClient(app)

    response = client.put(
        "/api/tasks/style-ui-matting/style",
        json={
            "mode": "llm",
            "character_global_style": "",
            "scene_global_style": "",
            "remove_character_background": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["remove_character_background"] is False
    assert client.get("/api/tasks/style-ui-matting/style").json()[
        "remove_character_background"
    ] is False


def test_experiment_ui_exposes_progress_preview_debug_and_matting_controls():
    client = TestClient(app)

    index = client.get("/")
    debug = client.get("/debug")

    assert index.status_code == 200
    assert "本地美术资源进度" in index.text
    assert "启动抠图" in index.text
    assert "关闭抠图" in index.text
    assert "开发者调试" in index.text
    assert debug.status_code == 200
    assert "最终提示词" in debug.text
    assert "图生图参考" in debug.text
