---
name: vn-chapter-writing
description: Use when writing visual-novel chapters, scenes, dialogue, narration, choices, stage directions, or asset metadata from a validated story node.
---

# VN Chapter Writing

Write playable chapter content from an approved node and context package. The node plan is authoritative; prose must not quietly change topology or established facts.

## Workflow

1. Load the node's parent state, target state, source evidence, character constraints, branch effects, and required convergence behavior.
2. Build a scene outline with location, time, cast, dramatic objective, conflict, choice or reveal, and exit state.
3. Write scenes in visual-novel form:
   - concise stage direction;
   - readable narration;
   - speaker-labelled dialogue;
   - explicit choice blocks and effects;
   - asset metadata only when required by the downstream builder.
4. Preserve character voice, world rules, timeline, and irreversible end states.
5. Ensure the chapter has a concrete dramatic movement and a valid exit to the planned child nodes.
6. Run factual and structural review before persistence.
7. Reject empty, placeholder, or materially under-length output; repair or fail explicitly.

## Output Contract

For a generated node, preserve the runtime `StoryNode` shape and populate
the fields that downstream publication checks consume:

- `id` or the topology node's mapped `real_id`;
- `parent_ids` for DAG ancestry, with legacy `parent_id` only when required
  by compatibility code;
- `title`;
- `content_file`;
- `children`, each containing `choice_text` and `child_id`;
- `summary`;
- `plan_direction` when a planning direction was supplied;
- `terminal_states`;
- `is_original`;
- `generated_at`.

The source evidence, scene outline, choice intent, and quality findings are
supporting generation metadata. They are not substitutes for `content_file`,
`children`, `summary`, or `terminal_states`.

Do not generate a new branch, ending, or character death merely because the prose needs a stronger climax. Send topology changes back to the structure or branch skill.

## Project Alignment

Relevant implementation is in `story_generator.py`, `hook_generator.py`, `chapter_context.py`, `visual_style_context.py`, and the branch runtime modules.

Read [references/chapter-output-contract.md](references/chapter-output-contract.md) before producing a handoff for `game_builder`.
