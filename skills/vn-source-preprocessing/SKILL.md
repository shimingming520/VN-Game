---
name: vn-source-preprocessing
description: Use when cleaning uploaded novel front matter, extracting chapters, preserving canonical source text, or preparing resumable preprocessing artifacts.
---

# VN Source Preprocessing

Prepare a reusable story source without rewriting narrative content. Remove
only confirmed opening metadata, preserve chapter order and body text, and
record enough provenance to reproduce or resume the result.

## Workflow

1. Preserve the canonical uploaded source before translation or mutation.
2. Identify title, author, tags, URLs, repost notices, translation notices,
   and other metadata only at the safe opening boundary.
3. Remove confirmed metadata by local boundary slicing; do not ask a model to
   rewrite the full novel.
4. Extract chapters while preserving body order, paragraph boundaries, and
   source evidence.
5. Write normalized UTF-8 text and a preprocessing checkpoint containing
   source and chapter hashes.
6. Reuse the checkpoint only when schema, producer version, source fingerprint,
   chapter count, and every chapter hash match.

## Hard Constraints

- Never replace canonical source text with translated working text.
- Never remove narrative text merely because it resembles a heading.
- Preserve source and working-text provenance separately.
- A stale or corrupt preprocessing checkpoint must trigger regeneration.

Read [references/source-contract.md](references/source-contract.md) before
changing source-processing behavior.
