# Localization Contract

## Current Story-Tree Locale Bundle

The current `story_planner` localization path localizes player-visible story
tree fields. It does not rewrite chapter Markdown or generate voice scripts.

```json
{
  "locale": "en-US",
  "source_locale": "zh-CN",
  "player_language": "en-US",
  "book_title": "A Sample Story",
  "book_summary": "A faithful player-facing summary.",
  "nodes": {
    "ch2": {
      "id": "ch2",
      "title": "The Choice",
      "summary": "The protagonist faces a difficult decision.",
      "children": [
        {
          "choice_text": "Tell the truth",
          "child_id": "ch3"
        }
      ]
    }
  },
  "content_warnings": {
    "schema_version": 1,
    "work": {
      "status": "detected",
      "has_sensitive_content": true,
      "severity": "moderate",
      "categories": [
        {
          "code": "violence",
          "presence": "depicted",
          "severity": "moderate",
          "confidence": 0.78,
          "evidence_chapters": [1]
        }
      ],
      "display_policy": {
        "show_at_work_entry": true,
        "requires_acknowledgement": true,
        "dismissible": true,
        "allow_continue": true,
        "allow_return": true
      },
      "localized": {
        "title": "Content warning",
        "summary": "This work contains violence or physical harm.",
        "items": [
          {
            "code": "violence",
            "label": "Violence",
            "description": "The work contains violence or physical harm."
          }
        ],
        "acknowledge_text": "I understand",
        "return_text": "Return"
      },
      "detector": {
        "version": "work-content-warning-v1",
        "method": "rules_and_reviewed_classifier",
        "review_required": false,
        "revision": "example-revision"
      }
    },
    "chapters": {}
  }
}
```

For a multilingual `story_tree.json`, store complete locale snapshots keyed by
BCP-47-like tags such as `en-US` and `ja-JP`, with the publication record kept
at the bundle root. Preserve node IDs, `id`, `depth`, `parent_id`,
`is_original`, child target IDs, branch variables, effect objects, and asset
IDs across locales. Titles, summaries, choice labels, book metadata, and
warning labels may be localized.

## Review Checklist

- Names, aliases, titles, and pronouns match the glossary.
- A choice still expresses the same action and consequence.
- Internal variables, routing metadata, and parser markers are unchanged.
- Every locale has the same node ID set and locale-invariant routing structure.
- `book_title`, `book_summary`, node titles, node summaries, and choice texts
  remain non-empty and player-facing.
- Content-warning machine category codes, presence values, severity values, and
  chapter evidence remain identical across locales.
- Localized warning title, summary, item labels, item descriptions, and
  acknowledgement text may be translated.
- Preserve `schema_version`, `work.status`, `work.has_sensitive_content`,
  `work.display_policy`, `work.detector`, and the `chapters` object shape.
- Unresolved terms are listed rather than silently normalized.

For full chapter or subtitle localization, use the same invariants but add
stable segment IDs, speaker IDs, exact source/target text, and timing review
in the caller-provided content pipeline.
