# Audio Asset Contract

## Cue Record

```json
{
  "asset_id": "ch2_scene1_aya_line04_enus_v1",
  "kind": "voice",
  "locale": "en-US",
  "node_id": "ch2",
  "segment_id": "ch2.scene1.line04",
  "speaker_id": "aya",
  "text": "I will stay.",
  "file": "assets/audio/en-US/ch2/aya-line04-v1.wav",
  "format": "wav",
  "sample_rate_hz": 48000,
  "duration_ms": 1240,
  "direction": {
    "emotion": "determined",
    "intensity": 0.65,
    "pace": "measured"
  },
  "review": {
    "text_match": true,
    "speaker_match": true,
    "technical_valid": true,
    "status": "approved"
  }
}
```

## Cue Rules

- Voice records must retain `segment_id`, `speaker_id`, locale, and exact text.
- BGM records must declare loop, fade-in, fade-out, and intended scene range.
- Sound effects must declare the triggering event and whether they are
  one-shot, layered, or repeatable.
- Keep source/provider, voice model or performer metadata, and prompt/script
  version when available.

## Review Checklist

- Spoken text matches the localized segment exactly.
- Pronunciation, speaker identity, emotional direction, and pacing are valid.
- No clipping, accidental silence, background noise, or abrupt boundaries.
- Loop points and fades are clean; dialogue remains intelligible over music.
- File format, duration, path, and asset ID are usable by integration.
