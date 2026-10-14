---
name: vn-story-structure
description: Use when planning the main visual-novel route, chapter topology, node dependencies, player variables, or a story tree from analyzed source material.
---

# VN Story Structure

Design a playable story topology before writing chapter prose.

## Workflow

1. Load the source-analysis context and adaptation constraints.
2. Select the requested `branch_type` and establish the root, original chapter nodes, branch nodes, and terminal nodes.
3. Define stable node IDs, chapter order, parent-child relationships, and player variables.
4. Validate the graph before any prose generation:
   - unique node IDs;
   - every `next` target exists;
   - exactly one root;
   - every node is reachable;
   - no cycles;
   - original chapters are complete and non-duplicated;
   - ending nodes are terminal;
   - branch variables exist in `variable_defs`.
5. Record a short purpose and source evidence for each non-trivial node.
6. Hand off a versioned topology plan to branch, chapter, ending, and runtime work.

## Output Contract

For the existing `story_planner` runtime, return a topology result with:

- `topology`: a list of topology nodes;
- `variable_defs`: the declared numeric axes, or `[]` for modes that do not use them;
- `branch_type`: one of the supported planner modes;
- `topology_generation` and validation metadata when supplied by the caller.

Each topology node must use the project's fields: `node_id`, `type`, `title`,
`is_original`, `source_chapter_idx`, `choice_text`, `summary`,
`branch_variable`, `branch_threshold`, and `next`. Additional runtime metadata
may include `merge_policy`, `merge_target_node_id`, `terminal_branch`, and
`convergence_status`.

`root_id`, `nodes`, `parent_id`, `source_chapter`, and `terminal` are not
substitutes for the fields above when producing data for this project.

If topology validation fails, return a diagnostic result and stop. Do not continue to chapter writing with a guessed or partially valid graph.

## Project Alignment

The main implementation is in `branch_designer.py`, `chapter_branch_runtime.py`, `chapter_branch_checkpoint.py`, and `chapter_branch_quality.py` under `novel-game-factory/services/story_planner/core/`.

Read [references/story-tree-contract.md](references/story-tree-contract.md) for the planning contract and validation checklist.
