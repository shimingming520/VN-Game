# Project Code Map

## Story Planner

| Concern | Existing implementation |
| --- | --- |
| Task entrypoints | `novel-game-factory/services/story_planner/tasks.py` |
| CLI and service entry | `novel-game-factory/services/story_planner/main.py` |
| Upload and task creation | `creation-engine/gateway/creation.py`, `creation-engine-frontend/src/api/modules/creation.js` |
| Input preflight | `core/novel_preflight.py`, `core/novel_text_encoding.py` |
| Source preprocessing and chapter extraction | `novel-game-factory/services/story_planner/core/novel_reader.py`, `novel-game-factory/services/story_planner/tasks.py` |
| Name anonymization and mappings | `novel-game-factory/common/name_mapping_utils.py`, `novel-game-factory/common/generic_appellations.py`, `novel-game-factory/services/info_extractor/character/name_mappings.py` |
| Source language detection and prose translation | `novel-game-factory/services/story_planner/tasks.py`, `novel-game-factory/services/story_planner/batch_translate.py`, `creation-engine/gateway/translate.py` |
| Content warnings | `novel-game-factory/services/story_planner/core/content_warnings.py` |
| Source reading and extraction | `core/novel_reader.py`, `core/world_analyst.py` |
| Long-text processing | `core/hierarchical_processing.py`, `core/prompt_budget.py` |
| Main story and branch planning | `core/branch_designer.py` |
| Soft branches and effects | `core/soft_branch.py`, `core/effects_validator.py` |
| Chapter prose | `core/story_generator.py`, `core/hook_generator.py` |
| Endings | `core/ending_writer.py` |
| Player-visible story-tree localization | `core/story_tree_player_localization.py`, `core/content_warnings.py` |
| Branch execution | `core/chapter_branch_runtime.py` |
| Quality and publication checks | `core/chapter_branch_quality.py`, `core/chapter_branch_artifacts.py` |
| Runtime observability | `core/runtime_observability.py` |
| Chapter-plan checkpoints and resume | `core/chapter_branch_checkpoint.py`, `core/chapter_branch_resume.py` |

## Media Services

The `story_planner` service does not generate media itself, but the project has
separate execution services:

| Concern | Existing implementation |
| --- | --- |
| Character images and character manifest | `services/char_designer/core/character_manifest.py` |
| Scene, background, and CG images | `services/scene_painter/core/scene_generator.py` |
| Per-asset visual checkpoints and resume | `common/visual_checkpoint.py` |
| Style anchors and visual input preflight | `common/style_anchor_generation.py`, `common/visual_input_preflight.py`, `common/visual_style.py` |
| Player-visible story-tree locale selection | `common/story_tree_locale.py`, `services/story_planner/core/story_tree_player_localization.py` |
| Voice generation and WAV acceptance | `services/audio_producer/main.py`, `services/audio_producer/tasks.py` |
| BGM, SFX, and ambience generation and cache reuse | `services/sound_arranger/generate_sound.py` |

The character service currently publishes a concrete
`manifests/character_image_manifest.json` under a task output. Its v1 records
include `task_id`, `schema_version`, `manifest_type`, `assets`, `asset_id`,
`asset_type`, `file_path`, `speaker_id`, `logical_character_id`, `combination`,
`emotion`, `shot_type`, `asset_role`, and `consistency_roles`.

Do not describe the whole project as lacking media execution. Conversely, do
not claim that a single unified scene manifest already covers scene images,
voice, BGM, SFX, and ambience unless the target builder provides and validates
that contract. The asset-integration Skill is the cross-service handoff
contract for that gap.

## Existing Planning References

- `novel-game-factory/services/story_planner/CHAPTER_BRANCH_OPTIMIZATION_PLAN.md`
- `novel-game-factory/services/story_planner/LARGE_NOVEL_PRODUCTION_PLAN.md`

Use these as implementation context. Do not treat a planning document as proof that every listed feature is currently enabled; verify the code path when execution matters.
