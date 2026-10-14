# Source Analysis Contract

## Purpose

This is the handoff contract between source analysis and later visual-novel planning. It is guidance for Codex and does not replace the project's Python models or database schema.

## Fact Classes

| Class | Meaning | Examples |
| --- | --- | --- |
| `canon_fact` | Directly supported by the source | A character is injured in chapter 3 |
| `derived_fact` | Safe conclusion from multiple canon facts | Two events must occur in chronological order |
| `adaptation_option` | Proposed game-design choice | A choice can expose a hidden relationship |
| `unknown` | Not established or contradictory | The exact date of an event |

Never pass an `adaptation_option` downstream as if it were a `canon_fact`.

## Character End State

Track at least:

```json
{
  "character_id": "stable-id",
  "alive": true,
  "status": "injured",
  "location": "location-id",
  "identity": "known identity",
  "timeline": "main",
  "evidence": ["chapter-3-scene-2"]
}
```

Use explicit evidence references. Death, identity reveal, timeline changes, and irreversible injuries must never rely on an unreferenced summary.

## Context Package

The package handed to later stages should contain only the facts needed for the requested stage. Prefer chapter-local facts plus a compact global invariant list over repeatedly sending the full manuscript.

The project has dedicated code for long-input preflight, hierarchical summaries, prompt budgeting, and end-state extraction. Keep this reference aligned with those modules rather than inventing a second runtime format.
