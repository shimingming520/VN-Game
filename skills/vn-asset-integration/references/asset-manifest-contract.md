# Asset Manifest Contract

The scene manifest below is an illustrative cross-service handoff shape. It is
not proof that the current project writes or consumes one unified manifest for
scene images, voice, BGM, SFX, and ambience. Use the target builder's actual
contract when one exists; otherwise preserve separate service manifests and
report the integration gap.

## Scene Manifest

```json
{
  "node_id": "ch2",
  "locale": "en-US",
  "content_file": "ch2.en-US.md",
  "segments": [
    {
      "segment_id": "ch2.scene1.line04",
      "speaker_id": "aya",
      "text": "I will stay.",
      "image_asset_ids": ["aya_sprite_neutral_v1"],
      "audio_asset_ids": ["ch2_scene1_aya_line04_enus_v1"]
    }
  ],
  "choices": [
    {
      "choice_id": "ch2.choice1",
      "child_id": "ch3",
      "effects": {"honesty": 3}
    }
  ],
  "validation": {
    "all_paths_resolvable": true,
    "all_ids_unique": true,
    "locale_complete": true,
    "status": "ready"
  }
}
```

## Validation Rules

- Every referenced node, child, segment, speaker, and asset ID exists.
- Every file path resolves to the expected file type.
- Asset IDs are unique within the manifest and stable across rebuilds.
- Localized text, voice text, and UI labels use the same locale and glossary.
- Missing optional assets are explicitly marked `pending` or `fallback`; they
  are not silently omitted.
- A failed validation blocks publication of the affected node or bundle.
