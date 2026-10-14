# Chapter Output Contract

## Scene Checklist

Every scene should make its role clear:

- where and when it occurs;
- who is present;
- what changes during the scene;
- what information or emotion the player receives;
- how the scene reaches the next planned node or choice.

## Runtime Handoff

The writer produces chapter prose and metadata; the runtime persists it as a
`StoryNode`. A valid handoff must be mappable to:

```json
{
  "id": "node_real_id",
  "parent_ids": ["parent_real_id"],
  "content_file": "node_real_id.md",
  "children": [
    {
      "choice_text": "Continue",
      "child_id": "child_real_id"
    }
  ],
  "summary": "A short factual node summary",
  "terminal_states": {},
  "is_original": true
}
```

This is a runtime-oriented example. Keep the actual generated prose in the
Markdown artifact referenced by `content_file`.

## Choice Checklist

Each player choice must have:

- a meaningful difference in intent;
- player-visible wording;
- declared effects or a documented non-state consequence;
- a valid destination or continuation;
- compatibility with the node's allowed state transitions.

## Quality Gates

Before publishing:

1. prose is non-empty and not placeholder text;
2. required characters and locations are present;
3. facts and end states match the context package;
4. all referenced assets and IDs are resolvable or explicitly marked pending;
5. JSON and Markdown representations agree;
6. downstream game-builder input remains parseable.

Do not claim a chapter is complete when it only contains a summary or outline unless the caller explicitly requested an outline.
