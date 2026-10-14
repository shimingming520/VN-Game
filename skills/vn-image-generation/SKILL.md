---
name: vn-image-generation
description: Use when creating or revising visual-novel character sprites, expressions, backgrounds, CGs, UI illustrations, or image asset metadata from an approved story package.
---

# VN Image Generation

Create visual assets that serve an approved node and remain consistent across
characters, locations, lighting, costume state, and narrative continuity.
This is a provider-neutral workflow across the project's media services.
`story_planner` plans story data but does not execute image generation;
`char_designer` generates character images and their character-image manifest,
while `scene_painter` generates scene, background, and CG images. Use the
service-specific execution path supplied by the caller.

## Workflow

1. Load the node plan, character cards, world rules, scene outline, visual
   style context, and any existing reference assets.
2. Classify each request as sprite, expression variant, background, CG, prop,
   effect layer, or UI asset.
3. Write an asset brief with semantic purpose, subject identity, pose,
   expression, camera framing, setting, time, lighting, exclusions, and
   continuity references.
4. Generate or edit the asset using the approved image tool/provider.
5. Review identity, anatomy, text artifacts, composition, transparency,
   resolution, and consistency with neighboring assets.
6. Save a deterministic asset record and link it to the scene or segment.

## Hard Constraints

- Do not invent a character, costume state, location, or event that the node
  plan does not authorize.
- Keep identity-critical features stable across sprite, expression, and CG
  variants.
- Treat generated images as candidates until visual review passes.
- Keep prompts, seeds, model/provider metadata, and source references with the
  asset record when available.

Read [references/image-asset-contract.md](references/image-asset-contract.md)
before producing an image handoff.
