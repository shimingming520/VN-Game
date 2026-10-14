"""MiniMax 英语整线灾备。

所有英语音频先生成到任务共享目录中的确定性暂存区。只有本次字幕对应的
全部 WAV 都通过验收后，才以可回滚的目录替换方式发布到正式 en-US-audio。
因此灾备失败不会破坏已经发布的 ElevenLabs 音频。
"""

import json
import os
from pathlib import Path
import shutil

from common.config import OUTPUTS_PATH
from resume_state import (
    ResumeState,
    atomic_write_json,
    file_digest,
    stable_digest,
    valid_wav,
)

from auto_cast_voices_from_db import run_auto_casting
from generate_json_from_txt import main as generate_minimax_json
from generate_audio_from_speech import main as generate_minimax_audio
from logger_config import setup_logger


logger = setup_logger(
    "MiniMaxEnglishFallback", "pipeline_minimax_english_fallback.log"
)

LOCALE = "en-US"
STAGE_DIR_NAME = "minimax-en-US-fallback-stage"
VOICE_MAPPING_FILENAME = "minimax_voice_mappings.en-US.json"
PROVIDER_MANIFEST_FILENAME = "audio_provider_manifest.json"
FALLBACK_STATE_KEY = "minimax_english_fallback"
COMMIT_JOURNAL_FILENAME = "fallback-commit-journal.json"
COMMIT_BACKUP_DIR_NAME = "fallback-commit-backup"


def _subtitle_nodes(task_root):
    return sorted(
        subtitle_dir.parents[1].name
        for subtitle_dir in Path(task_root).glob(
            f"*/audios/{LOCALE}-subtitle"
        )
        if subtitle_dir.is_dir()
    )


def _english_subtitle_digest(task_root):
    task_root = Path(task_root)
    entries = [
        (str(path.relative_to(task_root)), file_digest(path))
        for path in sorted(
            task_root.glob(f"*/audios/{LOCALE}-subtitle/*.txt"), key=str
        )
        if path.is_file()
    ]
    return stable_digest("en-US-subtitles-v1", entries)


def _expected_audio_paths(stage_root, task_root):
    expected = set()
    for json_path in Path(stage_root).glob(f"*/{LOCALE}-json/*.json"):
        node_id = json_path.parent.parent.name
        expected.add(
            Path(node_id) / "audios" / f"{LOCALE}-audio"
            / f"{json_path.stem}.wav"
        )
    # 成功资产的 JSON 会被 Step 5 删除。续跑时必须从状态中恢复这些预期项，
    # 否则只剩失败 JSON 时会误把已完成 WAV 当成陈旧文件删掉。
    audio_items = ResumeState(stage_root).list_items(
        "minimax_english_fallback_audio"
    )
    for item_key, item_state in audio_items.items():
        try:
            node_id, locale, stem = item_key.split("/", 2)
        except ValueError:
            continue
        subtitle_path = (
            Path(task_root) / node_id / "audios" / f"{locale}-subtitle"
            / f"{stem}.txt"
        )
        wav_path = (
            Path(stage_root) / node_id / "audios" / f"{locale}-audio"
            / f"{stem}.wav"
        )
        if (
            locale == LOCALE
            and subtitle_path.is_file()
            and item_state.get("status") == "success"
            and valid_wav(wav_path)
        ):
            expected.add(wav_path.relative_to(stage_root))
    return expected


def _prepare_stage_audio_dirs(stage_root, node_ids, expected_paths):
    stage_root = Path(stage_root)
    expected_absolute = {stage_root / path for path in expected_paths}
    for node_id in node_ids:
        (stage_root / node_id / "audios" / f"{LOCALE}-audio").mkdir(
            parents=True, exist_ok=True
        )
    for wav_path in stage_root.glob(f"*/audios/{LOCALE}-audio/*.wav"):
        if wav_path not in expected_absolute:
            wav_path.unlink()


def _validate_complete_stage(stage_root, expected_paths):
    invalid = [
        str(relative_path)
        for relative_path in sorted(expected_paths, key=str)
        if not valid_wav(Path(stage_root) / relative_path)
    ]
    if invalid:
        preview = ", ".join(invalid[:5])
        raise RuntimeError(
            f"MiniMax 英语灾备产物不完整：{len(invalid)} 个 WAV 缺失或损坏"
            f"（示例: {preview}）"
        )


def _publish_audio_directories(task_root, stage_root, node_ids, owner_check):
    """替换所有英语目录；任一替换失败时回滚已经切换的节点。"""
    task_root = Path(task_root)
    stage_root = Path(stage_root)
    backup_root = stage_root / COMMIT_BACKUP_DIR_NAME
    journal_path = stage_root / COMMIT_JOURNAL_FILENAME
    records = []
    for node_id in node_ids:
        source = stage_root / node_id / "audios" / f"{LOCALE}-audio"
        destination = task_root / node_id / "audios" / f"{LOCALE}-audio"
        backup = backup_root / node_id / f"{LOCALE}-audio"
        records.append((source, destination, backup, destination.exists()))
    atomic_write_json(journal_path, {
        "version": 1,
        "records": [
            {
                "source": str(source),
                "destination": str(destination),
                "backup": str(backup),
                "had_previous": had_previous,
            }
            for source, destination, backup, had_previous in records
        ],
    })
    committed = []
    try:
        for source, destination, backup, had_previous in records:
            owner_check()
            source.mkdir(parents=True, exist_ok=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if had_previous:
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, backup)
            try:
                os.replace(source, destination)
            except Exception:
                if had_previous and backup.exists():
                    os.replace(backup, destination)
                raise
            committed.append((source, destination, backup, had_previous))
    except Exception:
        for source, destination, backup, had_previous in reversed(committed):
            if destination.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                if source.exists():
                    shutil.rmtree(source)
                os.replace(destination, source)
            if had_previous and backup.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, destination)
        raise
    finally:
        if backup_root.exists() and not committed:
            shutil.rmtree(backup_root, ignore_errors=True)
    return records, backup_root, journal_path


def _rollback_audio_directories(records, backup_root, journal_path):
    _restore_metadata_from_journal(journal_path)
    for source, destination, backup, had_previous in reversed(records):
        if destination.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            if source.exists():
                shutil.rmtree(source)
            os.replace(destination, source)
        if had_previous and backup.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, destination)
    shutil.rmtree(backup_root, ignore_errors=True)
    Path(journal_path).unlink(missing_ok=True)


def _finish_audio_directory_publish(backup_root, journal_path):
    shutil.rmtree(backup_root, ignore_errors=True)
    Path(journal_path).unlink(missing_ok=True)


def _prepare_metadata_rollback(task_root, backup_root, journal_path):
    metadata = []
    for filename in (VOICE_MAPPING_FILENAME, PROVIDER_MANIFEST_FILENAME):
        destination = Path(task_root) / filename
        backup = Path(backup_root) / "metadata" / filename
        had_previous = destination.is_file()
        if had_previous:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup)
        metadata.append({
            "destination": str(destination),
            "backup": str(backup),
            "had_previous": had_previous,
        })
    payload = json.loads(Path(journal_path).read_text("utf-8"))
    payload["metadata"] = metadata
    atomic_write_json(journal_path, payload)


def _restore_metadata_from_journal(journal_path):
    try:
        payload = json.loads(Path(journal_path).read_text("utf-8"))
    except (OSError, ValueError, TypeError):
        return
    for record in reversed(payload.get("metadata") or []):
        destination = Path(record["destination"])
        backup = Path(record["backup"])
        if destination.exists():
            destination.unlink()
        if record.get("had_previous") and backup.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, destination)


def _recover_interrupted_publish(task_root, stage_root):
    """Pod 若在多目录切换中途退出，下次执行先恢复发布前状态。"""
    journal_path = Path(stage_root) / COMMIT_JOURNAL_FILENAME
    if not journal_path.is_file():
        return False
    try:
        payload = json.loads(journal_path.read_text("utf-8"))
        records = payload["records"]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"MiniMax 英语灾备提交日志损坏：{journal_path}") from exc
    _restore_metadata_from_journal(journal_path)
    for record in reversed(records):
        source = Path(record["source"])
        destination = Path(record["destination"])
        backup = Path(record["backup"])
        had_previous = bool(record["had_previous"])
        # source 消失说明新目录已经发布到 destination，先移回暂存区。
        if not source.exists() and destination.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, source)
        if had_previous and backup.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(backup, destination)
    shutil.rmtree(Path(stage_root) / COMMIT_BACKUP_DIR_NAME, ignore_errors=True)
    journal_path.unlink(missing_ok=True)
    logger.warning("♻️ 已回滚上次中断的 MiniMax 英语目录切换，将从暂存区续跑")
    return True


def _write_provider_manifest(task_root, expected_paths, fallback_reason):
    manifest_path = Path(task_root) / PROVIDER_MANIFEST_FILENAME
    try:
        with manifest_path.open("r", encoding="utf-8") as file_obj:
            manifest = json.load(file_obj)
    except (OSError, ValueError, TypeError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    manifest["schema_version"] = 1
    locales = manifest.setdefault("locales", {})
    locales[LOCALE] = {
        "provider": "minimax-fallback",
        "status": "success",
        "fallback_reason": str(fallback_reason),
        "input_digest": _english_subtitle_digest(task_root),
        "implementation_digest": file_digest(__file__),
        "assets": {
            str(path): {"provider": "minimax-fallback", "status": "success"}
            for path in sorted(expected_paths, key=str)
        },
    }
    atomic_write_json(manifest_path, manifest)
    return str(manifest_path)


def is_current_minimax_english_fallback(task_id):
    """已完整发布的灾备结果在同一输入下保持粘性，避免重跑时混入 EL。"""
    task_root = Path(OUTPUTS_PATH) / str(task_id)
    manifest_path = task_root / PROVIDER_MANIFEST_FILENAME
    try:
        payload = json.loads(manifest_path.read_text("utf-8"))
        locale_state = payload["locales"][LOCALE]
        assets = locale_state["assets"]
    except (OSError, ValueError, TypeError, KeyError):
        return False
    if (
        locale_state.get("provider") != "minimax-fallback"
        or locale_state.get("status") != "success"
        or locale_state.get("input_digest") != _english_subtitle_digest(task_root)
        or locale_state.get("implementation_digest") != file_digest(__file__)
        or not isinstance(assets, dict)
    ):
        return False
    return all(valid_wav(task_root / relative_path) for relative_path in assets)


def has_minimax_english_fallback_history(task_id):
    manifest_path = (
        Path(OUTPUTS_PATH) / str(task_id) / PROVIDER_MANIFEST_FILENAME
    )
    try:
        payload = json.loads(manifest_path.read_text("utf-8"))
        return (
            payload["locales"][LOCALE].get("provider")
            == "minimax-fallback"
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False


def record_primary_english_provider(task_id):
    """记录正常英语链最终已发布的资产，便于后续审计实际供应链。"""
    task_root = Path(OUTPUTS_PATH) / str(task_id)
    assets = {
        str(path.relative_to(task_root)): {
            "provider": "elevenlabs-primary-chain",
            "status": "success",
        }
        for path in sorted(
            task_root.glob(f"*/audios/{LOCALE}-audio/*.wav"), key=str
        )
        if valid_wav(path)
    }
    manifest_path = task_root / PROVIDER_MANIFEST_FILENAME
    try:
        with manifest_path.open("r", encoding="utf-8") as file_obj:
            manifest = json.load(file_obj)
    except (OSError, ValueError, TypeError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    manifest["schema_version"] = 1
    manifest.setdefault("locales", {})[LOCALE] = {
        "provider": "elevenlabs-primary-chain",
        "status": "success",
        "input_digest": _english_subtitle_digest(task_root),
        "assets": assets,
    }
    atomic_write_json(manifest_path, manifest)
    return str(manifest_path)


def run_minimax_english_fallback(
    task_id,
    *,
    fallback_reason,
    owner_check=None,
    refresh_voice_map=True,
):
    owner_check = owner_check or (lambda: None)
    task_root = Path(OUTPUTS_PATH) / str(task_id)
    if not task_root.is_dir():
        raise RuntimeError(f"MiniMax 英语灾备找不到任务目录：{task_root}")
    node_ids = _subtitle_nodes(task_root)
    if not node_ids:
        raise RuntimeError("MiniMax 英语灾备未找到任何 en-US-subtitle 目录")

    stage_root = task_root / STAGE_DIR_NAME
    stage_root.mkdir(parents=True, exist_ok=True)
    _recover_interrupted_publish(task_root, stage_root)
    state = ResumeState(task_root)
    digest = stable_digest(
        "minimax-english-fallback-v1",
        str(fallback_reason),
        node_ids,
    )
    state.mark_step(FALLBACK_STATE_KEY, digest, status="running")

    logger.warning(
        f"🧯 ElevenLabs 系统性故障，启动 MiniMax 英语整线灾备：{fallback_reason}"
    )
    publish_transaction = None
    try:
        owner_check()
        mapping_path = stage_root / VOICE_MAPPING_FILENAME
        published_mapping_path = task_root / VOICE_MAPPING_FILENAME
        if (
            not refresh_voice_map
            and not mapping_path.exists()
            and published_mapping_path.is_file()
        ):
            shutil.copy2(published_mapping_path, mapping_path)
        voice_map = run_auto_casting(
            str(task_id),
            locales=(LOCALE,),
            task_output_dir=str(stage_root),
            output_filename=VOICE_MAPPING_FILENAME,
            persist_db=False,
            state_key="minimax_english_fallback_casting",
        ) if refresh_voice_map or not mapping_path.exists() else json.loads(
            mapping_path.read_text("utf-8")
        )

        generate_minimax_json(
            str(task_id),
            memory_voice_map=voice_map,
            locales=(LOCALE,),
            input_task_root=str(task_root),
            output_task_root=str(stage_root),
            mapping_filename=VOICE_MAPPING_FILENAME,
            use_db_mapping=False,
            state_key="minimax_english_fallback_json",
            audio_state_key="minimax_english_fallback_audio",
        )
        expected_paths = _expected_audio_paths(stage_root, task_root)
        _prepare_stage_audio_dirs(stage_root, node_ids, expected_paths)

        generate_minimax_audio(
            str(task_id),
            owner_check=owner_check,
            locales=(LOCALE,),
            input_task_root=str(stage_root),
            output_task_root=str(stage_root),
            state_key="minimax_english_fallback_audio",
        )
        _validate_complete_stage(stage_root, expected_paths)
        owner_check()
        publish_transaction = _publish_audio_directories(
            task_root, stage_root, node_ids, owner_check
        )
        _prepare_metadata_rollback(
            task_root, publish_transaction[1], publish_transaction[2]
        )
        atomic_write_json(task_root / VOICE_MAPPING_FILENAME, voice_map)
        manifest_path = _write_provider_manifest(
            task_root, expected_paths, fallback_reason
        )
        state.mark_step(
            FALLBACK_STATE_KEY,
            digest,
            status="success",
            artifacts=[
                str(task_root / VOICE_MAPPING_FILENAME), manifest_path,
                *[str(task_root / path) for path in sorted(expected_paths, key=str)],
            ],
        )
        _finish_audio_directory_publish(
            publish_transaction[1], publish_transaction[2]
        )
        publish_transaction = None
        shutil.rmtree(stage_root, ignore_errors=True)
        logger.info(
            f"✅ MiniMax 英语灾备完整发布：{len(expected_paths)} 个 WAV"
        )
        return {
            "provider": "minimax-fallback",
            "success": len(expected_paths),
            "failed": 0,
            "fallback_reason": str(fallback_reason),
        }
    except BaseException as exc:
        if publish_transaction is not None:
            _rollback_audio_directories(*publish_transaction)
        if type(exc).__name__ == "StageLeaseLostError":
            raise
        state.mark_step(
            FALLBACK_STATE_KEY, digest, status="failed", error=exc
        )
        logger.exception(
            "❌ MiniMax 英语灾备失败；正式英语音频保持不变，暂存目录保留：%s",
            stage_root,
        )
        raise
