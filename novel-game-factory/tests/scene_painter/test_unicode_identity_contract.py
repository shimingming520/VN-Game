from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from common.visual_style import AssetStylePlan, STYLE_MODE_CUSTOM
from services.game_builder.converter import FinalConverter
from services.scene_painter.core import finalize_character_asset_names as finalizer
from services.scene_painter.core.cg_generator import (
    CGProcessor,
    CG_REFERENCE_CONTRACT_SCHEMA,
)


def _png(path: Path, *, rgba: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "RGBA" if rgba else "RGB"
    color = (20, 40, 60, 255) if rgba else (20, 40, 60)
    Image.new(mode, (16, 12), color).save(path, format="PNG")
    return path


def test_finalizer_accepts_unicode_ids_and_updates_manifest_and_cg(tmp_path, monkeypatch):
    task_id = "task-unicode-finalize"
    task_root = tmp_path / task_id
    old_id = "询问“云里”单位的听众"
    new_id = "询问“云里”单位的新称呼"
    old_filename = f"{old_id}_1-1-1_calm_LS.png"
    new_filename = f"{new_id}_1-1-1_calm_LS.png"
    old_path = _png(task_root / "images" / "characters" / old_filename)

    mapping = {
        "original_name": {"conversion": {"原始听众": old_id}},
        "change_name": {
            "new-name": {"原始听众": "新听众"},
            "conversion": {"新听众": new_id},
        },
    }
    cg_data = {
        "cg_1": {
            "reference_requirements": [
                {
                    "role": "character",
                    "required": True,
                    "entity_id": old_id,
                    "file_candidates": [f"images/characters/{old_filename}"],
                }
            ]
        }
    }
    manifest_path = task_root / "manifests" / "character_image_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    source_manifest = {
        "schema_version": "character-image-v1",
        "manifest_type": "character_image",
        "task_id": task_id,
        "assets": [
            {
                "asset_id": f"character:{old_id}:1-1-1:calm:LS",
                "asset_type": "character",
                "status": "generated",
                "file_path": f"images/characters/{old_filename}",
                "speaker_id": old_id,
                "logical_character_id": old_id,
                "combination": "原始听众-1-1-1",
                "emotion": "calm",
                "shot_type": "LS",
                "asset_role": "identity_base",
                "consistency_roles": [],
            }
        ],
    }
    manifest_path.write_text(
        json.dumps(source_manifest, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(finalizer, "ensure_stage_lease_owned", lambda *args: True)

    result = finalizer.finalize_character_asset_names(
        task_id,
        outputs_root=tmp_path,
        mapping_data=mapping,
        cg_data=cg_data,
        persist_cg=False,
        sync_oss=False,
    )

    assert not old_path.exists()
    assert (task_root / "images" / "characters" / new_filename).exists()
    assert result.rename_map == {old_id: new_id}
    assert result.cg_data["cg_1"]["reference_requirements"][0][
        "file_candidates"
    ][0] == f"images/characters/{new_filename}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    asset = manifest["assets"][0]
    assert asset["speaker_id"] == new_id
    assert asset["logical_character_id"] == new_id
    assert asset["file_path"] == f"images/characters/{new_filename}"
    assert asset["asset_id"] == f"character:{new_id}:1-1-1:calm:LS"


def test_fake_cg_provider_and_game_builder_preserve_unicode_identity(tmp_path):
    task_root = tmp_path / "task-e2e"
    scene_path = _png(
        task_root / "images" / "scenes" / "scene-room.png", rgba=False
    )
    character_id = "ChiZaiXiaDeShiYou（FuHeQuChiFanZhe）"
    character_path = _png(
        task_root
        / "images"
        / "characters"
        / f"{character_id}_1-1-1_calm_LS.png"
    )
    anchor_path = _png(
        task_root / "style_anchors" / "scene_anchor.png", rgba=False
    )

    class FakeCgProvider:
        model = "fake-image-model"
        max_reference_images = 8
        supports_edit = True
        supports_anchor_edit = True

        def generate_with_references(self, prompt, references, output_path):
            assert references[0] == scene_path
            assert character_path in references
            assert references[-1] == anchor_path
            _png(output_path, rgba=False)
            return True

    processor = CGProcessor(
        FakeCgProvider(),
        task_root / "images" / "cg",
        task_root / "images" / "characters",
        task_root / "images" / "scenes",
        "task-e2e",
        AssetStylePlan(
            task_id="task-e2e",
            asset_kind="scene",
            mode=STYLE_MODE_CUSTOM,
            style="test style",
            anchor_path=anchor_path,
        ),
    )
    result = processor._process_structured(
        "cg_1",
        {
            "reference_contract_schema": CG_REFERENCE_CONTRACT_SCHEMA,
            "cg_prompt": "one coherent moment",
            "cg_visual_spec": {"offscreen_characters": []},
            "reference_requirements": [
                {
                    "role": "scene",
                    "entity_id": "scene-room",
                    "entity_name": "room",
                    "required": True,
                    "file_candidates": ["images/scenes/scene-room.png"],
                },
                {
                    "role": "character",
                    "entity_id": character_id,
                    "entity_name": "池再夏的室友（附和去吃饭者）",
                    "required": True,
                    "file_candidates": [
                        f"images/characters/{character_path.name}"
                    ],
                },
            ],
        },
    )
    assert result.success is True
    cg_path = task_root / "images" / "cg" / "cg_1.png"
    assert cg_path.is_file()

    template_dir = (
        Path(__file__).resolve().parents[2]
        / "services"
        / "game_builder"
        / "game_template"
        / "shijuexiaoshuo"
    )
    build_root = tmp_path / "built-e2e"
    (build_root / "assets").mkdir(parents=True)
    converter = FinalConverter(
        source_dir=task_root,
        template_dir=template_dir,
        output_dir=build_root,
        images_base_dir=task_root,
        task_id="task-e2e",
        enable_oss_upload=False,
        delete_local_assets=False,
    )
    converter.scan_assets()
    copied = converter.copy_assets_for_chapter(
        {
            "materials": [],
            "start_index": 0,
            "end_index": 0,
            "needed_scene_files": {"scene-room.png", "cg_1.png"},
            "needed_char_pngs": {character_path.name},
        }
    )
    assert copied["characters"][0]["name"] == character_path.name
    assert (build_root / "assets" / "characters" / character_path.name).is_file()
    assert (build_root / "assets" / "scenes" / "scene-room.png").is_file()
    assert (build_root / "assets" / "scenes" / "cg_1.png").is_file()
    definitions = converter.generate_character_definitions()
    assert character_id in definitions
    assert character_path.name in definitions
