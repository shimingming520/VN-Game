from pathlib import Path
import json

from PIL import Image

from services.scene_painter.core import finalize_character_asset_names as module


def _png(path: Path, color) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (12, 12), color).save(path, format="PNG")
    return path


def _rgba(path: Path, size, color) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, color).save(path, format="PNG")
    return path


def test_synchronize_replayed_assets_replaces_stale_final_copy(tmp_path, monkeypatch):
    task_id = "task-rename"
    char_dir = tmp_path / task_id / "images" / "characters"
    old_path = _png(char_dir / "OldId_1-1-1_calm_LS.png", (200, 10, 10))
    new_path = _png(char_dir / "NewId_1-1-1_calm_LS.png", (10, 10, 200))
    _png(
        tmp_path
        / task_id
        / ".resume_sources"
        / "char_designer"
        / "ls"
        / old_path.name,
        (200, 10, 10),
    )
    mapping = {
        "original_name": {"conversion": {"旧名": "OldId"}},
        "change_name": {
            "new-name": {"旧名": "新名"},
            "conversion": {"新名": "NewId"},
        },
    }
    monkeypatch.setattr(module, "ensure_stage_lease_owned", lambda *args: True)
    monkeypatch.setattr(module, "_load_cg_data_optional", lambda *args: None)

    result = module.synchronize_replayed_character_assets(
        task_id,
        outputs_root=tmp_path,
        mapping_data=mapping,
    )

    assert result == {"OldId": "NewId"}
    assert module.file_sha256(old_path) == module.file_sha256(new_path)
    # Old path stays available until the final rename transaction completes.
    assert old_path.exists()


def test_synchronize_replayed_assets_rejects_unproven_old_file(tmp_path, monkeypatch):
    task_id = "task-unproven"
    char_dir = tmp_path / task_id / "images" / "characters"
    _png(char_dir / "OldId_1-1-1_calm_LS.png", (200, 10, 10))
    _png(char_dir / "NewId_1-1-1_calm_LS.png", (10, 10, 200))
    mapping = {
        "original_name": {"conversion": {"旧名": "OldId"}},
        "change_name": {
            "new-name": {"旧名": "新名"},
            "conversion": {"新名": "NewId"},
        },
    }
    monkeypatch.setattr(module, "ensure_stage_lease_owned", lambda *args: True)

    try:
        module.synchronize_replayed_character_assets(
            task_id, outputs_root=tmp_path, mapping_data=mapping
        )
    except module.CharacterAssetNameFinalizationError as exc:
        assert "恢复源证明" in str(exc)
    else:
        raise AssertionError("unproven old-ID file must not overwrite final output")


def test_synchronize_replayed_assets_accepts_expanded_ls_and_derived_shots(
    tmp_path, monkeypatch
):
    task_id = "task-derived-shots"
    task_root = tmp_path / task_id
    char_dir = task_root / "images" / "characters"
    resume_dir = task_root / ".resume_sources" / "char_designer" / "ls"

    raw = _rgba(resume_dir / "OldId_1-1-1_happy_LS.png", (8, 8), (40, 80, 120, 255))
    with Image.open(raw) as source:
        expanded = Image.new("RGBA", (8, 11), (0, 0, 0, 0))
        expanded.paste(source.convert("RGBA"), (0, 3))
    old_ls = char_dir / "OldId_1-1-1_happy_LS.png"
    old_ls.parent.mkdir(parents=True, exist_ok=True)
    expanded.save(old_ls, format="PNG")
    old_ms = char_dir / "OldId_1-1-1_happy_MS.png"
    old_cs = char_dir / "OldId_1-1-1_happy_CS.png"
    expanded.crop((0, 0, 8, 9)).save(old_ms, format="PNG")
    expanded.crop((0, 0, 8, 7)).save(old_cs, format="PNG")

    new_paths = [
        _rgba(char_dir / "NewId_1-1-1_happy_LS.png", (8, 11), (200, 10, 10, 255)),
        _rgba(char_dir / "NewId_1-1-1_happy_MS.png", (8, 9), (200, 10, 10, 255)),
        _rgba(char_dir / "NewId_1-1-1_happy_CS.png", (8, 7), (200, 10, 10, 255)),
    ]
    mapping = {
        "original_name": {"conversion": {"旧名": "OldId"}},
        "change_name": {
            "new-name": {"旧名": "新名"},
            "conversion": {"新名": "NewId"},
        },
    }
    monkeypatch.setattr(module, "ensure_stage_lease_owned", lambda *args: True)
    monkeypatch.setattr(module, "_load_cg_data_optional", lambda *args: None)

    module.synchronize_replayed_character_assets(
        task_id,
        outputs_root=tmp_path,
        mapping_data=mapping,
    )

    for old_path, new_path in zip((old_ls, old_ms, old_cs), new_paths):
        assert module.file_sha256(old_path) == module.file_sha256(new_path)


def test_synchronize_replayed_assets_rejects_invalid_derived_shot(
    tmp_path, monkeypatch
):
    task_id = "task-invalid-derived-shot"
    task_root = tmp_path / task_id
    char_dir = task_root / "images" / "characters"
    resume_dir = task_root / ".resume_sources" / "char_designer" / "ls"
    _rgba(resume_dir / "OldId_1-1-1_happy_LS.png", (8, 8), (40, 80, 120, 255))
    _rgba(char_dir / "OldId_1-1-1_happy_LS.png", (8, 8), (40, 80, 120, 255))
    _rgba(char_dir / "OldId_1-1-1_happy_MS.png", (8, 6), (1, 2, 3, 255))
    _rgba(char_dir / "NewId_1-1-1_happy_LS.png", (8, 8), (40, 80, 120, 255))
    _rgba(char_dir / "NewId_1-1-1_happy_MS.png", (8, 6), (200, 10, 10, 255))
    mapping = {
        "original_name": {"conversion": {"旧名": "OldId"}},
        "change_name": {
            "new-name": {"旧名": "新名"},
            "conversion": {"新名": "NewId"},
        },
    }
    monkeypatch.setattr(module, "ensure_stage_lease_owned", lambda *args: True)

    try:
        module.synchronize_replayed_character_assets(
            task_id, outputs_root=tmp_path, mapping_data=mapping
        )
    except module.CharacterAssetNameFinalizationError as exc:
        assert "恢复源证明" in str(exc)
    else:
        raise AssertionError("invalid derived shot must not overwrite final output")


def test_debug_record_replay_replaces_stale_final_record(tmp_path, monkeypatch):
    task_id = "task-debug-replay"
    task_root = tmp_path / task_id
    char_dir = task_root / "images" / "characters"
    resume_dir = task_root / ".resume_sources" / "char_designer" / "ls"
    debug_dir = task_root / "style_anchors" / "debug_assets" / "characters"
    _rgba(resume_dir / "OldId_1-1-1_happy_LS.png", (8, 8), (40, 80, 120, 255))
    _rgba(char_dir / "NewId_1-1-1_happy_LS.png", (8, 8), (40, 80, 120, 255))
    debug_dir.mkdir(parents=True, exist_ok=True)
    old_debug = debug_dir / "OldId_1-1-1_happy_LS.png.json"
    new_debug = debug_dir / "NewId_1-1-1_happy_LS.png.json"
    old_debug.write_text(
        json.dumps(
            {
                "filename": "OldId_1-1-1_happy_LS.png",
                "recorded_at": "new-attempt",
                "reference_images": [
                    {
                        "name": "OldId_1-1-1_calm_LS.png",
                        "relative_path": "images/characters/OldId_1-1-1_calm_LS.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    new_debug.write_text(
        json.dumps(
            {
                "filename": "NewId_1-1-1_happy_LS.png",
                "recorded_at": "stale-attempt",
            }
        ),
        encoding="utf-8",
    )
    mapping = {"OldId": "NewId"}
    monkeypatch.setattr(module, "ensure_stage_lease_owned", lambda *args: True)

    updated, renamed = module._update_debug_records(
        task_id, task_root, mapping
    )

    assert not old_debug.exists()
    payload = json.loads(new_debug.read_text(encoding="utf-8"))
    assert payload["recorded_at"] == "new-attempt"
    assert payload["filename"] == "NewId_1-1-1_happy_LS.png"
    assert payload["reference_images"][0]["relative_path"].startswith(
        "images/characters/NewId_"
    )
    assert updated == [new_debug]
    assert module.AssetPathPair(old_debug, new_debug) in renamed


def test_finalization_accepts_equivalent_old_and_new_files(tmp_path, monkeypatch):
    task_id = "task-equivalent"
    char_dir = tmp_path / task_id / "images" / "characters"
    old_path = _png(char_dir / "OldId_1-1-1_calm_LS.png", (20, 30, 40))
    new_path = _png(char_dir / "NewId_1-1-1_calm_LS.png", (20, 30, 40))
    pair = module.AssetPathPair(old_path, new_path)

    module._validate_asset_pairs([pair], allow_equivalent_duplicates=True)
    monkeypatch.setattr(module, "ensure_stage_lease_owned", lambda *args: True)
    renamed = module._rename_images(task_id, tmp_path / task_id, [pair])

    assert not old_path.exists()
    assert new_path.exists()
    assert renamed == ["images/characters/NewId_1-1-1_calm_LS.png"]
