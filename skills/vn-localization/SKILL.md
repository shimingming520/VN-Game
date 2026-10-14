---
name: vn-localization
description: Use when localizing visual-novel story-tree metadata, choices, UI text, subtitles, content warnings, or asset-facing metadata between languages.
---

# VN Localization

Localize player-visible visual-novel metadata without changing story logic,
route structure, player-state effects, or canonical facts. Use
`vn-text-translation` for source-novel prose translation. Use this Skill for
story-tree metadata, UI-facing text, subtitles, warnings, and locale bundles.
In the current `story_planner` implementation, story-tree localization is
directly supported; full chapter Markdown and voice-script localization remain
caller-provided handoffs and must not be mistaken for the prose translation
stage.

## Workflow

1. Load the approved story tree or caller-provided player-visible text,
   target locale, character glossary, world terms, choice effects, and asset
   references.
2. Build or update a terminology table before translating repeated names,
   titles, locations, items, and system labels.
3. Translate only the player-visible fields in the supplied artifact,
   according to their function and target-language conventions.
4. Preserve IDs, markers, variables, effect values, speaker identity, and
   content boundaries exactly.
5. Check line length, punctuation, honorifics, pronouns, subtitle timing hints,
   and text that is embedded in image or audio metadata.
6. Produce the locale artifact supported by the caller and a
   terminology/change report.

## Hard Constraints

- Do not use this Skill to translate the canonical novel or its working prose;
  route that request to `vn-text-translation`.
- Never translate or rename machine fields such as `node_id`, `child_id`,
  `branch_variable`, `effects_a`, `effects_b`, or asset IDs.
- Never convert an adaptation proposal into a canon fact during translation.
- Preserve the source choice's intent; natural wording may change syntax but
  not the player-facing consequence.
- Flag ambiguity instead of silently choosing between incompatible identities,
  relationships, timelines, or world terms.

Read [references/localization-contract.md](references/localization-contract.md)
for the locale artifact and review contract.
