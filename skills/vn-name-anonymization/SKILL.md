---
name: vn-name-anonymization
description: Use when anonymizing personal names, normalizing aliases, or mapping source names to stable visual-novel speaker identities.
---

# VN Name Anonymization

Protect personal identity data while keeping character references stable across
the novel, prompts, subtitles, story tree, voice selection, and asset names.
Treat anonymization as a versioned mapping contract, not ad hoc text replacement.

## Workflow

1. Load the source-name, changed-name, localized-name, and canonical speaker
   mapping sections.
2. Detect complete names, compound surnames, transliterated family segments,
   nicknames, and surname-plus-role forms.
3. Preserve generic appellations such as system, narrator, driver, teacher, or
   parent when they do not contain a personal name.
4. Apply one deterministic mapping throughout all player-visible and
   asset-facing text.
5. Persist the mapping and replacement report for downstream analysis and
   regeneration.
6. Route ambiguous names, aliases, or role terms to review instead of
   inventing a replacement.

## Hard Constraints

- Do not rename machine fields, node IDs, speaker IDs, or asset IDs.
- Do not turn a generic role into a fabricated personal name.
- Keep source, anonymized, and localized aliases distinguishable.
- A name mapping change invalidates dependent text, voice, and asset outputs.

Read [references/name-mapping-contract.md](references/name-mapping-contract.md)
for the project mapping rules.
