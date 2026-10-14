# Ending Causality Contract

## Runtime Ending Route

For topology and runtime routing, use the project's actual fields:

```json
{
  "node_id": "ending_aya_true",
  "type": "ending",
  "is_original": false,
  "branch_variable": "affection_aya",
  "branch_threshold": 65,
  "branch_ratio_threshold": 0.7,
  "branch_margin_ratio": 0.1,
  "required_legacy_flags": [],
  "next": []
}
```

`branch_ratio_threshold`, `branch_margin_ratio`, and
`effective_choice_count` are optional runtime-enriched route fields.
`required_legacy_flags` may be derived from
`legacy_requirements`. A generic trigger object can be used in design notes,
but it is not a replacement for these topology fields.

The generated ending node separately stores `terminal_states`, summary,
content file, legacy evidence, and semantic review results.

## Review Questions

- Can the ending be reached from the root?
- Are all trigger variables declared and reachable?
- Does the outcome follow from prior choices rather than an unexplained jump?
- Are core character end states complete and internally consistent?
- Is death irreversible unless the world rules explicitly allow reversal?
- Are location, injury, identity, and timeline fields legal?
- Does the ending close the intended threads?
- Is it materially distinguishable from the source and sibling endings?

Failures should lead to targeted revision and a second review, not a warning that is ignored during publishing.
