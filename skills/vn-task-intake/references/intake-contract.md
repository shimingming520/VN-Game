# Task Intake Contract

## Implementation Map

| Concern | Implementation |
| --- | --- |
| Upload and task creation | `creation-engine/gateway/creation.py` |
| Client upload and confirmation calls | `creation-engine-frontend/src/api/modules/creation.js` |
| Raw source storage | `storage/raw_novels` through the gateway/configured storage |
| Task status and resume entry | `/api/v1/tasks/{task_id}/status`, `/api/v1/tasks/{task_id}/entry` |

## State Boundary

The user-facing flow separates:

```text
upload -> summary/configuration -> confirmation -> generation -> material/performance -> build/publication
```

`uploadStoryApi` stores a single source without starting full generation;
batch submission may start the pipeline directly under its own idempotency
contract. Keep those behaviors distinct when routing a task.

## Intake Output

Return or persist the task ID, raw filename, project name when available,
selected output language, story mode, visual/audio configuration, ownership
metadata, and current stage/status. A later preflight must still validate the
actual file contents.
