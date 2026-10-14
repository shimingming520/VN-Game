# Content Warning Contract

## Implementation Map

| Concern | Implementation |
| --- | --- |
| Detection, normalization, localization, validation | `novel-game-factory/services/story_planner/core/content_warnings.py` |
| Story-tree integration | `novel-game-factory/services/story_planner/core/story_tree_player_localization.py` |
| Checkpoint orchestration | `novel-game-factory/services/story_planner/tasks.py` |

## Required Shape

The root contains `schema_version`, `work`, and `chapters`. `work` contains
`status`, `has_sensitive_content`, `severity`, `categories`, `display_policy`,
`localized`, and `detector`.

Each category contains `code`, `presence`, `severity`, `confidence`, and
`evidence_chapters`. Each localized item contains the same machine `code` plus
player-facing `label` and `description`.

## Checkpoint Identity

Bind reuse to the input digest, detector version, ruleset digest, output
digest, schema validation, and detector revision. Do not reuse a warning result
after source text or detection rules change.
