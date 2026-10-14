---
name: vn-ending-design
description: Use when designing or writing visual-novel endings, parallel conclusions, bad ends, route closures, terminal states, or ending-specific causality.
---

# VN Ending Design

Design endings as earned terminal states, not disconnected scenes.

## Workflow

1. Load the validated topology, route conditions, source ending facts, character end states, and emotional target.
2. Define the ending's trigger, route history, terminal node, and required unresolved-thread closures.
3. Specify the causal chain from player decisions and prior state to the ending outcome.
4. Distinguish the ending from the source or sibling endings through concrete changes in at least two core dimensions, such as:
   - key character survival or status;
   - event direction;
   - relationship resolution;
   - information revealed;
   - emotional or thematic resolution.
5. Write the final chapter only after its terminal state is explicit.
6. Validate character roster, alive/dead status, location, injuries, identity, timeline, and irreversible facts.
7. Run consistency review and repair concrete failures before persistence.

## Ending Types

- `ending`: intended successful or canonical route closure;
- `bad_ending`: a terminal failure or loss state with a clear causal trigger;
- parallel or What-If ending: an alternate terminal interpretation that remains compatible with its branch premise.

Do not use a generic "the story continues" or equivalent placeholder as an ending. If the causal chain cannot be justified, return an incomplete-ending diagnostic.

## Project Alignment

Relevant implementation is in `ending_writer.py`, `branch_designer.py`, `chapter_branch_quality.py`, and `novel_reader.py`.

Read [references/ending-causality-contract.md](references/ending-causality-contract.md) for terminal-state checks.
