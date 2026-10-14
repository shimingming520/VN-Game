import json
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.config import OUTPUTS_PATH
from generate_json_from_txt import (
    has_pronounceable_content,
    parse_line_info,
    strip_dialogue_quotes,
    strip_option_prefix,
)
from logger_config import setup_logger
from resume_state import (
    ResumeState,
    atomic_write_json,
    file_digest,
    stable_digest,
    valid_json,
    valid_wav,
)


logger = setup_logger("ElevenLabs_Dialogue_JSON", "elevenlabs_dialogue_json.log")

OUTPUT_ROOT = OUTPUTS_PATH
MAPPING_FILENAME = "elevenlabs_voice_mappings.json"
# 儿童/少年角色用的是 MiniMax 音色，必须走单句 /v1/audio/speech，
# 不能混进 ElevenLabs 的 text-to-dialogue 批次。
PROVIDER_FILENAME = "elevenlabs_voice_providers.json"
PROVIDER_MINIMAX = "minimax"
PROVIDER_ELEVENLABS = "elevenlabs"
MINIMAX_SPEECH_MODEL = os.getenv("MINIMAX_SPEECH_MODEL", "speech-2.8-hd")
MINIMAX_VOICES_FILE_PATH = os.getenv(
    "MINIMAX_VOICES_FILE_PATH",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "voice", "minimax_voices.json",
    ),
)
ELEVENLABS_VOICES_FILE_PATH = os.getenv(
    "ELEVENLABS_VOICES_FILE_PATH",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "voice", "elevenlabs_voices.json",
    ),
)
LANG_SUBTITLE_DIRS = {
    "zh-CN-subtitle": ("zh-CN", "zh"),
    "en-US-subtitle": ("en-US", "en"),
    "ja-JP-subtitle": ("ja-JP", "ja"),
}
# ElevenLabs 链只负责英语；日语由 MiniMax 链生成。
TARGET_LOCALES = tuple(
    item.strip()
    for item in os.getenv("ELEVENLABS_RELAY_LOCALES", "en-US").split(",")
    if item.strip()
)
MAX_BATCH_CHARS = int(os.getenv("ELEVENLABS_DIALOGUE_MAX_CHARS", "1900"))
MAX_UNIQUE_VOICES = 10
# Narration is emitted with PangBai by the plot extractor and must never enter
# character casting; otherwise a missing mapping warning causes it to borrow a
# random English voice and generate spoken narration by accident.
NON_VOICED_ROLE_IDS = {
    "xitong", "system", "pangbai", "narrator", "story", "nb", "旁白",
}
STEP_CACHE_VERSION = "elevenlabs-dialogue-json-v3"
STAGE_DIR_PREFIX = "elevenlabs-relay-stage-"
LEGACY_STAGE_DIR_PREFIX = ".elevenlabs-relay-stage-"
FAILED_BATCH_DIR_NAME = "failed-batches"

EMOTION_TAGS = {
    "sad": "[sad]",
    "happy": "[cheerfully]",
    "excited": "[excited]",
    "angry": "[angry]",
    "fearful": "[fearful]",
    "surprised": "[surprised]",
    "disgusted": "[disgusted]",
    "tired": "[tired]",
}

MATERIAL_ID_RE = re.compile(r"^(.*)-(\d+(?:-[A-Za-z]\d+)?)$")
BRANCH_BEAT_RE = re.compile(r"^(\d+)-([A-Za-z])(\d+)$")
CONTROL_BEAT_RE = re.compile(r"^\d+-C\d+$", re.IGNORECASE)


def _natural_key(value):
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", str(value))]


def _parse_material_id(filename):
    material_id = os.path.splitext(filename)[0]
    match = MATERIAL_ID_RE.match(material_id)
    if not match:
        return material_id, material_id, material_id, "main", None
    scene_id, beat_id = match.groups()
    branch_match = BRANCH_BEAT_RE.match(beat_id)
    branch_key = (
        f"{branch_match.group(1)}-{branch_match.group(2).upper()}"
        if branch_match else "main"
    )
    branch_base = int(branch_match.group(1)) if branch_match else None
    return material_id, scene_id, beat_id, branch_key, branch_base


def _is_control_beat(beat_id):
    """variable_update（-C1/-C2…）只有字幕寻址意义，不应生成 TTS。"""
    return CONTROL_BEAT_RE.fullmatch(str(beat_id or "").strip()) is not None


def _build_spoken_text(content, emotion, provider=PROVIDER_ELEVENLABS):
    """情绪标签只对 Eleven v3 有效。

    MiniMax 通过 voice_setting.emotion 参数表达情绪，文本里的 [angry] 会被
    当成普通字符念出来，所以这里必须按 provider 区分。
    """
    content = strip_dialogue_quotes(strip_option_prefix(content))
    if provider == PROVIDER_MINIMAX:
        return content
    tag = EMOTION_TAGS.get(emotion)
    return f"{tag} {content}" if tag else content


def _load_voice_mapping(task_id, memory_voice_map=None):
    if memory_voice_map:
        return memory_voice_map
    mapping_path = os.path.join(OUTPUT_ROOT, str(task_id), MAPPING_FILENAME)
    if not os.path.exists(mapping_path):
        logger.warning(f"⚠️ 找不到 ElevenLabs 音色映射：{mapping_path}")
        return {}
    with open(mapping_path, "r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def _load_minimax_voice_ids():
    """MiniMax 音色 id 集合，用作 provider 判定的兜底事实来源。"""
    try:
        with open(MINIMAX_VOICES_FILE_PATH, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except (OSError, ValueError) as exc:
        logger.warning(f"⚠️ MiniMax 音色库无法读取：{exc}")
        return frozenset()
    raw = payload.get("voices_list", payload) if isinstance(
        payload, dict
    ) else payload
    return frozenset(
        str(voice["voice_id"]) for voice in raw
        if isinstance(voice, dict) and voice.get("voice_id")
    )


def _load_fallback_elevenlabs_voice_id():
    try:
        with open(ELEVENLABS_VOICES_FILE_PATH, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except (OSError, ValueError) as exc:
        logger.warning(f"⚠️ ElevenLabs 音色库无法读取：{exc}")
        return None
    raw = payload.get("voices_list", payload) if isinstance(
        payload, dict
    ) else payload
    for voice in raw or []:
        if (
            isinstance(voice, dict)
            and voice.get("voice_id")
            and voice.get("status", "OK") == "OK"
        ):
            return str(voice["voice_id"])
    return None


def _load_provider_mapping(task_id):
    """provider 文件缺失或不完整时，按音色是否属于 MiniMax 库推断。

    这一步不能只信旁挂文件：文件丢了而映射里仍有 MiniMax 音色时，那些台词会
    被塞进 text-to-dialogue 批次，ElevenLabs 不认识该 voice_id，白烧额度。
    """
    provider_path = os.path.join(OUTPUT_ROOT, str(task_id), PROVIDER_FILENAME)
    if not os.path.exists(provider_path):
        logger.warning(
            f"⚠️ 找不到音色 provider 映射：{provider_path}；"
            "将按音色库归属推断 provider"
        )
        return {}
    with open(provider_path, "r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def _resolve_provider(role, voice_id, lang_provider_map, minimax_voice_ids):
    declared = lang_provider_map.get(role)
    inferred = (
        PROVIDER_MINIMAX
        if str(voice_id) in minimax_voice_ids
        else PROVIDER_ELEVENLABS
    )
    if declared and declared != inferred:
        logger.warning(
            f"⚠️ provider 标注与音色库不一致，按音色库为准："
            f"role={role}, voice_id={voice_id}, "
            f"declared={declared}, inferred={inferred}"
        )
    return inferred


def _batch_resume_identity(payload):
    item_key = (
        f"{payload['node_id']}/{payload['language']}/{payload['batch_id']}"
    )
    input_digest = stable_digest(
        STEP_CACHE_VERSION,
        {key: value for key, value in payload.items() if not key.startswith("_resume_")},
        file_digest(__file__),
        file_digest(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "generate_audio_from_elevenlabs_dialogue.py",
            )
        ),
    )
    return item_key, input_digest


def _write_batch_payload(payload, output_path, task_root):
    casting_digest = ResumeState(task_root).get_step(
        "relay_casting"
    ).get("input_digest")
    if casting_digest:
        payload["_resume_upstream_digest"] = casting_digest
    item_key, input_digest = _batch_resume_identity(payload)
    payload["_resume_item_key"] = item_key
    payload["_resume_input_digest"] = input_digest
    state = ResumeState(task_root)
    output_files = [
        os.path.join(
            task_root, payload["node_id"], "audios",
            f"{payload['language']}-audio", f"{item['material_id']}.wav",
        )
        for item in payload.get("inputs") or []
    ]
    if (
        output_files
        and state.item_matches("relay_audio", item_key, input_digest)
        and all(valid_wav(path) for path in output_files)
    ):
        try:
            os.remove(output_path)
        except OSError:
            pass
        return "audio_complete"
    if valid_json(output_path, lambda current: current == payload):
        state.mark_item(
            "relay_json", item_key, input_digest,
            status="success", artifacts=[output_path],
        )
        return "json_complete"
    atomic_write_json(output_path, payload)
    state.mark_item(
        "relay_json", item_key, input_digest,
        status="success", artifacts=[output_path],
    )
    return "written"


def _flush_minimax_batch(item, output_dir, task_root):
    """MiniMax 单句任务：一个文件一句，不参与切分。"""
    safe_material = re.sub(r"[^A-Za-z0-9_.-]+", "_", item["material_id"])
    batch_id = f"{safe_material}__minimax"
    output_path = os.path.join(output_dir, f"{batch_id}.json")
    payload = {
        "provider": PROVIDER_MINIMAX,
        "model_id": MINIMAX_SPEECH_MODEL,
        "language_code": item["language_code"],
        "node_id": item["node_id"],
        "language": item["language"],
        "scene_id": item["scene_id"],
        "branch_key": item["branch_key"],
        "batch_id": batch_id,
        "inputs": [{**item, "dialogue_input_index": 0}],
    }
    _write_batch_payload(payload, output_path, task_root)
    return output_path


def _flush_batch(batch, output_dir, batch_index, task_root):
    if not batch:
        return None
    for index, item in enumerate(batch):
        item["dialogue_input_index"] = index

    first = batch[0]
    safe_scene = re.sub(r"[^A-Za-z0-9_.-]+", "_", first["scene_id"])
    safe_branch = re.sub(r"[^A-Za-z0-9_.-]+", "_", first["branch_key"])
    batch_id = f"{safe_scene}__{safe_branch}__{batch_index:03d}"
    output_path = os.path.join(output_dir, f"{batch_id}.json")
    payload = {
        "provider": "elevenlabs",
        "model_id": "eleven_v3",
        "language_code": first["language_code"],
        "node_id": first["node_id"],
        "language": first["language"],
        "scene_id": first["scene_id"],
        "branch_key": first["branch_key"],
        "batch_id": batch_id,
        "inputs": batch,
    }
    _write_batch_payload(payload, output_path, task_root)
    return output_path


def _validate_failed_payload(payload, source_path):
    if not isinstance(payload, dict):
        raise RuntimeError(f"失败批次不是 JSON 对象：{source_path}")
    required = ("node_id", "language", "batch_id", "inputs")
    missing = [key for key in required if not payload.get(key)]
    if missing or not isinstance(payload.get("inputs"), list):
        raise RuntimeError(
            f"失败批次结构不完整：{source_path}，缺少/非法字段={missing or ['inputs']}"
        )
    node_id = str(payload["node_id"])
    language = str(payload["language"])
    if Path(node_id).name != node_id or Path(language).name != language:
        raise RuntimeError(f"失败批次包含非法输出路径：{source_path}")


def _copy_if_different(source, destination):
    """Atomically copy a diagnostic file unless source and destination coincide."""
    source_path = Path(source)
    destination_path = Path(destination)
    try:
        same_path = os.path.samefile(source_path, destination_path)
    except FileNotFoundError:
        same_path = os.path.abspath(os.path.realpath(source_path)) == os.path.abspath(
            os.path.realpath(destination_path)
        )
    except OSError:
        same_path = False
    if same_path:
        return False
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_name(
        f"{destination_path.name}.{os.getpid()}.tmp"
    )
    try:
        shutil.copy2(source_path, temporary_path)
        os.replace(temporary_path, destination_path)
    except shutil.SameFileError:
        # A concurrent retry may have made the destination the same inode after
        # the preflight check.  Treat that race as an idempotent no-op.
        return False
    finally:
        try:
            temporary_path.unlink()
        except OSError:
            pass
    return True


def _restore_failed_batches_only(task_root):
    """检测到失败缓存时，不扫描字幕，只恢复这些失败 JSON。

    多个旧 execution 的缓存会合并进最新可见 stage；同目标 JSON 内容冲突时
    明确失败，绝不猜测一个版本或静默退回全量重建。
    """
    stage_dirs = [
        path for pattern in (f"{STAGE_DIR_PREFIX}*", f"{LEGACY_STAGE_DIR_PREFIX}*")
        for path in Path(task_root).glob(pattern)
        if path.is_dir() and (path / FAILED_BATCH_DIR_NAME).exists()
    ]
    if not stage_dirs:
        return None

    canonical_stage = max(stage_dirs, key=lambda path: path.stat().st_mtime)
    if canonical_stage.name.startswith(LEGACY_STAGE_DIR_PREFIX):
        suffix = canonical_stage.name[len(LEGACY_STAGE_DIR_PREFIX):]
        visible = canonical_stage.with_name(f"{STAGE_DIR_PREFIX}{suffix}")
        counter = 1
        while visible.exists():
            visible = canonical_stage.with_name(
                f"{STAGE_DIR_PREFIX}{suffix}-{counter}"
            )
            counter += 1
        os.replace(canonical_stage, visible)
        stage_dirs = [visible if path == canonical_stage else path for path in stage_dirs]
        canonical_stage = visible

    cached_paths = [
        path
        for stage_dir in stage_dirs
        for path in (stage_dir / FAILED_BATCH_DIR_NAME).glob("*/*/*.json")
    ]
    if not cached_paths:
        raise RuntimeError(
            "检测到 failed-batches 目录但没有可解析的失败 JSON；"
            "为避免误触发全量生成，已停止任务"
        )

    restored_by_destination = {}
    current_casting_digest = ResumeState(task_root).get_step(
        "relay_casting"
    ).get("input_digest")
    for cached_path in sorted(cached_paths, key=lambda path: str(path)):
        try:
            with cached_path.open("r", encoding="utf-8") as file_obj:
                payload = json.load(file_obj)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"失败批次 JSON 损坏：{cached_path}: {exc}") from exc
        _validate_failed_payload(payload, cached_path)
        cached_casting_digest = payload.get("_resume_upstream_digest")
        if (
            current_casting_digest
            and cached_casting_digest
            and cached_casting_digest != current_casting_digest
        ):
            timestamp = int(time.time())
            for stage_dir in stage_dirs:
                failed_root = stage_dir / FAILED_BATCH_DIR_NAME
                if failed_root.exists():
                    os.replace(
                        failed_root,
                        stage_dir / f"invalidated-failed-batches-{timestamp}",
                    )
            logger.warning(
                "♻️ 失败缓存依赖的选角输入已变化，已保留 invalidated 诊断目录；"
                "转为重新规划批次，已有成品仍按资产摘要复用"
            )
            return None
        destination = Path(
            task_root, str(payload["node_id"]),
            f"{payload['language']}-elevenlabs-dialogue-json",
            cached_path.name,
        )
        digest = stable_digest(payload)
        previous = restored_by_destination.get(str(destination))
        if previous and previous != digest:
            raise RuntimeError(
                f"多个 execution 的失败缓存内容冲突：{destination}"
            )
        restored_by_destination[str(destination)] = digest
        atomic_write_json(destination, payload)

        canonical_failed_dir = Path(
            canonical_stage, FAILED_BATCH_DIR_NAME,
            str(payload["node_id"]), str(payload["language"]),
        )
        canonical_failed_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(canonical_failed_dir / cached_path.name, payload)
        source_error = cached_path.with_suffix(".error.txt")
        if source_error.is_file():
            _copy_if_different(source_error, canonical_failed_dir / source_error.name)

    # 合并后清掉其他 stage 的失败索引，防止下一次把已经成功的旧失败再次复活。
    for stage_dir in stage_dirs:
        if stage_dir == canonical_stage:
            continue
        shutil.rmtree(stage_dir / FAILED_BATCH_DIR_NAME, ignore_errors=True)

    logger.info(
        f"♻️ 失败续跑模式：只恢复 {len(restored_by_destination)} 个失败批次，"
        "不扫描字幕、不重建成功批次"
    )
    return {
        "batches": len(restored_by_destination),
        "inputs": sum(
            len(json.loads(Path(path).read_text("utf-8")).get("inputs") or [])
            for path in restored_by_destination
        ),
        "skipped": 0,
        "failed": 0,
        "resumed_failed_only": True,
    }


def main(target_task_id, memory_voice_map=None):
    task_root = os.path.join(OUTPUT_ROOT, str(target_task_id))
    if not os.path.exists(task_root):
        logger.warning(f"⚠️ 找不到任务产物目录，跳过：{task_root}")
        return {"batches": 0, "inputs": 0, "skipped": 0, "failed": 0}

    failed_resume_result = _restore_failed_batches_only(task_root)
    if failed_resume_result is not None:
        ResumeState(task_root).mark_step(
            "relay_json",
            stable_digest("failed-resume", failed_resume_result),
            status="success",
        )
        return failed_resume_result

    loaded_map = _load_voice_mapping(target_task_id, memory_voice_map)
    provider_map = _load_provider_mapping(target_task_id)
    minimax_voice_ids = _load_minimax_voice_ids()
    library_fallback_voice_id = _load_fallback_elevenlabs_voice_id()
    grouped_items = defaultdict(list)
    skipped = 0
    non_voiced_lines = 0

    for root, _, files in os.walk(task_root):
        dir_name = os.path.basename(root)
        if dir_name not in LANG_SUBTITLE_DIRS:
            continue
        language, language_code = LANG_SUBTITLE_DIRS[dir_name]
        if language not in TARGET_LOCALES:
            continue
        node_id = Path(root).parents[1].name
        lang_map = {
            str(role).lower(): voice_id
            for role, voice_id in (loaded_map.get(language) or {}).items()
        }
        # 默认音色必须来自 ElevenLabs。不能直接拿映射中的第一个音色：
        # 映射通常先出现 MiniMax 儿童/少年音色，拿它兜底会把未知角色
        # 错误地送入 MiniMax 单句分支，并造成“默认音色”跨角色漂移。
        fallback_voice_id = next(
            (
                voice_id for voice_id in lang_map.values()
                if str(voice_id) not in minimax_voice_ids
            ),
            library_fallback_voice_id,
        )
        lang_provider_map = {
            str(role).lower(): provider
            for role, provider in (provider_map.get(language) or {}).items()
        }

        parsed_files = []
        branch_bases_by_scene = defaultdict(set)
        for filename in files:
            if filename.endswith(".txt"):
                parsed = _parse_material_id(filename)
                # Step 2 会为 variable_update 输出原样字幕，Step 3 不得把
                # effects/flags 控制字段误当成可发音台词。
                if _is_control_beat(parsed[2]):
                    continue
                parsed_files.append((filename, parsed))
                if parsed[4] is not None:
                    branch_bases_by_scene[parsed[1]].add(parsed[4])

        for filename, parsed_material in sorted(
            parsed_files, key=lambda item: _natural_key(item[0])
        ):
            source_path = os.path.join(root, filename)
            try:
                with open(source_path, "r", encoding="utf-8") as file_obj:
                    raw_text = file_obj.read().strip()
            except OSError as exc:
                skipped += 1
                logger.error(f"⚠️ 字幕读取失败，跳过本条继续：{source_path}: {exc}")
                continue
            if not raw_text:
                skipped += 1
                continue

            valid, _, role, emotion, content = parse_line_info(raw_text)
            if not valid:
                skipped += 1
                continue
            if str(role).strip().lower() in NON_VOICED_ROLE_IDS:
                skipped += 1
                continue
            if not has_pronounceable_content(strip_option_prefix(content)):
                skipped += 1
                non_voiced_lines += 1
                logger.info(
                    f"⏭️ 台词没有可发音字符，不生成配音："
                    f"{filename} -> {json.dumps(content, ensure_ascii=False)}"
                )
                continue

            role_key = str(role or "").strip().lower()
            voice_id = lang_map.get(role_key)
            if not voice_id:
                if fallback_voice_id:
                    voice_id = fallback_voice_id
                    logger.warning(
                        f"⚠️ ElevenLabs 音色映射缺失："
                        f"language={language}, role={role}；"
                        f"复用本语言默认音色 {voice_id}"
                    )
                else:
                    skipped += 1
                    logger.error(
                        f"⚠️ ElevenLabs 无任何可复用音色，"
                        f"跳过台词：{source_path}"
                    )
                    continue

            material_id, scene_id, beat_id, branch_key, _ = parsed_material
            if branch_key == "main" and branch_bases_by_scene.get(scene_id):
                beat_number_match = re.match(r"^(\d+)$", beat_id)
                if beat_number_match:
                    beat_number = int(beat_number_match.group(1))
                    segment_index = sum(
                        1 for base in branch_bases_by_scene[scene_id]
                        if base < beat_number
                    )
                    branch_key = f"main-{segment_index}"
            provider = _resolve_provider(
                role_key, voice_id, lang_provider_map, minimax_voice_ids
            )
            spoken_text = _build_spoken_text(content, emotion, provider)
            if len(spoken_text) > MAX_BATCH_CHARS:
                skipped += 1
                logger.error(
                    f"⚠️ 单条台词超过 ElevenLabs 批次上限，"
                    f"跳过本条并继续：{source_path} "
                    f"({len(spoken_text)} > {MAX_BATCH_CHARS})"
                )
                continue
            grouped_items[(node_id, language, scene_id, branch_key)].append({
                "material_id": material_id,
                "source_filename": filename,
                "speaker_id": role,
                "provider": provider,
                "emotion": emotion,
                "text": spoken_text,
                "voice_id": voice_id,
                "node_id": node_id,
                "language": language,
                "language_code": language_code,
                "scene_id": scene_id,
                "branch_key": branch_key,
            })

    if not grouped_items:
        logger.warning("⚠️ 没有找到需要生成 ElevenLabs 对话音频的台词")
        return {"batches": 0, "inputs": 0, "skipped": skipped, "failed": 0}

    written = 0
    total_inputs = 0
    minimax_lines = 0
    expected_json_paths = set()
    for group_key in sorted(grouped_items, key=lambda key: tuple(map(str, key))):
        items = sorted(
            grouped_items[group_key],
            key=lambda item: _natural_key(item["material_id"]),
        )
        node_id, language, _, _ = group_key
        output_dir = os.path.join(
            task_root, node_id, f"{language}-elevenlabs-dialogue-json"
        )
        os.makedirs(output_dir, exist_ok=True)

        batch = []
        char_count = 0
        voice_ids = set()
        batch_index = 1
        for item in items:
            # MiniMax 音色（儿童/少年）从批次里抽出单独合成；抽走后剩下的
            # 台词仍按原顺序组批，material_id 与字幕保持一一对应。
            if item["provider"] == PROVIDER_MINIMAX:
                try:
                    output_path = _flush_minimax_batch(item, output_dir, task_root)
                    expected_json_paths.add(os.path.abspath(output_path))
                except Exception as exc:
                    skipped += 1
                    logger.error(
                        f"⚠️ MiniMax 单句 JSON 写入失败，跳过本条继续："
                        f"{item['material_id']}: {exc}"
                    )
                else:
                    written += 1
                    total_inputs += 1
                    minimax_lines += 1
                continue

            next_voice_ids = voice_ids | {item["voice_id"]}
            would_overflow = (
                batch
                and (
                    char_count + len(item["text"]) > MAX_BATCH_CHARS
                    or len(next_voice_ids) > MAX_UNIQUE_VOICES
                )
            )
            if would_overflow:
                try:
                    output_path = _flush_batch(
                        batch, output_dir, batch_index, task_root
                    )
                    expected_json_paths.add(os.path.abspath(output_path))
                except Exception as exc:
                    skipped += len(batch)
                    logger.error(
                        f"⚠️ ElevenLabs Dialogue JSON 批次写入失败，"
                        f"跳过本批继续：{exc}"
                    )
                else:
                    written += 1
                    total_inputs += len(batch)
                batch_index += 1
                batch = []
                char_count = 0
                voice_ids = set()

            batch.append(item)
            char_count += len(item["text"])
            voice_ids.add(item["voice_id"])

        if batch:
            try:
                output_path = _flush_batch(
                    batch, output_dir, batch_index, task_root
                )
                expected_json_paths.add(os.path.abspath(output_path))
            except Exception as exc:
                skipped += len(batch)
                logger.error(
                    f"⚠️ ElevenLabs Dialogue JSON 批次写入失败，"
                    f"跳过本批继续：{exc}"
                )
            else:
                written += 1
                total_inputs += len(batch)

    # 输入变化导致分组/批次号变化时，只删除已经不属于当前计划的旧 JSON；
    # 已成功且被 Step 5 消费掉的 JSON 本来就不存在，不会被重新创建。
    stale_removed = 0
    dialogue_suffix = "-elevenlabs-dialogue-json"
    for root, _, files in os.walk(task_root):
        if not os.path.basename(root).endswith(dialogue_suffix):
            continue
        for filename in files:
            if not filename.endswith(".json"):
                continue
            path = os.path.abspath(os.path.join(root, filename))
            if path in expected_json_paths:
                continue
            os.remove(path)
            stale_removed += 1
    if stale_removed:
        logger.info(f"🧹 已删除 {stale_removed} 个输入已变化的旧 Dialogue JSON")

    logger.info(
        f"✅ ElevenLabs Dialogue JSON 完成：批次={written}, "
        f"配音台词={total_inputs}, 过滤字幕={skipped}, "
        f"其中无可发音字符={non_voiced_lines}, "
        f"MiniMax 单句={minimax_lines}"
    )
    item_states = ResumeState(task_root).list_items("relay_json")
    ResumeState(task_root).mark_step(
        "relay_json",
        stable_digest(
            STEP_CACHE_VERSION,
            sorted(
                (key, value.get("input_digest"), value.get("status"))
                for key, value in item_states.items()
            ),
        ),
        status="success",
    )
    return {
        "batches": written,
        "inputs": total_inputs,
        "skipped": skipped,
        "non_voiced": non_voiced_lines,
    }


if __name__ == "__main__":
    main("")
