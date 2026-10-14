# Story Tree Contract

## Runtime Topology Shape

```json
{
  "topology": [
    {
      "node_id": "ch1",
      "type": "chapter",
      "title": "Chapter 1",
      "is_original": true,
      "source_chapter_idx": 0,
      "choice_text": "Continue along the original route",
      "summary": null,
      "branch_variable": null,
      "branch_threshold": null,
      "next": ["ch2"]
    },
    {
      "node_id": "ch2",
      "type": "ending",
      "title": "Original Ending",
      "is_original": true,
      "source_chapter_idx": 1,
      "choice_text": "",
      "summary": null,
      "branch_variable": null,
      "branch_threshold": null,
      "next": []
    }
  ],
  "variable_defs": []
}
```

The planner currently uses `type=chapter` for ordinary chapters,
`type=ending` for terminal endings, and `type=bad_ending` for terminal bad
ends. What-If chapters are normally `type=chapter` with
`is_original=false`; they are distinguished by their topology position and
metadata, not by a required `type=what_if` value.

The runtime later materializes these topology nodes as `StoryNode` records.
That second representation uses `id`, `parent_ids`, `content_file`,
`children`, `terminal_states`, and related fields; do not mix it into the
LLM topology result.

## Graph Invariants

Before generating prose, verify:

1. `node_id` values are unique.
2. Every referenced child exists.
3. One and only one root is present.
4. All nodes are reachable from the root.
5. The graph is acyclic unless the runtime explicitly supports a loop.
6. `ending` and `bad_ending` nodes have `next=[]`.
7. Original source chapters appear once and in source order.
8. Every branch variable has a definition and bounded values.
9. Every ending route has a reachable trigger or explicit manual unlock.
10. For immediate or bridge-style What-If nodes, the alternate node and its
    original sibling use the same downstream `next` list to form a diamond
    DAG. Delayed merges may target a later shared node and therefore do not
    require identical `next` lists.

## Failure Behavior

Do not silently repair a failed graph by dropping nodes. Report the violated invariant, identify the affected nodes, and either regenerate the topology or request a targeted correction.

The chapter-branch optimization plan in the project documents these
invariants and should be consulted when the requested work touches
persistence, recovery, or publishing.
