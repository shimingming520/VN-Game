# Input Preflight Contract

## Implementation Map

| Concern | Implementation |
| --- | --- |
| Workload and size validation | `novel-game-factory/services/story_planner/core/novel_preflight.py` |
| Strict encoding detection | `novel-game-factory/services/story_planner/core/novel_text_encoding.py` |
| Source reading and chapter extraction | `novel-game-factory/services/story_planner/core/novel_reader.py` |
| Main task orchestration | `novel-game-factory/services/story_planner/tasks.py` |

## Blocking Conditions

- Empty or whitespace-only text.
- No usable chapters.
- Novel or chapter count exceeds configured limits.
- Too many Unicode replacement characters.
- Unsupported encoding, invalid strict decoding, suspicious control
  characters, or binary-like null bytes.

## Report Invariants

Keep `story_mode`, character count, UTF-8 byte count, chapter count, chapter
length statistics, segmentation estimates, branch estimates, LLM call
estimates, runtime estimates, and warnings together. A report may contain
warnings, but a raised preflight error blocks downstream stages.
