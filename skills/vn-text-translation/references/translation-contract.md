# Text Translation Contract

## Implementation Map

| Concern | Implementation |
| --- | --- |
| Source-language detection and routing | `novel-game-factory/services/story_planner/tasks.py` |
| CPU batch translation | `novel-game-factory/services/story_planner/batch_translate.py` |
| Translation gateway | `creation-engine/gateway/translate.py` |
| Client translation entry | `creation-engine-frontend/src/api/modules/translate.js` |
| Source sidecar and provenance | `novel-game-factory/services/story_planner/tasks.py` |

## Decision Rule

Use language prefixes:

```text
source language == target language prefix -> skip prose translation
source language != target language prefix -> translate prose
```

The supported model map is provider/runtime-specific. If a language pair is
unsupported, record the failure and preserve the source rather than selecting
an unrelated model silently.

## Translation Status

At minimum distinguish `not_required`, `completed`, `failed_fallback`, and
`blocked`. A successful model call is not enough: verify that the resulting
text can be read and that source and target provenance remain available.
