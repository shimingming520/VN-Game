# Persistence And Recovery Contract

## Implementation Map

| Concern | Implementation |
| --- | --- |
| Chapter-plan checkpoint storage | `novel-game-factory/services/story_planner/core/chapter_branch_checkpoint.py` |
| Resume validation and dependency identity | `novel-game-factory/services/story_planner/core/chapter_branch_resume.py` |
| Stage lease and progress fencing | `novel-game-factory/common/progress_utils.py` |
| Celery/Redis execution liveness and task ownership | `novel-game-factory/common/celery_app.py`, `novel-game-factory/common/progress_utils.py` |
| PostgreSQL terminal-write fencing | `novel-game-factory/common/stage_fencing.py` |
| Visual per-asset checkpoints | `novel-game-factory/common/visual_checkpoint.py` |
| Builder stage status | `novel-game-factory/services/game_builder/tasks.py` |

## Reuse Predicate

An artifact is reusable only when its persisted status is successful and its
schema version, producer version, input digest, dependency digest, output
contract, file existence, and output hash all match the current request.

## Ownership

Redis lease loss, stale Celery execution IDs, foreign owners, PostgreSQL fence
rejection, and rejected takeovers are blocking write conditions. The old worker
must stop publishing after another execution owns the stage. Redis liveness
alone is not enough to authorize a terminal database write; the current
PostgreSQL owner/token must also validate.
