# Build And Preview Contract

## Implementation Map

| Concern | Implementation |
| --- | --- |
| Build task | `novel-game-factory/services/game_builder/tasks.py` |
| Story and asset conversion | `novel-game-factory/services/game_builder/converter.py` |
| Game package output | `storage/built_games/<task_id>` |
| Browser gateway | `creation-engine/gateway/games.py` |
| Local visual runtime mode | `novel-game-factory/common/local_runtime.py` |

## Local Visual Mode

The local visual path may materialize UTF-8 subtitle files from plot data
without generating or requiring voice audio. Record this as an explicit
`audio_skipped` stage result. It is not equivalent to an audio-complete
publication.

## Release Checks

Verify source directory, story-tree normalization, required node content,
asset path resolution, build output, and browser-serving configuration. A
successful builder task does not erase upstream quality warnings.
