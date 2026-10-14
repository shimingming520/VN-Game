---
name: vn-pipeline-orchestration
description: Use when coordinating an end-to-end visual-novel generation workflow across source analysis, story topology, branches, chapter writing, endings, consistency review, and persistence.
---

# VN Pipeline Orchestration

Use the smallest set of VN skills needed for the request, while preserving the dependency order and release gates.

## Routing

| Request | Skill |
| --- | --- |
| Accept upload and create or recover a production task | `vn-task-intake` |
| Validate uploaded source and workload | `vn-input-preflight` |
| Clean front matter and prepare chapters | `vn-source-preprocessing` |
| Anonymize names and normalize aliases | `vn-name-anonymization` |
| Detect sensitive content and warnings | `vn-content-warnings` |
| Detect source language or translate prose | `vn-text-translation` |
| Understand source material | `vn-story-analysis` |
| Plan main route or graph | `vn-story-structure` |
| Create choices or alternate routes | `vn-branch-design` |
| Write scenes or chapters | `vn-chapter-writing` |
| Close a route or write a bad end | `vn-ending-design` |
| Audit generated output | `vn-consistency-review` |
| Translate source-novel prose | `vn-text-translation` |
| Localize story-tree, UI, subtitles, or warnings | `vn-localization` |
| Generate visual assets | `vn-image-generation` |
| Generate audio assets | `vn-audio-generation` |
| Map reviewed assets into runtime manifests | `vn-asset-integration` |
| Build a game package or verify browser preview | `vn-build-preview` |
| Persist state or resume interrupted work | `vn-persistence-recovery` |
| Run the full workflow | This skill plus all required stages |

## Full Workflow

1. Accept the upload and create or recover the production task.
2. Validate the uploaded source and estimate workload.
3. Preserve canonical source text, clean confirmed front matter, and extract
   chapters.
4. Apply approved name anonymization and persist the mapping.
5. Detect and normalize content warnings from canonical source evidence.
6. Detect source language, decide whether prose translation is required, and
   record translation provenance or fallback.
7. Analyze the prepared source using the completed preflight report.
8. Obtain approval for the story context and adaptation constraints when the surrounding product workflow requires it.
9. Build and validate topology.
10. Design branches and variables.
11. Generate chapters and endings node by node.
12. Localize approved player-visible text when additional locales are requested.
13. Generate and review approved media through the project's media services:
   character images through `char_designer`, scene/background/CG images through
   `scene_painter`, voice through `audio_producer`, and BGM/SFX/ambience through
   `sound_arranger`.
14. When the target builder exposes a compatible integration contract, map
   reviewed text and media into deterministic scene manifests. Do not assume
   that a single unified cross-media manifest already exists.
15. Review each artifact and repair or regenerate blocking failures.
16. Validate publication completeness: every node has the required content,
    links, IDs, state data, and required asset references.
17. Build the game package and verify browser-preview delivery when requested.
18. Persist checkpoints and stage state throughout the workflow; after the
    publication gate, persist the final release record and build identifiers.
19. Record metrics, errors, ownership, and generated artifact identifiers so
    interrupted work can resume.

## Execution Boundaries

- A failed topology gate blocks prose generation.
- A failed artifact or persistence check blocks completion.
- Completed nodes may be reused only after their stored content and checkpoint hashes agree.
- Targeted regeneration should invalidate affected downstream non-original nodes while preserving unrelated completed work.
- Asset regeneration should invalidate only manifests and dependent scene
  outputs that reference the changed asset.
- Do not alter project source code, environment configuration, database schema, or prompts merely because a Skill was loaded. This Skill documents and routes the existing system.

## Project Map

The current execution core is under `novel-game-factory/services/story_planner/`. The main task entrypoint is `tasks.py`; branch execution and recovery are in the `chapter_branch_*` modules; generation and domain prompts are in `story_generator.py`, `branch_designer.py`, `soft_branch.py`, `ending_writer.py`, `novel_reader.py`, and `world_analyst.py`.

Read [references/pipeline-contract.md](references/pipeline-contract.md) for stage handoffs and [references/project-code-map.md](references/project-code-map.md) when navigating implementation. This Skill routes the workflow and does not execute those services automatically.
