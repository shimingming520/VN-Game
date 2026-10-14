# Name Mapping Contract

## Implementation Map

| Concern | Implementation |
| --- | --- |
| Mapping readers and language aliases | `novel-game-factory/common/name_mapping_utils.py` |
| Generic appellation detection | `novel-game-factory/common/generic_appellations.py` |
| Extractor-side mapping generation | `novel-game-factory/services/info_extractor/character/name_mappings.py` |
| Persisted outputs | `storage/outputs/<task_id>/name_mappings.json`, `name_replacements.json` |

## Mapping Layers

Keep these concepts separate:

- `original_name`: source names and source-to-canonical conversion.
- `change_name`: anonymized names and conversion.
- Locale sections such as `zh-CN`, `en-US`, and `ja-JP`: localized display
  names keyed by canonical speaker identity.
- Machine identifiers: stable values that are never subject to name replacement.

## Generic-Term Rules

Preserve standalone generic terms. For a surname-plus-role form such as a
surname followed by driver or teacher, replace only the surname. For a
confident complete personal name, replace surname and given name according to
the active mapping. When confidence is insufficient, preserve the text and
record a review item.
