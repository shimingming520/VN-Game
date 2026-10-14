# Review Checklist

## Finding Shape

```json
{
  "severity": "blocking",
  "category": "state|topology|canon|causality|output",
  "location": "node_id or field path",
  "finding": "What is wrong",
  "evidence": ["source chapter or invariant"],
  "repair": "Specific correction or regeneration action"
}
```

## Blocking Findings

- missing or duplicate node IDs;
- missing child references or unreachable nodes;
- cycle where the runtime does not support one;
- invalid JSON/Markdown handoff;
- undeclared or impossible player effects;
- contradictory character end state;
- ending that cannot be reached;
- empty or placeholder chapter content;
- canon violation affecting identity, survival, chronology, or world rules.

`severity=blocking` describes the finding itself. The current artifact quality
evaluator may still place artifact findings in the warning channel and apply
the configured aggregate score gate (`blocking_policy=min_score_only`).
Infrastructure, persistence, and explicit release-gate failures remain hard
failures outside that score-only diagnostic policy.

## Recovery

Use deterministic repair only when the intended value is unambiguous. Otherwise route the issue back to story analysis, topology, branch design, chapter writing, or ending design. Preserve the original finding and the post-repair result for diagnostics.
