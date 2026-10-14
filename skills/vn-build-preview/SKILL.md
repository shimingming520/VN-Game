---
name: vn-build-preview
description: Use when converting reviewed visual-novel artifacts into a game package, materializing local visual sources, or validating browser preview delivery.
---

# VN Build And Preview

Build only after story, locale, content, and required asset gates pass. The
builder converts story data and discovered assets into a browser-deliverable
game package; it does not repair upstream narrative or asset contracts.

## Workflow

1. Confirm the task output directory and required source artifacts exist.
2. Normalize the story-tree locale bundle for the target runtime.
3. Materialize local subtitle sources when the local no-audio visual mode is
   active.
4. Scan and validate scene, character, and subtitle assets. Validate voice,
   BGM, ambience, and SFX only when the selected pipeline enables audio; in
   local no-audio mode, verify the explicit `audio_skipped` result instead.
5. Convert nodes into the game template and generate server configuration.
6. Verify the built package path, browser endpoint, static asset loading, and
   task status.
7. Record whether audio was built, intentionally skipped, or failed.

## Hard Constraints

- Do not build from a partially generated or unvalidated story tree.
- Do not treat local no-audio preview as a complete audio release.
- Do not invent missing asset paths or silently hide failed nodes.
- Preserve locale, node, scene, subtitle, and asset identity through conversion.

Read [references/build-preview-contract.md](references/build-preview-contract.md)
for the current builder boundary.
