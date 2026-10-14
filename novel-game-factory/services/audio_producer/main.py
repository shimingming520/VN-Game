import asyncio
from contextlib import nullcontext
import json
import os
import time
import sys
import traceback
from logger_config import setup_logger

logger = setup_logger("MainPipeline", "pipeline_main.log")

try:
    from extract_subtitles_from_db import (
        PlotsDataMissingError,
        process_subtitles as step2_subs,
    )
    from auto_cast_voices_from_db import run_auto_casting as step3_casting
    from generate_json_from_txt import main as step4_json
    from generate_audio_from_speech import (
        MiniMaxAuthenticationError,
        main as step5_audio,
    )
    from auto_cast_elevenlabs_voices import (
        run_auto_casting as step3_casting_relay,
    )
    from generate_elevenlabs_dialogue_json import (
        main as step4_dialogue_json_relay,
    )
    import importlib.util as _importlib_util

    _relay_spec = _importlib_util.spec_from_file_location(
        "generate_audio_from_elevenlabs_dialogue_relay",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "generate_audio_from_elevenlabs_dialogue.py",
        ),
    )
    _relay_module = _importlib_util.module_from_spec(_relay_spec)
    sys.modules[_relay_spec.name] = _relay_module
    _relay_spec.loader.exec_module(_relay_module)
    step5_audio_relay = _relay_module.main
    ElevenLabsAuthenticationError = _relay_module.ElevenLabsAuthenticationError
    ElevenLabsServiceUnavailableError = (
        _relay_module.ElevenLabsServiceUnavailableError
    )
except ImportError as e:
    logger.error(f"❌ 导入模块失败: {e}")
    sys.exit(1)

from common.config import OUTPUTS_PATH
from common.audio_languages import (
    AUDIO_OUTPUT_LOCALES,
    cleanup_disabled_local_audio_outputs,
    cleanup_orphan_audio_outputs,
)
from common.output_mirror import (
    cleanup_disabled_audio_outputs,
    delete_output_files,
)
from resume_state import (
    ResumeState,
    StepIncompleteError,
    file_digest,
    stable_digest,
    valid_wav,
)
from minimax_english_fallback import (
    has_minimax_english_fallback_history,
    is_current_minimax_english_fallback,
    record_primary_english_provider,
    run_minimax_english_fallback,
)

# 英语走 ElevenLabs 中转链，日语走 MiniMax 链，两边产物都落在 audios/{locale}-audio/。
RELAY_LOCALES = _relay_module.TARGET_LOCALES
MINIMAX_LOCALES = tuple(
    item.strip()
    for item in os.getenv("MINIMAX_LOCALES", "ja-JP").split(",")
    if item.strip()
)
ENGLISH_PROVIDER_MODE = os.getenv(
    "AUDIO_ENGLISH_PROVIDER_MODE", "ELEVENLABS_THEN_MINIMAX"
).strip().upper()
VALID_ENGLISH_PROVIDER_MODES = {
    "ELEVENLABS",
    "MINIMAX",
    "ELEVENLABS_THEN_MINIMAX",
}
MINIMAX_VOICE_MAPPING_FILENAME = "minimax_voice_mappings.json"


class AudioAssetContractError(StepIncompleteError):
    """有可执行输入但没有任何可验收音频。"""


class EnglishAudioContractError(AudioAssetContractError):
    """英语有可执行输入但没有任何可验收音频。"""


def _audio_asset_snapshot(task_id):
    """Return a language-level snapshot of subtitles, dialogue JSON and WAVs.

    A pipeline/worker status is not an output contract.  This snapshot is used
    both after a takeover and before OSS mirroring so an empty English line
    cannot be reported as a successful audio stage.
    """
    task_root = os.path.join(OUTPUTS_PATH, str(task_id))
    snapshot = {}
    for locale in AUDIO_OUTPUT_LOCALES:
        subtitle_files = []
        json_files = []
        dialogue_files = []
        audio_files = []
        if not os.path.isdir(task_root):
            snapshot[locale] = {
                "subtitles": 0, "dialogue_json": 0, "dialogue_inputs": 0,
                "json": 0, "json_inputs": 0, "audio": 0,
            }
            continue
        for root, _, files in os.walk(task_root):
            dirname = os.path.basename(root)
            if dirname == f"{locale}-subtitle":
                subtitle_files.extend(
                    os.path.join(root, name)
                    for name in files if name.endswith(".txt")
                )
            elif dirname == f"{locale}-json":
                json_files.extend(
                    os.path.join(root, name)
                    for name in files if name.endswith(".json")
                )
            elif dirname == f"{locale}-elevenlabs-dialogue-json":
                dialogue_files.extend(
                    os.path.join(root, name)
                    for name in files if name.endswith(".json")
                )
            elif dirname == f"{locale}-audio":
                audio_files.extend(
                    os.path.join(root, name)
                    for name in files if name.endswith(".wav")
                )
        nonempty_subtitles = []
        expected_subtitle_inputs = 0
        subtitle_parser = None
        pronounceable = None
        if locale == "en-US":
            try:
                from generate_json_from_txt import (
                    has_pronounceable_content as pronounceable,
                    parse_line_info as subtitle_parser,
                )
            except ImportError as exc:
                # 静默吞掉会让 expected_inputs 恒为 0，英语零产物就检测不出来。
                logger.warning(
                    f"⚠️ 可发音判据导入失败，英语输入计数将退化为 0：{exc}"
                )
        for path in subtitle_files:
            try:
                if os.path.getsize(path) > 0:
                    with open(path, encoding="utf-8") as file_obj:
                        raw_text = file_obj.read().strip()
                        if raw_text:
                            nonempty_subtitles.append(path)
                            if subtitle_parser and pronounceable:
                                valid, _, role, _, content = subtitle_parser(raw_text)
                                if (
                                    valid
                                    and str(role).strip().lower()
                                    not in {"system", "xitong"}
                                    and pronounceable(content)
                                ):
                                    expected_subtitle_inputs += 1
            except OSError:
                continue
        valid_audio = [path for path in audio_files if valid_wav(path)]
        dialogue_inputs = 0
        json_inputs = 0
        for path in (*json_files, *dialogue_files):
            try:
                with open(path, encoding="utf-8") as file_obj:
                    payload = json.load(file_obj)
                count = len(payload.get("inputs") or [])
                if path in dialogue_files:
                    dialogue_inputs += count
                json_inputs += count
            except (OSError, ValueError, TypeError):
                continue
        snapshot[locale] = {
            "subtitles": len(nonempty_subtitles),
            "expected_inputs": expected_subtitle_inputs,
            "dialogue_json": len(dialogue_files),
            "dialogue_inputs": dialogue_inputs,
            "json": len(json_files),
            "json_inputs": json_inputs,
            "audio": len(valid_audio),
        }
    return snapshot


def _validate_audio_asset_snapshot(task_id, snapshot=None):
    """Fail only when a language has expected input but zero valid output."""
    snapshot = snapshot or _audio_asset_snapshot(task_id)
    failures = []
    for locale, values in snapshot.items():
        expected = (
            values.get("expected_inputs", 0) > 0
            or values.get("dialogue_inputs", 0) > 0
            or values.get("json_inputs", 0) > 0
            or values.get("audio", 0) > 0
        )
        if expected and values.get("audio", 0) == 0:
            failures.append(
                f"{locale}: 字幕={values['subtitles']}, "
                f"Dialogue JSON={values['dialogue_json']}, "
                f"输入台词={values['dialogue_inputs']}, 有效 WAV=0"
            )
    if failures:
        raise AudioAssetContractError(
            "音频产物验收失败（有输入但没有有效 WAV）：" + "；".join(failures)
        )
    return snapshot


def _english_audio_contract_error(snapshot):
    values = (snapshot or {}).get("en-US") or {}
    # 仅有字幕不代表一定需要配音：系统文本、空台词和纯标点会被合法过滤。
    # Dialogue JSON 的 inputs 才是 Step 4 明确判定为可执行的英语输入。
    expected = max(
        values.get("expected_inputs", 0),
        values.get("dialogue_inputs", 0),
        values.get("json_inputs", 0),
    )
    if expected <= 0:
        return None
    if values.get("audio", 0) > 0:
        return None
    return EnglishAudioContractError(
        "en-US 有英语字幕但没有任何有效 WAV："
        f"字幕={values.get('subtitles', 0)}, "
        f"Dialogue JSON={values.get('dialogue_json', 0)}, "
        f"输入台词={values.get('dialogue_inputs', 0)}"
    )


_STEP_STATE_KEY_OWNERS = {}


def _step_state_key(step_name):
    """步骤名转断点状态键。

    转换会丢掉大小写和标点，所以两个只差标点的步骤名会撞成同一个键、互相覆盖
    状态。键格式不能改（线上断点状态依赖它），因此这里只做冲突检测并告警，
    让新增步骤时立刻暴露，而不是静默串状态。
    """
    key = "execution_" + "_".join(
        part.lower() for part in step_name.replace("：", " ").split()
    )
    owner = _STEP_STATE_KEY_OWNERS.setdefault(key, step_name)
    if owner != step_name:
        logger.error(
            f"❌ 断点状态键冲突：{step_name!r} 与 {owner!r} 都映射到 {key!r}，"
            "两者的执行状态会互相覆盖，请改用可区分的步骤名"
        )
    return key

try:
    from common.usage_tracker import tracker, init_usage_table
    HAS_TRACKER = True
except ImportError:
    HAS_TRACKER = False


def _run_relay_chain(current_task_id, owner_check):
    # Step 3/4 也会重建/删除共享字幕与批次 JSON，必须和 Step 5 共用同一
    # task 级锁；否则接管 Worker 可能在旧 Worker 的 Step 4/5 之间读到半成品。
    with _relay_module.stage_lock(current_task_id, owner_check):
        owner_check()
        relay_voice_map = _run_required_step(
            "ElevenLabs Step 3 选角",
            step3_casting_relay,
            current_task_id,
        )
        owner_check()
        _run_required_step(
            "ElevenLabs Step 4 对话 JSON",
            step4_dialogue_json_relay,
            current_task_id,
            memory_voice_map=relay_voice_map,
        )
        owner_check()
        audio_result = _run_required_step(
            "ElevenLabs Step 5 音频",
            step5_audio_relay,
            current_task_id,
            owner_check=owner_check,
        )
        owner_check()
        return {"audio": audio_result}


def _run_minimax_chain(current_task_id, owner_check):
    owner_check()
    voice_map_dict = _run_required_step(
        "MiniMax Step 3 选角",
        step3_casting,
        current_task_id,
    )
    owner_check()
    _run_required_step(
        "MiniMax Step 4 JSON",
        step4_json,
        current_task_id,
        memory_voice_map=voice_map_dict,
        mapping_filename=MINIMAX_VOICE_MAPPING_FILENAME,
    )
    owner_check()
    audio_result = _run_required_step(
        "MiniMax Step 5 音频",
        step5_audio,
        current_task_id,
        owner_check=owner_check,
    )
    owner_check()
    return {"audio": audio_result}


def _exception_chain(exc):
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_elevenlabs_system_failure(exc):
    """供应商级或零产物契约故障触发整线灾备；普通内容质量问题留在原链。"""
    return any(
        isinstance(
            current,
            (
                ElevenLabsAuthenticationError,
                ElevenLabsServiceUnavailableError,
                EnglishAudioContractError,
            ),
        )
        for current in _exception_chain(exc)
    )


def _run_minimax_english_fallback_chain(
    current_task_id, owner_check, relay_error
):
    return _run_required_step(
        "MiniMax 英语灾备",
        run_minimax_english_fallback,
        current_task_id,
        fallback_reason=f"{type(relay_error).__name__}: {relay_error}",
        owner_check=owner_check,
    )


def _is_control_flow_exception(exc):
    return isinstance(exc, (KeyboardInterrupt, SystemExit)) or (
        type(exc).__name__ == "StageLeaseLostError"
    )


def _step_result_payload(result):
    """取出步骤结果本体，兼容 Step 5 返回的 {audio: ...} 包装。"""
    if isinstance(result, dict) and isinstance(result.get("audio"), dict):
        return result["audio"]
    return result if isinstance(result, dict) else {}


def _run_required_step(step_name, function, *args, **kwargs):
    """执行步骤；明确 partial 可继续，但 failed/error 仍然抛错。"""
    task_id = args[0] if args else None
    task_root = os.path.join(OUTPUTS_PATH, str(task_id)) if task_id else None
    state_key = _step_state_key(step_name)
    execution_digest = stable_digest(
        os.getenv("AUDIO_PRODUCER_EXECUTION_ID"),
        os.getenv("AUDIO_PRODUCER_ATTEMPT_ID"),
        step_name,
        getattr(function, "__module__", ""),
        getattr(function, "__name__", ""),
    )
    if task_root:
        ResumeState(task_root).mark_step(
            state_key, execution_digest, status="running"
        )
    try:
        result = function(*args, **kwargs)
    except BaseException as exc:
        if _is_control_flow_exception(exc):
            raise
        if task_root:
            ResumeState(task_root).mark_step(
                state_key, execution_digest, status="failed", error=exc
            )
        raise StepIncompleteError(
            f"{step_name}: {type(exc).__name__}: {exc}"
        ) from exc

    if result is None:
        raise StepIncompleteError(f"{step_name}: 未返回明确成功结果")
    result_status = {}
    if isinstance(result, dict):
        # relay_audio 的结果嵌在 Step 5 的 {"audio": ...} 中。
        result_status = (
            result.get("audio")
            if isinstance(result.get("audio"), dict)
            else result
        )
        failed = int(result_status.get("failed") or 0)
        error = result_status.get("error")
        skipped_reason = result_status.get("skipped_reason")
        if failed or error or skipped_reason:
            reason = error or skipped_reason or f"仍有 {failed} 个失败资产"
            if task_root:
                ResumeState(task_root).mark_step(
                    state_key, execution_digest,
                    status="failed", error=reason,
                )
            raise StepIncompleteError(f"{step_name}: {reason}")
    if task_root:
        ResumeState(task_root).mark_step(
            state_key, execution_digest,
            status="partial" if result_status.get("partial") else "success",
            error=(
                f"跳过 {result_status.get('skipped', 0)} 个失败资产"
                if result_status.get("partial") else None
            ),
        )
    return result


def _safe_tracker_call(operation_name, function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception as exc:
        logger.warning(f"⚠️ 用量跟踪 {operation_name} 失败，不影响音频流程：{exc}")
        return None


async def run_pipeline(manual_task_id=None, owner_check=None):
    """
    全流程调度函数
    """
    pipeline_start = time.time()
    owner_check = owner_check or (lambda: None)
    if ENGLISH_PROVIDER_MODE not in VALID_ENGLISH_PROVIDER_MODES:
        raise ValueError(
            "AUDIO_ENGLISH_PROVIDER_MODE 必须是 "
            + ", ".join(sorted(VALID_ENGLISH_PROVIDER_MODES))
        )

    logger.info("#" * 50)
    logger.info("🚀 [Pipeline] 启动音频生成自动化生产 (Enterprise Mode)")
    logger.info(f"🌐 [Output] 本次仅生产: {', '.join(AUDIO_OUTPUT_LOCALES)}")
    logger.info(f"🎙️ [English] 供应策略: {ENGLISH_PROVIDER_MODE}")
    logger.info(
        "🧭 [Execution] execution_id=%s attempt_id=%s OUTPUTS_PATH=%s "
        "RELAY_LOCALES=%s MINIMAX_LOCALES=%s",
        os.getenv("AUDIO_PRODUCER_EXECUTION_ID") or "-",
        os.getenv("AUDIO_PRODUCER_ATTEMPT_ID") or "-",
        OUTPUTS_PATH,
        ",".join(RELAY_LOCALES) or "-",
        ",".join(MINIMAX_LOCALES) or "-",
    )
    if manual_task_id:
        logger.info(f"🔧 [Mode] 指定任务模式: {manual_task_id}")
    else:
        logger.info(f"🆕 [Mode] 全新生成模式")
    logger.info("#" * 50)

    if HAS_TRACKER and manual_task_id:
        _safe_tracker_call("init", init_usage_table)
        _safe_tracker_call(
            "set_context",
            tracker.set_context,
            manual_task_id, "audio_producer", force_new_run=True
        )

    logger.info("🔹 Step 2: 生成字幕文件 (Extract Subtitles)")
    step2_execution_digest = stable_digest(
        os.getenv("AUDIO_PRODUCER_EXECUTION_ID"),
        os.getenv("AUDIO_PRODUCER_ATTEMPT_ID"),
        "step2",
    )
    if manual_task_id:
        ResumeState(os.path.join(OUTPUTS_PATH, str(manual_task_id))).mark_step(
            "execution_step2_subtitles",
            step2_execution_digest,
            status="running",
        )
    try:
        # 服务器按原 task_id 接管时，Step 2 也会重建/删除字幕及 JSON；
        # 必须先取得 ElevenLabs 任务锁，不能在旧 Worker 的 Step 5 期间改写输入。
        task_root_exists = bool(
            manual_task_id
            and os.path.isdir(os.path.join(OUTPUTS_PATH, str(manual_task_id)))
        )
        step2_lock = (
            _relay_module.stage_lock(manual_task_id, owner_check)
            if task_root_exists else nullcontext()
        )
        with step2_lock:
            owner_check()
            current_task_id = step2_subs(manual_task_id)

            if not current_task_id:
                raise PlotsDataMissingError(
                    "Step 2 未获取到 Task ID，plots 前置数据不存在"
                )
            owner_check()
    except PlotsDataMissingError as e:
        if manual_task_id:
            ResumeState(os.path.join(OUTPUTS_PATH, str(manual_task_id))).mark_step(
                "execution_step2_subtitles", step2_execution_digest,
                status="failed", error=e,
            )
        logger.error(f"❌ [Critical] Step 2 失败: {e}")
        raise
    except Exception as e:
        if manual_task_id:
            ResumeState(os.path.join(OUTPUTS_PATH, str(manual_task_id))).mark_step(
                "execution_step2_subtitles", step2_execution_digest,
                status="failed", error=e,
            )
        logger.exception(f"❌ Step 2 失败: {e}")
        raise

    if not current_task_id:
        raise PlotsDataMissingError("Step 2 未获得 task_id，无法确定 plots 数据")

    logger.info(f"🌟 [Context] 当前流水线 Task ID: {current_task_id}")
    # Step 2 允许带硬校验报告的 partial 结果继续下游；不要在这里把
    # extract_subtitles_from_db 已记录的 partial 状态覆盖成 success。
    subtitle_state = ResumeState(
        os.path.join(OUTPUTS_PATH, str(current_task_id))
    ).get_step("subtitles")
    subtitle_partial = subtitle_state.get("status") == "partial"
    ResumeState(os.path.join(OUTPUTS_PATH, str(current_task_id))).mark_step(
        "execution_step2_subtitles",
        step2_execution_digest,
        status="partial" if subtitle_partial else "success",
        error=subtitle_state.get("error") if subtitle_partial else None,
    )
    if subtitle_partial:
        logger.warning(
            "⚠️ Step 2 带硬校验报告继续下游：%s",
            subtitle_state.get("error") or "存在未修复字幕",
        )
    if HAS_TRACKER:
        _safe_tracker_call("init", init_usage_table)
        _safe_tracker_call(
            "set_context", tracker.set_context, current_task_id, "audio_producer"
        )

    forced_minimax_english = ENGLISH_PROVIDER_MODE == "MINIMAX"
    sticky_minimax_fallback = (
        ENGLISH_PROVIDER_MODE == "ELEVENLABS_THEN_MINIMAX"
        and is_current_minimax_english_fallback(current_task_id)
    )
    rebuild_sticky_minimax_fallback = (
        ENGLISH_PROVIDER_MODE == "ELEVENLABS_THEN_MINIMAX"
        and not sticky_minimax_fallback
        and has_minimax_english_fallback_history(current_task_id)
    )
    run_direct_minimax_english = (
        forced_minimax_english or rebuild_sticky_minimax_fallback
    )
    if run_direct_minimax_english:
        logger.info(
            "🔹 Step 3-5 先运行 MiniMax 日语主链，完成后串行运行英语整线"
        )
        chain_specs = (("MiniMax", _run_minimax_chain),)
    elif sticky_minimax_fallback:
        logger.info(
            "♻️ 当前字幕已有完整 MiniMax 英语灾备产物；"
            "本次重跑保持供应商一致，不再混入 ElevenLabs"
        )
        chain_specs = (("MiniMax", _run_minimax_chain),)
    else:
        logger.info(
            "🔹 Step 3-5 并行启动："
            f"ElevenLabs ({', '.join(RELAY_LOCALES)}) + "
            f"MiniMax ({', '.join(MINIMAX_LOCALES)})"
        )
        chain_specs = (
            ("ElevenLabs", _run_relay_chain),
            ("MiniMax", _run_minimax_chain),
        )
    chain_results = await asyncio.gather(
        *(
            asyncio.to_thread(chain_func, current_task_id, owner_check)
            for _, chain_func in chain_specs
        ),
        return_exceptions=True,
    )

    chain_errors = []
    partial_chain_notes = []
    if subtitle_partial:
        partial_chain_notes.append(
            "Step 2 字幕部分完成：存在未通过硬校验的条目，已写报告并继续处理全部条目"
        )
    relay_error = None
    for (chain_name, _), result in zip(chain_specs, chain_results):
        if not isinstance(result, BaseException):
            result_payload = _step_result_payload(result)
            if result_payload.get("partial"):
                note = (
                    f"{chain_name} 部分完成：成功 {result_payload.get('success', 0)}，"
                    f"跳过 {result_payload.get('skipped', 0)} 个批次"
                )
                partial_chain_notes.append(note)
                logger.warning(f"⚠️ {chain_name} 链{note}；继续后续流程")
            else:
                logger.info(f"✅ {chain_name} 链完成: {result}")
            continue
        if _is_control_flow_exception(result):
            raise result
        if chain_name == "ElevenLabs":
            relay_error = result
        else:
            chain_errors.append(f"{chain_name}: {result}")
        logger.error(f"❌ {chain_name} 链失败: {result}；保留已有成功产物")
        logger.error(
            "".join(
                traceback.format_exception(
                    type(result), result, result.__traceback__
                )
            )
        )

    # 接管 Worker 或 Step 5 空扫描时，链函数可能没有抛异常但英语没有任何
    # 成品。把它提升为可识别的系统性契约故障，触发 MiniMax 英语灾备，避免
    # 日语成功掩盖英语零产物。
    english_contract_error = None
    try:
        asset_snapshot = _audio_asset_snapshot(current_task_id)
        contract_error = _english_audio_contract_error(asset_snapshot)
        if contract_error is not None:
            english_contract_error = contract_error
            if relay_error is None:
                relay_error = contract_error
            logger.error("❌ 英语音频产物验收失败：%s", contract_error)
        else:
            logger.info("📊 音频产物快照：%s", asset_snapshot)
    except Exception as snapshot_error:
        logger.warning("⚠️ 音频产物快照检查失败：%s", snapshot_error)

    if run_direct_minimax_english:
        try:
            direct_reason = (
                "AUDIO_ENGLISH_PROVIDER_MODE=MINIMAX"
                if forced_minimax_english
                else "existing MiniMax fallback inputs or implementation changed"
            )
            fallback_result = await asyncio.to_thread(
                _run_required_step,
                "MiniMax 英语灾备",
                run_minimax_english_fallback,
                current_task_id,
                fallback_reason=direct_reason,
                owner_check=owner_check,
            )
            logger.info(f"✅ MiniMax 英语整线完成: {fallback_result}")
        except BaseException as fallback_error:
            if _is_control_flow_exception(fallback_error):
                raise fallback_error
            chain_errors.append(f"MiniMaxEnglish: {fallback_error}")
            logger.error(f"❌ MiniMax 英语整线失败: {fallback_error}")

    if relay_error is not None:
        if (
            ENGLISH_PROVIDER_MODE == "ELEVENLABS_THEN_MINIMAX"
            and (
                _is_elevenlabs_system_failure(relay_error)
                or english_contract_error is not None
            )
        ):
            logger.warning(
                "🧯 ElevenLabs 链发生供应商或英语产物契约故障；"
                "开始串行运行 MiniMax 英语完整重建"
            )
            try:
                fallback_result = await asyncio.to_thread(
                    _run_minimax_english_fallback_chain,
                    current_task_id,
                    owner_check,
                    relay_error,
                )
                logger.info(f"✅ MiniMax 英语灾备链完成: {fallback_result}")
            except BaseException as fallback_error:
                if _is_control_flow_exception(fallback_error):
                    raise fallback_error
                chain_errors.append(
                    "ElevenLabs 系统故障且 MiniMax 英语灾备失败："
                    f"{fallback_error}"
                )
                logger.error(
                    f"❌ MiniMax 英语灾备失败: {fallback_error}；"
                    "正式英语音频保持原样"
                )
                logger.error(
                    "".join(
                        traceback.format_exception(
                            type(fallback_error),
                            fallback_error,
                            fallback_error.__traceback__,
                        )
                    )
                )
        else:
            chain_errors.append(f"ElevenLabs: {relay_error}")
            if ENGLISH_PROVIDER_MODE == "ELEVENLABS_THEN_MINIMAX":
                logger.error(
                    "🚫 ElevenLabs 失败不属于鉴权/服务级故障，"
                    "不会启动整线灾备，以免内容或质量问题导致换音色"
                )

    task_root = os.path.join(OUTPUTS_PATH, str(current_task_id))
    try:
        removed_local = cleanup_disabled_local_audio_outputs(task_root)
        if removed_local:
            logger.info(f"🧹 已清理 {len(removed_local)} 个本地中文历史音频产物目录")
        cleanup_disabled_audio_outputs(str(current_task_id))
    except Exception as exc:
        logger.warning(f"⚠️ 历史产物清理失败，不影响流程完成：{exc}")

    # 清理前先记账：孤儿清理是不可回滚的删除，一旦把某语言清成零产物，后面的
    # 验收只能报错，文件却已经没了。所以先留一份清理前的快照做对照。
    pre_cleanup_snapshot = _audio_asset_snapshot(current_task_id)

    # 剧情改动后被删掉的台词不会再有字幕，但上一轮的 WAV 仍留在成品目录里，
    # 不清掉会被 game_builder 打包进去。只删本语言字幕清单外的 WAV。
    for locale in AUDIO_OUTPUT_LOCALES:
        try:
            orphans = cleanup_orphan_audio_outputs(task_root, locale)
            if not orphans:
                continue
            logger.info(
                f"🧹 已删除 {len(orphans)} 个无对应字幕的 {locale} 孤儿音频"
            )
            delete_output_files(str(current_task_id), orphans)
            # 清理把本来有音频的语言清成了零，说明字幕清单与 WAV 命名已经不一致
            # （上游改名/漏产字幕），而不是"台词被删"。这属于判据失效，必须显式
            # 报出来，否则下一步验收只会看到零产物却不知道是被清掉的。
            after = _audio_asset_snapshot(current_task_id)
            before_count = pre_cleanup_snapshot.get(locale, {}).get("audio", 0)
            after_count = after.get(locale, {}).get("audio", 0)
            if before_count > 0 and after_count == 0:
                chain_errors.append(
                    f"{locale} 孤儿清理把全部 {before_count} 个音频删空，"
                    "字幕清单与 WAV 命名不一致，需人工核对"
                )
                logger.error(
                    f"❌ {locale} 孤儿清理后音频归零（清理前 {before_count} 个）；"
                    "已记为失败，不再据此写供应商清单"
                )
        except Exception as exc:
            logger.warning(
                f"⚠️ {locale} 孤儿音频清理失败，不影响流程完成：{exc}"
            )

    # 清理完成后再次做英语硬验收；只有真实 WAV 存在时才记录
    # ElevenLabs 主供应商，避免空资产生成“成功” provider manifest。
    final_snapshot = _audio_asset_snapshot(current_task_id)
    final_contract_error = _english_audio_contract_error(final_snapshot)
    if final_contract_error is not None:
        # 区分"从未产出"和"产出后被清理删空"：前者要重跑配音，后者是字幕/WAV
        # 命名不一致，重跑配音也会再被删一次，必须人工核对。
        if pre_cleanup_snapshot.get("en-US", {}).get("audio", 0) > 0:
            chain_errors.append(
                "ElevenLabs: 英语音频在孤儿清理前存在、清理后归零，"
                f"疑似字幕清单与 WAV 命名不一致（{final_contract_error}）"
            )
        else:
            chain_errors.append(f"ElevenLabs: {final_contract_error}")
    elif (
        not run_direct_minimax_english
        and not sticky_minimax_fallback
        and not partial_chain_notes
        and relay_error is None
        and final_snapshot.get("en-US", {}).get("audio", 0) > 0
    ):
        try:
            record_primary_english_provider(current_task_id)
        except Exception as exc:
            logger.warning(
                f"⚠️ ElevenLabs provider 清单写入失败，不改变音频结果：{exc}"
            )

    if chain_errors:
        ResumeState(task_root).mark_step(
            "pipeline",
            stable_digest(
                "audio-producer-pipeline-v3", AUDIO_OUTPUT_LOCALES,
                ENGLISH_PROVIDER_MODE,
                file_digest(__file__),
            ),
            status="failed",
            error="；".join(chain_errors),
        )
        if HAS_TRACKER:
            _safe_tracker_call("flush", tracker.flush)
        raise StepIncompleteError(
            "audio_producer 未完整完成；" + "；".join(chain_errors)
        )

    duration = time.time() - pipeline_start
    ResumeState(task_root).mark_step(
        "pipeline",
        stable_digest(
            "audio-producer-pipeline-v3", AUDIO_OUTPUT_LOCALES,
            ENGLISH_PROVIDER_MODE,
            file_digest(__file__),
        ),
        status="partial" if partial_chain_notes else "success",
        error="；".join(partial_chain_notes) if partial_chain_notes else None,
    )
    logger.info("#" * 50)
    if partial_chain_notes:
        logger.warning(
            "⚠️ [Partial] 全流程继续完成，但存在跳过批次：%s",
            "；".join(partial_chain_notes),
        )
    else:
        logger.info(f"🎉 [Success] 全流程执行完毕！")
    logger.info(f"⏱️  总耗时: {duration:.2f} 秒")
    logger.info(f"📂 产物路径: ../outputs/{current_task_id}/")
    logger.info("#" * 50)

    if HAS_TRACKER:
        _safe_tracker_call("flush", tracker.flush)
    return current_task_id


if __name__ == "__main__":
    MANUAL_TASK_ID = ""
    try:
        asyncio.run(run_pipeline(MANUAL_TASK_ID))
    except KeyboardInterrupt:
        logger.warning("\n🛑 手动中止流程。")
        if HAS_TRACKER:
            tracker.flush()
    except Exception as e:
        logger.critical(f"💀 [CRITICAL] 发生未捕获异常: {e}")
        logger.error(traceback.format_exc())
        if HAS_TRACKER:
            tracker.flush()
