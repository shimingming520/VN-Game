# Branch Quality Contract

## Runtime Choice Shapes

There are two project-level choice representations. Use the first for story
tree topology and the second for injected soft-branch prose.

```json
{
  "choice_text": "Choose to tell the truth",
  "child_id": "ch2_alt",
  "branch_variable": null,
  "branch_threshold": null
}
```

For a soft branch, the persisted Markdown marker is represented by:

```text
choice_a=Tell the truth
content_a=...
effects_a={'honesty': 3}
choice_b=Keep silent
content_b=...
effects_b={'honesty': -3}
```

The exact marker also carries `branch_kind`, legacy flags, clue flags, and
validation metadata. Do not replace `choice_a`/`effects_a` and
`choice_b`/`effects_b` with a generic `options` array when producing content
for `soft_branch.py`.

## Effect Checks

- Reject undeclared variable keys.
- Convert numeric values to integers.
- Clamp values to the variable's per-variable effect cap. Effect deltas are
  bounded separately from runtime variable values, which are clamped to
  `0..100`.
- Reject identical A/B effects.
- Reject choices where all effects are zero, except when the runtime explicitly
  allows an empty-effect modular branch with no variable definitions or marks
  the choice as flavor-only.
- For romance mode, verify the named character and `affection_*` variable
  agree. Romance may use two different affection axes or a positive/negative
  decision on one route; do not apply the generic single-axis rule blindly.
- Verify the requested threshold can be reached through at least one valid route.

## What-If Checks

- The branch start state is compatible with the parent node.
- The branch does not copy a sibling or original chapter scene-by-scene.
- Its terminal state is compatible with the intended convergence chapter.
- Core character survival, event direction, and emotional tone have an intentional relationship to the source.

When a check fails, produce a targeted repair request instead of hiding the failure in prose.
