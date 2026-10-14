# Image Asset Contract

## Asset Record

```json
{
  "asset_id": "aya_sprite_neutral_v1",
  "kind": "sprite",
  "node_id": "ch2",
  "character_id": "aya",
  "state": {
    "expression": "neutral",
    "pose": "standing",
    "costume": "school_uniform",
    "injury": "none"
  },
  "file": "assets/characters/aya/sprite-neutral-v1.png",
  "mime_type": "image/png",
  "transparent": true,
  "prompt_version": "image-prompt-v1",
  "source_references": ["char_card:aya", "scene:ch2.scene1"],
  "review": {
    "identity_consistent": true,
    "continuity_consistent": true,
    "technical_valid": true,
    "status": "approved"
  }
}
```

## Naming and Variants

Use stable IDs based on semantic identity, not generation order alone:

`<character>_<kind>_<state>_<version>`

Keep expression, pose, costume, injury, location, time, and lighting as
explicit metadata. A revised image gets a new version or an explicit
replacement record; do not silently overwrite an approved asset.

## Review Checklist

- The intended character and state are recognizable.
- Backgrounds and props match the node's location and time.
- No unintended text, logos, watermarks, extra limbs, or identity drift.
- Transparent assets have clean edges and the expected canvas dimensions.
- CG composition leaves room for dialogue/UI overlays where required.
- The file path and asset ID are resolvable by the integration stage.
