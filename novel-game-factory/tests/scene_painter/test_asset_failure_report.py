from __future__ import annotations

import json

from PIL import Image

from services.scene_painter.core import asset_failure_report


def test_scene_failure_report_records_missing_cg_and_scene(tmp_path, monkeypatch):
    monkeypatch.setattr(asset_failure_report, "_checkpoint_failures", lambda task_id: [])
    task_root = tmp_path / "task-2"
    scene_dir = task_root / "images" / "scenes"
    scene_dir.mkdir(parents=True)
    Image.new("RGB", (4, 4), (0, 0, 0)).save(scene_dir / "scene_room.png")
    monkeypatch.setattr(
        asset_failure_report,
        "_load_json_table",
        lambda task_id, table: (
            {"scene_room": {"image_prompt": "room", "copy_count": 1}}
            if table == "scene_prompts"
            else ({"cg_1": {"cg_prompt": "event"}} if table == "cg" else {})
        ),
    )

    path = asset_failure_report.write_failure_report("task-2", outputs_root=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["status"] == "partial"
    assert payload["summary"]["generated"] == 1
    assert payload["summary"]["missing"] == 2
    assert {item["asset_id"] for item in payload["assets"]} == {
        "scene_room_1",
        "cg_1",
    }
