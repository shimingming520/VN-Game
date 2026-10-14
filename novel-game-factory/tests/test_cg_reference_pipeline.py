import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from common.visual_style import AssetStylePlan, STYLE_MODE_CUSTOM, STYLE_MODE_LLM
from services.char_designer import main as char_designer_main
from services.char_designer.core.generate_cg_identity_references import (
    collect_identity_requirements,
)
from services.info_extractor.cg import cg_build, cg_prompt
from services.info_extractor.plot.extract_plots_to_db import (
    _normalize_cg_visual_entities,
)
from services.scene_painter.core.cg_generator import (
    CGProcessor,
    CG_REFERENCE_CONTRACT_SCHEMA,
)
from services.scene_painter.image_providers.gpt_image import GPTImageModerationError


class FakeImageGenerator:
    def __init__(self):
        self.model = "fake-image-model"
        self.max_reference_images = 8
        self.calls = []

    def generate_with_reference(self, prompt, reference_image, output_path):
        self.calls.append((prompt, [reference_image]))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _write_png(output_path)
        return True

    def generate_with_references(self, prompt, reference_images, output_path):
        self.calls.append((prompt, list(reference_images)))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _write_png(output_path)
        return True


class ModerationOnceImageGenerator(FakeImageGenerator):
    def __init__(self):
        super().__init__()
        self.attempts = 0

    def generate_with_reference(self, prompt, reference_image, output_path):
        self.calls.append((prompt, [reference_image]))
        self.attempts += 1
        if self.attempts == 1:
            raise GPTImageModerationError(
                status_code=400,
                message="blocked",
                violations=["monster"],
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _write_png(output_path)
        return True


class ModerationAlwaysImageGenerator(ModerationOnceImageGenerator):
    def generate_with_reference(self, prompt, reference_image, output_path):
        self.calls.append((prompt, [reference_image]))
        self.attempts += 1
        raise GPTImageModerationError(
            status_code=400,
            message="blocked",
            violations=["monster"],
        )


class ReplacementLLM:
    def chat_completion_json(self, *_args, **_kwargs):
        return {
            "replacements": [
                {
                    "from": "monster",
                    "to": "creature",
                    "reason": "neutral wording",
                }
            ]
        }


def _write_png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (40, 60, 80)).save(path, format="PNG")
    return path


def _touch(path: Path) -> Path:
    return _write_png(path)


class CGReferencePipelineTests(unittest.TestCase):
    def test_char_designer_runs_identity_only_when_normal_prompts_are_empty(self):
        cg_data = {
            "cg_1": {
                "reference_contract_schema": CG_REFERENCE_CONTRACT_SCHEMA,
                "reference_requirements": [
                    {
                        "role": "character",
                        "required": True,
                        "reference_policy": "required_identity",
                    }
                ],
            }
        }
        original_loader = char_designer_main._load_character_generation_data
        try:
            char_designer_main._load_character_generation_data = lambda _task_id: ([], cg_data)
            self.assertTrue(char_designer_main._is_cg_identity_only_task("task"))
            steps = char_designer_main._build_character_generation_steps("task", None, None)
        finally:
            char_designer_main._load_character_generation_data = original_loader

        self.assertEqual([step[0] for step in steps], ["generate_cg_identity_references.py"])

    def test_normal_character_pipeline_no_longer_builds_contact_sheets(self):
        original_loader = char_designer_main._load_character_generation_data
        try:
            char_designer_main._load_character_generation_data = lambda _task_id: (
                [{"combination": "A-1-1-1"}],
                {},
            )
            steps = char_designer_main._build_character_generation_steps(
                "task", None, None
            )
        finally:
            char_designer_main._load_character_generation_data = original_loader

        self.assertNotIn("cg_char_img_stitch.py", [step[0] for step in steps])

    def test_annotation_normalizes_silent_visible_people_only(self):
        result = _normalize_cg_visual_entities(
            [
                {
                    "name": "小柳",
                    "entity_type": "human",
                    "reference_policy": "required_identity",
                },
                {"name": "烟雾", "entity_type": "effect"},
                {"name": "小柳", "entity_type": "human"},
            ]
        )
        self.assertEqual([item["name"] for item in result], ["小柳"])
        self.assertEqual(result[0]["reference_policy"], "required_identity")

    def test_cg_build_keeps_visual_entities_from_all_beats(self):
        flat = [
            (
                "chapter_1",
                {
                    "beat_id": "b1",
                    "cg": "cg_1",
                    "cg_visual_entities": [
                        {"name": "小柳", "entity_type": "human"}
                    ],
                },
            ),
            (
                "chapter_1",
                {
                    "beat_id": "b2",
                    "cg": "cg_1",
                    "cg_visual_entities": [
                        {"name": "曹麻子", "entity_type": "human"}
                    ],
                },
            ),
        ]
        groups = cg_build._group_cg_ranges(flat)
        self.assertEqual(
            [item["name"] for item in groups["cg_1"]["visual_entities"]],
            ["小柳", "曹麻子"],
        )

    def test_structured_planner_restores_declared_silent_character(self):
        parsed = {
            "selected_beat_ids": ["b2"],
            "single_moment": "the child reaches toward the woman",
            "required_visible_characters": [],
            "optional_visible_characters": [],
            "offscreen_characters": [],
            "init_description": "silent child reaching toward a kneeling woman",
        }
        visual_spec = cg_prompt._normalize_cg_visual_spec(
            parsed,
            [
                {
                    "name": "小柳",
                    "entity_type": "human",
                    "reference_policy": "required_identity",
                }
            ],
            [{"beat_id": "b1"}, {"beat_id": "b2"}],
        )
        self.assertIsNotNone(visual_spec)
        self.assertEqual(
            [item["name"] for item in visual_spec["required_visible_characters"]],
            ["小柳"],
        )

    def test_reference_requirements_use_normal_sprite_then_identity_fallback(self):
        visual_spec = {
            "required_visible_characters": [
                {"name": "曹麻子", "identity_description": "poor middle-aged woman"},
                {"name": "小柳", "identity_description": "young child with a broad mouth"},
            ],
            "optional_visible_characters": [],
        }
        requirements = cg_prompt._build_reference_requirements(
            visual_spec,
            scene_reference={
                "scene_id": "scene_shop",
                "entity_name": "曹家糟坊",
                "filename": "scene_shop.png",
                "source_beat_id": "b2",
            },
            characters_data={
                "曹麻子": {
                    "name": "曹麻子",
                    "speaker_id": "CaoMaZi",
                    "valid_status_combinations": [
                        {"combination": "曹麻子-1-1-1", "is_active": True}
                    ],
                }
            },
            cg_characters=[],
        )
        self.assertEqual(requirements[0]["role"], "scene")
        self.assertEqual(
            requirements[1]["file_candidates"][0],
            "images/characters/CaoMaZi_1-1-1_calm_LS.png",
        )
        self.assertTrue(
            requirements[2]["file_candidates"][-1].startswith("images/references/")
        )

    def test_identity_requirements_are_deduplicated_across_cgs(self):
        requirement = {
            "role": "character",
            "entity_id": "XiaoLiu",
            "entity_name": "小柳",
            "required": True,
            "reference_policy": "required_identity",
            "file_candidates": ["images/references/XiaoLiu.png"],
            "identity_prompt": "identity prompt",
        }
        cg_data = {
            "cg_1": {
                "reference_contract_schema": CG_REFERENCE_CONTRACT_SCHEMA,
                "reference_requirements": [requirement],
            },
            "cg_2": {
                "reference_contract_schema": CG_REFERENCE_CONTRACT_SCHEMA,
                "reference_requirements": [requirement],
            },
        }
        result = collect_identity_requirements(cg_data)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["entity_id"], "XiaoLiu")

    def test_structured_reference_order_scene_characters_anchor_last(self):
        with tempfile.TemporaryDirectory() as temp:
            task_dir = Path(temp) / "task"
            output_dir = task_dir / "images" / "cg"
            scene = _touch(task_dir / "images" / "scenes" / "scene_shop.png")
            first = _touch(task_dir / "images" / "characters" / "CaoMaZi.png")
            second = _touch(task_dir / "images" / "references" / "XiaoLiu.png")
            anchor = _touch(task_dir / "style_anchors" / "scene_anchor.png")
            generator = FakeImageGenerator()
            processor = CGProcessor(
                generator,
                output_dir,
                task_dir / "images" / "characters",
                task_dir / "images" / "scenes",
                "task",
                AssetStylePlan(
                    task_id="task",
                    asset_kind="scene",
                    mode=STYLE_MODE_CUSTOM,
                    style="custom scene style",
                    anchor_path=anchor,
                ),
            )
            spec = {
                "reference_contract_schema": CG_REFERENCE_CONTRACT_SCHEMA,
                "cg_prompt": "two people in one decisive moment",
                "cg_visual_spec": {"offscreen_characters": []},
                "reference_requirements": [
                    {
                        "role": "character",
                        "entity_id": "CaoMaZi",
                        "entity_name": "曹麻子",
                        "required": True,
                        "file_candidates": ["images/characters/CaoMaZi.png"],
                    },
                    {
                        "role": "scene",
                        "entity_id": "scene_shop",
                        "entity_name": "曹家糟坊",
                        "required": True,
                        "file_candidates": ["images/scenes/scene_shop.png"],
                    },
                    {
                        "role": "character",
                        "entity_id": "XiaoLiu",
                        "entity_name": "小柳",
                        "required": True,
                        "file_candidates": ["images/references/XiaoLiu.png"],
                    },
                ],
            }
            references, omitted = processor._resolve_structured_references("cg_1", spec)
            self.assertFalse(omitted)
            self.assertEqual(
                [item["path"] for item in references],
                [scene.resolve(), first.resolve(), second.resolve(), anchor],
            )
            self.assertEqual([item["image_index"] for item in references], [1, 2, 3, 4])
            prompt = processor._compose_structured_prompt(spec, references, omitted)
            self.assertIn("Use Image 1 as the exact 曹家糟坊 location reference", prompt)
            self.assertIn("Use Image 2 as the official 曹麻子 character identity reference", prompt)
            self.assertIn("Use Image 3 as the official 小柳 character identity reference", prompt)
            self.assertIn("Use Image 4 only as the final scene rendering-style Anchor", prompt)

    def test_missing_required_identity_blocks_before_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            task_dir = Path(temp) / "task"
            output_dir = task_dir / "images" / "cg"
            _touch(task_dir / "images" / "scenes" / "scene_shop.png")
            generator = FakeImageGenerator()
            processor = CGProcessor(
                generator,
                output_dir,
                task_dir / "images" / "characters",
                task_dir / "images" / "scenes",
                "task",
                AssetStylePlan(
                    task_id="task",
                    asset_kind="scene",
                    mode=STYLE_MODE_LLM,
                    style="llm scene style",
                ),
            )
            result = processor._process_structured(
                "cg_1",
                {
                    "reference_contract_schema": CG_REFERENCE_CONTRACT_SCHEMA,
                    "cg_prompt": "one moment",
                    "reference_requirements": [
                        {
                            "role": "scene",
                            "entity_id": "scene_shop",
                            "required": True,
                            "file_candidates": ["images/scenes/scene_shop.png"],
                        },
                        {
                            "role": "character",
                            "entity_id": "XiaoLiu",
                            "entity_name": "小柳",
                            "required": True,
                            "file_candidates": ["images/references/XiaoLiu.png"],
                        },
                    ],
                },
            )
            self.assertFalse(result.success)
            self.assertFalse(result.retryable)
            self.assertEqual(generator.calls, [])

    def test_structured_cg_uses_minimal_repair_after_moderation_only(self):
        with tempfile.TemporaryDirectory() as temp:
            task_dir = Path(temp) / "task"
            output_dir = task_dir / "images" / "cg"
            _touch(task_dir / "images" / "scenes" / "scene_shop.png")
            generator = ModerationOnceImageGenerator()
            processor = CGProcessor(
                generator,
                output_dir,
                task_dir / "images" / "characters",
                task_dir / "images" / "scenes",
                "task",
                AssetStylePlan(
                    task_id="task",
                    asset_kind="scene",
                    mode=STYLE_MODE_LLM,
                    style="",
                ),
                prompt_repair_llm_client=ReplacementLLM(),
            )
            result = processor._process_structured(
                "cg_1",
                {
                    "reference_contract_schema": CG_REFERENCE_CONTRACT_SCHEMA,
                    "cg_prompt": "a child stares at a monster in a courtyard",
                    "cg_visual_spec": {"single_moment": "a child stares at a monster"},
                    "reference_requirements": [
                        {
                            "role": "scene",
                            "entity_id": "scene_shop",
                            "entity_name": "courtyard",
                            "required": True,
                            "file_candidates": ["images/scenes/scene_shop.png"],
                        }
                    ],
                },
            )

            assert result.success
            assert len(generator.calls) == 2
            assert "a child stares at a monster" not in generator.calls[1][0]
            assert "a child stares at a creature" in generator.calls[1][0]
            assert "creature" in generator.calls[1][0]
            assert "16:9 aspect ratio" in generator.calls[1][0]
            assert "REFERENCE IMAGE CONTRACT" in generator.calls[1][0]
            assert "Use Image 1 as the exact courtyard location reference" in generator.calls[1][0]

    def test_structured_cg_does_not_fallback_after_repair_exhaustion(self):
        with tempfile.TemporaryDirectory() as temp:
            task_dir = Path(temp) / "task"
            _touch(task_dir / "images" / "scenes" / "scene_shop.png")
            generator = ModerationAlwaysImageGenerator()
            processor = CGProcessor(
                generator,
                task_dir / "images" / "cg",
                task_dir / "images" / "characters",
                task_dir / "images" / "scenes",
                "task",
                AssetStylePlan(
                    task_id="task",
                    asset_kind="scene",
                    mode=STYLE_MODE_LLM,
                    style="",
                ),
                prompt_repair_llm_client=ReplacementLLM(),
            )
            result = processor._process_structured(
                "cg_1",
                {
                    "reference_contract_schema": CG_REFERENCE_CONTRACT_SCHEMA,
                    "cg_prompt": "a child stares at a monster in a courtyard",
                    "reference_requirements": [
                        {
                            "role": "scene",
                            "entity_id": "scene_shop",
                            "entity_name": "courtyard",
                            "required": True,
                            "file_candidates": ["images/scenes/scene_shop.png"],
                        }
                    ],
                },
            )

            assert not result.success
            assert not result.retryable
            assert "修复上限" in result.error_message
            assert len(generator.calls) == 2

    def test_structured_contract_rejects_multiple_required_scenes(self):
        with tempfile.TemporaryDirectory() as temp:
            task_dir = Path(temp) / "task"
            first = _touch(task_dir / "images" / "scenes" / "first.png")
            second = _touch(task_dir / "images" / "scenes" / "second.png")
            generator = FakeImageGenerator()
            processor = CGProcessor(
                generator,
                task_dir / "images" / "cg",
                task_dir / "images" / "characters",
                task_dir / "images" / "scenes",
                "task",
                AssetStylePlan(
                    task_id="task",
                    asset_kind="scene",
                    mode=STYLE_MODE_LLM,
                    style="style",
                ),
            )
            with self.assertRaisesRegex(RuntimeError, "1 个 required 主场景"):
                processor._resolve_structured_references(
                    "cg_1",
                    {
                        "reference_requirements": [
                            {
                                "role": "scene",
                                "required": True,
                                "file_candidates": [str(first.relative_to(task_dir))],
                            },
                            {
                                "role": "scene",
                                "required": True,
                                "file_candidates": [str(second.relative_to(task_dir))],
                            },
                        ]
                    },
                )

    def test_legacy_spec_isolated_from_structured_dispatch(self):
        processor = object.__new__(CGProcessor)
        processor._process_legacy = Mock(return_value="legacy")
        processor._process_structured = Mock(return_value="structured")
        self.assertEqual(processor._process_single("cg_1", {"cg_prompt": "old"}), "legacy")
        processor._process_legacy.assert_called_once()
        processor._process_structured.assert_not_called()

    def test_legacy_contact_sheet_generation_is_blocked_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            task_dir = Path(temp) / "task"
            generator = FakeImageGenerator()
            processor = CGProcessor(
                generator,
                task_dir / "images" / "cg",
                task_dir / "images" / "characters",
                task_dir / "images" / "scenes",
                "task",
                AssetStylePlan(
                    task_id="task",
                    asset_kind="scene",
                    mode=STYLE_MODE_LLM,
                    style="style",
                ),
            )
            result = processor._process_legacy(
                "cg_legacy",
                {
                    "cg_prompt": "one historical moment",
                    "char_img_att": ["cg_legacy_char_1.png"],
                },
            )

            self.assertFalse(result.success)
            self.assertFalse(result.retryable)
            self.assertIn("cg-reference-v1", result.error_message)
            self.assertEqual(generator.calls, [])


if __name__ == "__main__":
    unittest.main()
