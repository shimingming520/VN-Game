---
name: vn-story-analysis
description: Use when analyzing a novel or source manuscript for visual-novel adaptation, including characters, world rules, chapter facts, timelines, secrets, and player-relevant state.
---

# VN Story Analysis

Use this skill to turn source fiction into structured facts that downstream visual-novel planning can safely consume.

## Workflow

1. Identify the source text, chapter boundaries, language, genre, and adaptation scope.
2. Run input preflight before expensive analysis: encoding, character count, chapter count, and malformed input checks.
3. Extract stable facts:
   - characters, identities, relationships, goals, secrets, abilities, injuries, and end states;
   - world rules, locations, factions, items, chronology, and irreversible events;
   - chapter summaries, scene beats, important dialogue facts, and unresolved threads.
4. Separate confirmed source facts from interpretation and adaptation proposals.
5. Produce a compact context package for structure, branch, chapter, and ending work.
6. Preserve source order and cite the source chapter or scene for every high-impact fact.

## Required Output

Return a context package that can be mapped to the project's `NovelContext`.
The runtime-facing top-level fields are:

- `worldview`
- `chapter_summaries`
- `style_samples`
- `char_cards`
- `relationship_map`
- `world_rules`
- `narrative_arc`
- `persona_axes`
- `is_romance_oriented`
- `factions_text`
- `hidden_threads_text`
- `ending_dimensions`
- `character_secrets_text`
- `character_behavior_anchors`
- `branch_type`
- `narrative_pov_contract`

Within `worldview`, preserve structured `characters`, `world_rules`,
`factions`, `key_locations`, `key_events`, and `hidden_threads` when those
facts are available. Use `chapter_summaries` for chapter-local handoff.
`character_end_states` are node-level terminal-state data and should be
stored with the generated node/checkpoint rather than assumed to be a
top-level `NovelContext` field.

Do not invent missing canon facts. Mark uncertainty explicitly and ask for a decision only when it affects topology, character survival, branch compatibility, or an ending.

## Project Alignment

The existing implementation is primarily in `novel_reader.py`, `novel_preflight.py`, `hierarchical_processing.py`, `world_analyst.py`, and `chapter_context.py` under `novel-game-factory/services/story_planner/core/`.

Read [references/source-analysis-contract.md](references/source-analysis-contract.md) when defining fields or handing results to another VN skill.
