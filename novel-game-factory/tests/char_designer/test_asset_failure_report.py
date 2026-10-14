from __future__ import annotations

import json

from PIL import Image

from services.char_designer.core import asset_failure_report


def test_character_failure_report_is_atomic_and_contains_missing_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(asset_failure_report, "_checkpoint_failures", lambda task_id: [])
    task_root = tmp_path / "task-1"
    image_dir = task_root / "images" / "characters"
    image_dir.mkdir(parents=True)
    Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(
        image_dir / "Hero_1-1-1_calm_LS.png"
    )
    monkeypatch.setattr(
        asset_failure_report,
        "_load_json_table",
        lambda task_id, table: (
            [{"speaker_id": "Hero", "combination": "Hero-1-1-1", "emotions": ["calm"]}]
            if table == "character_prompts"
            else {}
        ),
    )

    path = asset_failure_report.write_failure_report("task-1", outputs_root=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["status"] == "partial"
    assert payload["summary"]["generated"] == 1
    assert payload["summary"]["missing"] == 2
    assert {item["asset_id"] for item in payload["assets"]} == {
        "Hero_1-1-1_calm_MS",
        "Hero_1-1-1_calm_CS",
    }
