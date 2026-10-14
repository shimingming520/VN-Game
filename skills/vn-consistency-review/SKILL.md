---
name: vn-consistency-review
description: Use when reviewing visual-novel story trees, chapters, branches, endings, character states, effects, or generated assets for factual, structural, causal, and player-state consistency.
---

# VN Consistency Review

Review generated content against authoritative source facts and the validated story topology. Treat review as a release gate.

## Review Order

1. Check structural validity: IDs, links, root reachability, cycles, terminal nodes, and schema shape.
2. Check source fidelity: chapter order, canon facts, character identities, world rules, and irreversible events.
3. Check state transitions: alive/dead, injuries, locations, timeline, variables, effects, and thresholds.
4. Check causal quality: choices cause visible consequences and endings follow from route history.
5. Check presentation: non-empty content, no placeholders, stable speaker names, resolvable assets, and parseable output.
6. For each failure, produce a precise finding with location, violated rule, evidence, and repair action.
7. Re-run the affected checks after repair. Preserve the project's distinction
   between advisory warnings and blocking publication errors.

## Verdicts

- `pass`: no blocking finding;
- `warning`: advisory finding that does not fail the aggregate release policy;
- `repair_required`: deterministic or localized fix is possible;
- `regenerate_required`: source, topology, or model output must be regenerated;
- `blocked`: required evidence or user decision is missing.

Preserve the finding's real severity in the review record. The current
artifact quality evaluator routes many artifact-level findings, including
structural/content diagnostics, through its advisory warning channel and
applies an aggregate score threshold; report that policy instead of claiming
every warning blocks publication. Persistence failures and explicit release
gate failures remain hard failures outside that diagnostic policy.

## Project Alignment

Relevant implementation is in `chapter_branch_quality.py`, `effects_validator.py`, `chapter_branch_artifacts.py`, `chapter_branch_checkpoint.py`, and `chapter_branch_resume.py`.

Read [references/review-checklist.md](references/review-checklist.md) for the evidence-oriented review format.
