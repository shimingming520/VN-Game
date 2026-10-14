# Source Preprocessing Contract

## Implementation Map

| Concern | Implementation |
| --- | --- |
| Front-matter boundary processing | `novel-game-factory/services/story_planner/tasks.py` |
| Source reader and chapter splitting | `novel-game-factory/services/story_planner/core/novel_reader.py` |
| Encoding normalization | `novel-game-factory/services/story_planner/core/novel_text_encoding.py` |
| Chapter checkpoint construction | `novel-game-factory/services/story_planner/core/chapter_branch_checkpoint.py` |
| Persistent source sidecar | `novel-game-factory/services/story_planner/tasks.py` |

## Required Provenance

Track the original source fingerprint, working-text fingerprint, source
language, target language, chapter count, per-chapter hashes, cleaned-source
fingerprint, artifact path, and artifact hash when an artifact is external to
the checkpoint.

## Safe Boundary

The front-matter cleaner may inspect a bounded prefix and return a verbatim
boundary anchor. The source is then sliced locally. A model-generated rewrite
of the full source is not a valid preprocessing artifact.
