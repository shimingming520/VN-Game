---
name: vn-content-warnings
description: Use when detecting, normalizing, localizing, checkpointing, or validating sensitive-content warnings for a visual-novel work.
---

# VN Content Warnings

Detect sensitive themes independently from name anonymization and preserve a
machine-stable warning contract across locales. Warnings inform player entry
and review policy; they do not replace platform policy review.

## Workflow

1. Scan canonical source text and chapter evidence, not only a translated
   working copy.
2. Combine deterministic signals with reviewed classifier output when used.
3. Normalize category code, presence, severity, confidence, and evidence
   chapters.
4. Build work status, display policy, localized warning text, detector
   metadata, and chapter metadata.
5. Validate the schema and persist a digest-bound checkpoint.
6. Localize player-facing warning text while keeping machine fields unchanged.

## Hard Constraints

- Never confuse "not detected" with "review required".
- Keep category codes, presence, severity, evidence chapters, and policy
  booleans structurally stable across locale bundles.
- A malformed warning result must degrade to explicit review, not a clean pass.
- Do not let changing UI language bypass an acknowledgement gate.

Read [references/content-warning-contract.md](references/content-warning-contract.md)
for the current schema and implementation map.
