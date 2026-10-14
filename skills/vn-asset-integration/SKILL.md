---
name: vn-asset-integration
description: Use when mapping visual-novel text, image, and audio outputs into scene manifests, runtime references, localization bundles, or publishable game assets.
---

# VN Asset Integration

Turn reviewed text and media outputs into deterministic, resolvable scene
manifests without changing story topology or silently accepting missing files.
The project already has a concrete character-image manifest produced by
`char_designer`. A single unified manifest covering scene images, voice, BGM,
SFX, and ambience has not been confirmed. This Skill defines the cross-service
integration contract and must use the target builder's actual manifest support.

## Workflow

1. Load the validated node artifact, locale bundle, image records, audio
   records, and target runtime's existing asset conventions.
2. Map each scene, dialogue segment, choice, image, and audio cue by stable ID.
3. Validate paths, file types, locale coverage, node/segment references,
   timing metadata, and required fallback behavior.
4. Produce a manifest or handoff that the existing builder can consume.
5. Report missing, duplicate, stale, or incompatible assets as findings.
6. Publish only after reference and completeness checks pass.

## Hard Constraints

- Preserve `node_id`, `child_id`, choice effects, segment IDs, and asset IDs.
- Never make a missing asset appear valid by inventing a path or placeholder.
- Keep locale-specific assets separate while allowing an explicit fallback.
- Treat `manifests/character_image_manifest.json` as an existing character
  manifest contract, not proof that all other media types share its schema.
- Do not alter Python code, database schema, or runtime configuration merely
  to accommodate an unverified asset.

Read [references/asset-manifest-contract.md](references/asset-manifest-contract.md)
for the integration handoff and validation rules.
