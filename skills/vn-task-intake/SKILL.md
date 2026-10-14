---
name: vn-task-intake
description: Use when accepting a visual-novel source upload, creating a production task, collecting generation parameters, or resuming the user-facing creation workflow.
---

# VN Task Intake

Turn an uploaded novel and explicit creation parameters into an owned,
traceable production task. Intake ends with a stable task ID and validated
configuration; it does not imply that generation has started or succeeded.

## Workflow

1. Accept the source file and required creation parameters through the
   supported upload contract.
2. Store the raw source and create or recover the task record.
3. Return the task ID and summary/configuration state needed by the client.
4. Validate output language, story mode, visual style, model, resolution, and
   optional audio settings before confirmation.
5. Keep upload, configuration, confirmation, generation, and publication as
   distinct task states.
6. On retries, use the task's ownership and idempotency rules; do not create
   duplicate work from an ambiguous request.

## Hard Constraints

- Do not treat a successful upload as a completed generation task.
- Do not start expensive generation before required confirmation or the
  caller's explicit batch-submit contract.
- Preserve the source task ID, raw filename, configuration, and ownership
  metadata.
- Route the stored source into `vn-input-preflight` before analysis.

Read [references/intake-contract.md](references/intake-contract.md) for the
current API and state boundary.
