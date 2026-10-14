---
name: vn-branch-design
description: Use when designing What-If routes, soft branches, player choices, effects, relationship variables, or branch-compatible alternate outcomes for a visual novel.
---

# VN Branch Design

Create meaningful choices that preserve established facts while changing player-facing consequences.

## Workflow

1. Load the validated story topology, source facts, current node end state, and downstream convergence point.
2. Choose a branch point with an active decision, a visible consequence, and a reason the player could plausibly choose either option.
3. For What-If branches, define a changed premise and a complete route from compatible start state to compatible terminal state.
4. For soft branches, keep the same main facts and vary optional scene content, relationship movement, tone, information, or foreshadowing.
5. Define effects using only declared variables. Clamp numeric values to each variable's cap.
6. Check A/B options for meaningful contrast:
   - effects are not identical;
   - effects are not all zero, except for an explicitly allowed modular
     no-variable choice or a documented flavor marker;
   - the dominant variable and direction are clear;
   - romance choices name the same character affected by the relationship variable.
7. Check threshold reachability on normal player paths.
8. Validate the branch against parent and convergence-node end states before handing it to chapter writing.

## Guardrails

Do not use choices that only rephrase the same action, rewrite canon without an explicit What-If premise, or create an ending that cannot be reached. A branch is not complete until its start state, player-facing difference, effects, and convergence or terminal behavior are explicit.

## Project Alignment

Relevant implementation is in `branch_designer.py`, `soft_branch.py`, `effects_validator.py`, `ending_writer.py`, and `chapter_branch_quality.py`.

Read [references/branch-quality-contract.md](references/branch-quality-contract.md) for effect and compatibility checks.
