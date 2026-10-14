---
name: vn-audio-generation
description: Use when creating or preparing visual-novel voice lines, narration, BGM, sound effects, audio cues, or audio asset metadata for approved scenes.
---

# VN Audio Generation

Create audio that matches the approved scene, speaker state, emotional
direction, timing, and route context. Treat voice, music, and sound effects
as separate asset classes with different review criteria. This is a
provider-neutral workflow across the project's audio services.
`story_planner` does not execute audio generation. `audio_producer` generates
voice lines and validates subtitle, dialogue JSON, and WAV outputs;
`sound_arranger` generates or reuses BGM, SFX, and ambience assets.
When `LOCAL_PIPELINE_MODE=1`, the current visual chain intentionally omits both
audio services; record `audio_skipped` rather than invoking them or reporting
audio completion.

## Workflow

1. Load the chapter artifact, scene boundaries, speaker metadata, emotional
   direction, locale, content warnings, and existing audio references.
2. Classify each cue as voice, narration, BGM, ambience, or sound effect.
3. Prepare a cue brief with asset ID, scene/segment, speaker, text or event,
   delivery direction, start/stop behavior, and loop/fade requirements.
4. Generate or record the candidate using the approved audio tool/provider.
5. Review intelligibility, pronunciation, emotion, clipping, silence, timing,
   loop points, loudness, and continuity with adjacent cues.
6. Save the service-supported audio asset record or manifest and link each cue
   to the exact scene or segment; do not assume a unified cross-media manifest.

## Hard Constraints

- In local no-audio visual mode, preserve the skip result and do not treat
  subtitle materialization as generated voice, BGM, or SFX.
- Do not change dialogue wording, speaker identity, or scene causality during
  voice generation.
- Do not use a voice identity without the project's required authorization.
- Keep BGM and ambience from masking dialogue or important sound effects.
- Treat generated audio as a candidate until technical and narrative review
  passes.

Read [references/audio-asset-contract.md](references/audio-asset-contract.md)
before producing an audio handoff.
