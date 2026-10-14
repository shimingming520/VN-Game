---
name: vn-persistence-recovery
description: Use when persisting visual-novel stage state, resuming interrupted generation, validating checkpoints, or handling retries and stage ownership.
---

# VN Persistence And Recovery

Make long-running generation resumable without turning stale or incomplete
work into success. Checkpoints are integrity contracts with ownership,
dependency, version, and content identity. In this project, Redis/Celery
liveness and PostgreSQL stage fencing jointly control who may publish a stage
result.

## Workflow

1. Record stage status, run identity, attempts, errors, outputs, hashes, and
   dependency information.
2. Persist updates transactionally and verify stage ownership before writing.
3. Reuse only completed artifacts whose schema, producer version, input
   fingerprint, dependency fingerprint, output hash, and files still match.
4. On resume, invalidate affected downstream artifacts when an upstream input
   changes.
5. Distinguish retry, takeover, stale execution, failed, omitted, and
   intentionally skipped stages.
6. Preserve diagnostics and history when a retry or recovery fails.

## Hard Constraints

- Never mark an interrupted node completed merely because a file exists.
- Never allow an old execution to overwrite a newer checkpoint.
- Treat Redis lease loss, Celery execution takeover, and PostgreSQL fence
  rejection as ownership failures; the old worker must stop publishing.
- Do not reuse a checkpoint after schema, producer, prompt, source, or
  dependency identity changes.
- A skipped stage must be recorded as skipped, not completed.

Read [references/recovery-contract.md](references/recovery-contract.md) for
the current checkpoint and stage-lease map.
