from __future__ import annotations

from pathlib import Path

from PIL import Image

from common.visual_checkpoint import (
    canonical_digest,
    checkpoint_is_reusable,
    load_checkpoint,
    mark_success,
    reset_visual_checkpoint_schema_cache_for_tests,
)
from services.char_designer.core import character_manifest
from services.char_designer.core.generate_calm import _normalise_conversion_mapping
from services.char_designer.core.generate_cg_identity_references import (
    _identity_output_path,
)
from services.char_designer.utils.character_identity import (
    combination_filename,
    is_valid_resource_id,
    normalize_resource_id,
)


def _png(path: Path, *, rgba: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "RGBA" if rgba else "RGB"
    color = (20, 40, 60, 255) if rgba else (20, 40, 60)
    Image.new(mode, (16, 12), color).save(path, format="PNG")
    return path


def test_upstream_unicode_id_is_kept_but_path_like_values_are_rejected():
    value = "ChiZaiXiaDeShiYou（FuHeQuChiFanZhe）"
    assert normalize_resource_id(value) == value
    assert is_valid_resource_id("XunWen“YunLi”DanWeiDeTingZhong")
    assert is_valid_resource_id("XiaoMing'sPengYou")
    assert not is_valid_resource_id("A/B")
    assert not is_valid_resource_id("../character")
    assert not is_valid_resource_id("A\nB")


def test_character_filename_uses_the_authoritative_upstream_id():
    speaker_id = "ChiZaiXiaDeShiYou（FuHeQuChiFanZhe）"
    assert combination_filename(
        "池再夏的室友（附和去吃饭者）-1-1-1",
        prompt_record={"speaker_id": speaker_id},
    ) == "ChiZaiXiaDeShiYou（FuHeQuChiFanZhe）_1-1-1_calm_LS.png"


def test_character_loader_keeps_authoritative_id_for_active_combination():
    from services.char_designer.core import generate_calm

    name = "询问“云里”单位的听众"
    upstream_id = "XunWen“YunLi”DanWeiDeTingZhong"
    previous = generate_calm._conversion_cache
    generate_calm._conversion_cache = {name: upstream_id}
    try:
        data = {
            name: {
                "name": name,
                "personality_variants": [{"personality_id": 1}],
                "whole_body_variants": [
                    {
                        "whole_body_id": 1,
                        "age": "18",
                        "gender": "女",
                        "appearance": {},
                    }
                ],
                "clothing_variants": [{"clothing_id": 1}],
                "valid_status_combinations": [
                    {"combination": f"{name}-1-1-1", "is_active": True}
                ],
            }
        }
        characters = generate_calm.parse_characters_by_active_combinations(data)
        assert characters[0].speaker_id == upstream_id
    finally:
        generate_calm._conversion_cache = previous


def test_versioned_name_mapping_is_loaded_without_retransliteration():
    source_name = "池再夏的室友（附和去吃饭者）"
    upstream_id = "ChiZaiXiaDeShiYou（FuHeQuChiFanZhe）"
    data = {
        "original_name": {"conversion": {source_name: upstream_id}},
        "change_name": {"new-name": {}, "conversion": {}},
    }
    assert _normalise_conversion_mapping(data) == {source_name: upstream_id}


def test_cg_identity_reference_path_preserves_unicode_entity_id(tmp_path):
    entity_id = "XunWen“YunLi”DanWeiDeTingZhong"
    path = _identity_output_path(
        tmp_path / "task-cg",
        {"entity_id": entity_id, "file_candidates": []},
    )
    assert path.name == f"{entity_id}.png"


def test_unicode_character_manifest_is_valid(tmp_path):
    task_id = "task-unicode-manifest"
    speaker_id = "角色（别名）"
    filename = f"{speaker_id}_1-1-1_calm_LS.png"
    _png(tmp_path / task_id / "images" / "characters" / filename)

    manifest = character_manifest.build_character_image_manifest(
        task_id,
        outputs_root=tmp_path,
        prompt_records=[
            {
                "name": "角色（别名）",
                "speaker_id": speaker_id,
                "combination": "角色（别名）-1-1-1",
            }
        ],
    )
    assert manifest["assets"][0]["speaker_id"] == speaker_id
    assert manifest["assets"][0]["file_path"].endswith(filename)
    character_manifest.validate_character_image_manifest(
        manifest,
        outputs_root=tmp_path,
    )


def test_unicode_checkpoint_rechecks_actual_output(tmp_path):
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    reset_visual_checkpoint_schema_cache_for_tests()
    task_id = "task-unicode-checkpoint"
    speaker_id = "角色（别名）"
    output = _png(
        tmp_path
        / task_id
        / "images"
        / "characters"
        / f"{speaker_id}_1-1-1_calm_LS.png"
    )
    digest = canonical_digest({"speaker_id": speaker_id})
    mark_success(
        task_id,
        "char_designer",
        "generate_base_calm",
        f"{speaker_id}-1-1-1",
        input_digest=digest,
        generator_version="unicode-test-v1",
        output_paths=[output],
        validator=lambda path: Path(path).is_file()
        and Path(path).stat().st_size > 0,
        outputs_root=tmp_path,
        engine=engine,
    )
    record = load_checkpoint(
        task_id,
        "char_designer",
        "generate_base_calm",
        f"{speaker_id}-1-1-1",
        engine=engine,
    )
    assert checkpoint_is_reusable(
        record,
        task_id=task_id,
        input_digest=digest,
        generator_version="unicode-test-v1",
        output_paths=[output],
        validator=lambda path: Path(path).is_file()
        and Path(path).stat().st_size > 0,
        outputs_root=tmp_path,
    ).reusable
    output.unlink()
    assert not checkpoint_is_reusable(
        record,
        task_id=task_id,
        input_digest=digest,
        generator_version="unicode-test-v1",
        output_paths=[output],
        validator=lambda path: Path(path).is_file()
        and Path(path).stat().st_size > 0,
        outputs_root=tmp_path,
    ).reusable


def test_expand_canvas_height_lookup_uses_unicode_filename(monkeypatch):
    from services.char_designer.core import expand_canvas
    from services.char_designer.core import generate_calm

    speaker_id = "ChiZaiXiaDeShiYou（FuHeQuChiFanZhe）"
    combination = "池再夏的室友（附和去吃饭者）-1-1-1"

    class _Result:
        def fetchone(self):
            return ([{"combination": combination, "height": "168"}],)

    class _Connection:
        def execute(self, *_args, **_kwargs):
            return _Result()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Engine:
        def connect(self):
            return _Connection()

    # ``load_height_map`` is a standalone subprocess step; emulate the cache
    # initialization that normally reads the task's local/DB mapping.
    def _init(_task_id):
        generate_calm._conversion_cache = {
            "池再夏的室友（附和去吃饭者）": speaker_id
        }

    monkeypatch.setattr(expand_canvas, "_init_conversion_cache", _init)
    monkeypatch.setattr(expand_canvas, "platform_engine", _Engine())
    assert expand_canvas.load_height_map("task") == {
        f"{speaker_id}_1-1-1_calm_LS.png": "168"
    }
