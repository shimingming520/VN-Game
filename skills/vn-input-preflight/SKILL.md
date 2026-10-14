---
name: vn-input-preflight
description: Use when validating an uploaded visual-novel source before expensive analysis, translation, generation, or asset work.
---

# VN Input Preflight

Validate the source before any expensive LLM, translation, or media stage.
Fail closed on unreadable, empty, oversized, malformed, or suspicious input.
Return a structured report that makes workload, warnings, and blocking errors
observable to the caller.

## Workflow

1. Read the source with strict encoding detection and normalize only when the
   caller authorizes UTF-8 conversion.
2. Validate text presence, control characters, replacement characters, total
   size, chapter count, and chapter segmentation.
3. Detect the source language and record the requested output language.
4. Estimate analysis calls, output nodes, branch workload, and runtime when
   the topology is not known yet.
5. Stop downstream processing on a blocking preflight error.
6. Persist the report or checkpoint with the source fingerprint.

## Hard Constraints

- Never send an unreadable or binary-like file to an LLM.
- Do not silently discard chapters or repair source text during preflight.
- Keep source bytes, detected encoding, normalized text, and fingerprints
  distinguishable.
- A warning is not a pass when the configured limit makes the input unsafe.

Read [references/preflight-contract.md](references/preflight-contract.md)
for the current implementation boundary and report fields.
