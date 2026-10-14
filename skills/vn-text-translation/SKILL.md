---
name: vn-text-translation
description: Use when detecting source language, deciding whether novel prose needs translation, translating chapters, or recording translation fallback and provenance.
---

# VN Text Translation

Translate narrative source text only when source and requested output
languages differ. Keep this stage distinct from story-tree localization:
translation changes the working prose language, while localization changes
player-visible metadata and UI text.

## Workflow

1. Detect the source language and normalize the requested target language.
2. Compare language prefixes and skip translation when they are equivalent.
3. Preserve canonical source text and write translated working text separately
   or record the controlled overwrite and its source sidecar.
4. Translate with bounded batches while preserving chapter order, paragraph
   boundaries, and narrative facts.
5. Record source language, target language, model/provider, status, and error
   details.
6. On failure, preserve the original text and emit an explicit warning. Treat
   fallback as blocking when the target language is a hard publication
   requirement.

## Hard Constraints

- Do not translate IDs, variables, effects, asset IDs, or routing metadata.
- Do not silently report success when the output remains source-language text.
- Do not use translated text as the only input for source-based content
  warning detection.
- Keep translation and `vn-localization` as separate contracts.

Read [references/translation-contract.md](references/translation-contract.md)
for language routing and provider details.
