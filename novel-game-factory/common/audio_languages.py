"""音频生产链的语言契约。

中文可以继续作为 plots 的源文本语言，但不再属于音频生产产物语言。
所有新字幕、音色映射、TTS JSON 和音频只允许英语与日语。
"""

from pathlib import Path
import shutil


AUDIO_OUTPUT_LANGUAGES = (
    {
        "code": "en",
        "locale": "en-US",
        "label": "英语",
        "subtitle_dir": "en-US-subtitle",
        "json_dir": "en-US-json",
        "audio_dir": "en-US-audio",
        "language_boost": "English",
    },
    {
        "code": "ja",
        "locale": "ja-JP",
        "label": "日语",
        "subtitle_dir": "ja-JP-subtitle",
        "json_dir": "ja-JP-json",
        "audio_dir": "ja-JP-audio",
        "language_boost": "Japanese",
    },
)

AUDIO_OUTPUT_CODES = tuple(item["code"] for item in AUDIO_OUTPUT_LANGUAGES)
AUDIO_OUTPUT_LOCALES = tuple(item["locale"] for item in AUDIO_OUTPUT_LANGUAGES)
AUDIO_SUBTITLE_DIRS = frozenset(item["subtitle_dir"] for item in AUDIO_OUTPUT_LANGUAGES)
AUDIO_JSON_DIRS = frozenset(item["json_dir"] for item in AUDIO_OUTPUT_LANGUAGES)
AUDIO_AUDIO_DIRS = frozenset(item["audio_dir"] for item in AUDIO_OUTPUT_LANGUAGES)
AUDIO_LANG_CONFIGS = tuple(
    (item["locale"], item["label"]) for item in AUDIO_OUTPUT_LANGUAGES
)
AUDIO_LANGUAGE_BOOST_MAP = {
    item["locale"]: item["language_boost"] for item in AUDIO_OUTPUT_LANGUAGES
}

DEFAULT_AUDIO_LANGUAGE = "en-US"

# 只用于识别历史结构，不能重新生成这些语言的产物。
KNOWN_AUDIO_LOCALES = frozenset({"zh-CN", *AUDIO_OUTPUT_LOCALES})
DISABLED_AUDIO_LOCALES = frozenset({"zh-CN"})
DISABLED_AUDIO_DIRS = frozenset(
    f"{locale}-{kind}"
    for locale in DISABLED_AUDIO_LOCALES
    for kind in ("subtitle", "json", "audio")
)


def cleanup_disabled_local_audio_outputs(task_root) -> list[str]:
    """删除已停产语言的本地字幕、临时 JSON 与配音目录。"""
    root = Path(task_root)
    if not root.exists():
        return []

    removed = []
    for audios_dir in root.glob("*/audios"):
        if not audios_dir.is_dir():
            continue
        for dir_name in DISABLED_AUDIO_DIRS:
            target = audios_dir / dir_name
            if target.is_dir():
                shutil.rmtree(target)
                removed.append(str(target))
    return removed


def cleanup_orphan_audio_outputs(task_root, locale) -> list[str]:
    """删除本语言下没有对应字幕的 WAV，返回被删除的相对路径。

    剧情改动后 Step 2 会重建字幕目录，被删掉的台词不会再有字幕；但它上一轮
    生成的 WAV 留在 ``audios/{locale}-audio/`` 里会被 game_builder 打包进成品。
    WAV 与字幕严格同名（``{scene_id}-{beat_id}``），所以字幕是唯一权威清单。
    字幕目录整体缺失时不做任何删除：那是上游未产出，不是「台词被删」。
    """
    root = Path(task_root)
    if not root.exists():
        return []

    removed = []
    for audio_dir in root.glob(f"*/audios/{locale}-audio"):
        if not audio_dir.is_dir():
            continue
        subtitle_dir = audio_dir.with_name(f"{locale}-subtitle")
        if not subtitle_dir.is_dir():
            continue
        expected = {path.stem for path in subtitle_dir.glob("*.txt")}
        for wav_path in audio_dir.glob("*.wav"):
            if wav_path.stem in expected:
                continue
            wav_path.unlink()
            removed.append(str(wav_path.relative_to(root)))
    return removed
