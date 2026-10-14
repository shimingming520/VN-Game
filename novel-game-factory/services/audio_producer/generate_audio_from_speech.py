import sys
import os
import time
import random
import threading
import requests
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from logger_config import setup_logger

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import OUTPUTS_PATH
from common.audio_languages import AUDIO_LANGUAGE_BOOST_MAP
from resume_state import ResumeState, StepIncompleteError, stable_digest, valid_wav

try:
    from common.usage_tracker import tracker as _usage_tracker
except ImportError:
    _usage_tracker = None


def count_tts_chars(text: str) -> int:
    count = 0
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or
                0x3400 <= cp <= 0x4DBF or
                0x20000 <= cp <= 0x2A6DF or
                0x2A700 <= cp <= 0x2CEAF or
                0xF900 <= cp <= 0xFAFF):
            count += 2
        else:
            count += 1
    return count


load_dotenv()

# MiniMax API Key
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY")
MINIMAX_URL = os.getenv("MINIMAX_URL", "http://sefutech-api.kube-test.svc:3000/v1/audio/speech")

logger = setup_logger("MiniMax_Audio", "step5_minimax_audio.log")

if not MINIMAX_API_KEY:
    logger.error("❌ 错误: 未设置 MINIMAX_API_KEY 环境变量")

# 统一使用全局配置的路径
INPUT_ROOT = OUTPUTS_PATH
OUTPUT_ROOT = OUTPUTS_PATH

# 并发配置
MAX_WORKERS = 10
MAX_ITEM_RETRIES = max(1, int(os.getenv("MINIMAX_TTS_MAX_RETRIES", "3")))
MAX_RETRIES = MAX_ITEM_RETRIES

RATE_LIMIT_TAG = "RATE_LIMIT"


class MiniMaxAuthenticationError(RuntimeError):
    """MiniMax API Key 缺失、无效或无权访问时触发的不可恢复错误。"""


AUTH_ERROR_CODES = {1004}
AUTH_ERROR_KEYWORDS = (
    "invalid api key",
    "invalid_api_key",
    "invalidapikey",
    "api key invalid",
    "api key is invalid",
    "unauthorized",
    "authentication failed",
    "authorization failed",
    "鉴权失败",
    "认证失败",
    "密钥无效",
    "无效的 api key",
)

_auth_failure_event = threading.Event()
_auth_failure_lock = threading.Lock()
_auth_failure_message = ""

# lang_key -> MiniMax language_boost 映射
LANGUAGE_BOOST_MAP = AUDIO_LANGUAGE_BOOST_MAP
# MiniMax 链只负责日语；英语由 ElevenLabs 中转链合成。
MINIMAX_LOCALES = tuple(
    item.strip()
    for item in os.getenv("MINIMAX_LOCALES", "ja-JP").split(",")
    if item.strip()
)
# ================= 核心请求函数 =================

def _is_auth_error(status_code=None, message=""):
    normalized_code = str(status_code).strip() if status_code is not None else ""
    if normalized_code in {"401", "403", *(str(code) for code in AUTH_ERROR_CODES)}:
        return True
    normalized_message = str(message or "").lower()
    return any(keyword in normalized_message for keyword in AUTH_ERROR_KEYWORDS)


def _raise_auth_failure(message):
    global _auth_failure_message
    with _auth_failure_lock:
        if not _auth_failure_message:
            _auth_failure_message = str(message)
        final_message = _auth_failure_message
        _auth_failure_event.set()
    raise MiniMaxAuthenticationError(final_message)


def _check_auth_failure():
    if _auth_failure_event.is_set():
        _raise_auth_failure(_auth_failure_message or "MiniMax API Key 鉴权失败")


def _reset_auth_failure():
    global _auth_failure_message
    with _auth_failure_lock:
        _auth_failure_message = ""
        _auth_failure_event.clear()


def call_minimax_api(
    payload, lang_key=None, *, owner_check=None, idempotency_key=None
):
    """执行 MiniMax API 请求"""
    if owner_check:
        owner_check()
    _check_auth_failure()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {MINIMAX_API_KEY}"
    }
    if idempotency_key:
        headers["Idempotency-Key"] = str(idempotency_key)

    # new-api 的 MiniMax TTS 适配器使用 OpenAI /v1/audio/speech 入口，
    # 通过 metadata 保留 MiniMax 原生音频配置。
    voice_id = payload["voice_id"]
    speed = payload["speed"]
    vol = payload["vol"]
    emotion = payload["emotion"]
    full_payload = {
        "model": "speech-2.8-hd",
        "input": payload["text"],
        "voice": voice_id,
        # MiniMax output_format 只能是 hex/url；new-api 会再转成二进制音频响应。
        "response_format": "hex",
        "speed": speed,
        "metadata": {
            "voice_setting": {
                "voice_id": voice_id,
                "speed": speed,
                "vol": vol,
                "pitch": 0,
                "emotion": emotion,
                "text_normalization": True
            },
            "audio_setting": {
                "sample_rate": 44100,
                "format": "wav",
                "channel": 1
            },
            "output_format": "hex",
            "subtitle_enable": False
        }
    }

    # 注入语言增强，并由中转服务透传给 MiniMax。
    boost = LANGUAGE_BOOST_MAP.get(lang_key)
    if boost:
        full_payload["metadata"]["language_boost"] = boost

    try:
        response = requests.post(MINIMAX_URL, json=full_payload, headers=headers, timeout=60)

        content_type = response.headers.get("Content-Type", "").lower()
        response_message = "" if content_type.startswith("audio/") else response.text

        if _is_auth_error(response.status_code, response_message):
            _raise_auth_failure(
                f"MiniMax API Key 鉴权失败: HTTP {response.status_code}: {response_message}"
            )

        if response.status_code in (429, 503):
            return False, None, f"{RATE_LIMIT_TAG}: HTTP {response.status_code}"

        if response.status_code == 200:
            if content_type.startswith("audio/") and response.content:
                return True, response.content, ""

            res_json = response.json()
            # 检查业务状态码
            if res_json.get("base_resp", {}).get("status_code") == 0:
                # 获取 hex 编码音频
                hex_audio = res_json.get("data", {}).get("audio")
                if hex_audio:
                    try:
                        audio_bytes = bytes.fromhex(hex_audio)
                    except ValueError as decode_err:
                        return False, None, f"Hex decode error: {decode_err}"
                    return True, audio_bytes, ""
                return False, None, "No audio data in response"
            else:
                msg = res_json.get("base_resp", {}).get("status_msg", "Unknown Error")
                code = res_json.get("base_resp", {}).get("status_code")
                if _is_auth_error(code, msg):
                    _raise_auth_failure(
                        f"MiniMax API Key 鉴权失败: Business Error {code}: {msg}"
                    )
                return False, None, f"Business Error {code}: {msg}"
        else:
            return False, None, f"HTTP Error {response.status_code}: {response.text}"
    except MiniMaxAuthenticationError:
        raise
    except Exception as e:
        return False, None, f"Request Exception: {str(e)}"


# ================= 单个任务处理 =================

def process_one_minimax_task(task_info):
    _check_auth_failure()
    (
        index, total, src_path, task_id, node_id, batch_name, lang_key,
        owner_check, *runtime_options,
    ) = task_info
    output_task_root = (
        runtime_options[0]
        if runtime_options
        else os.path.join(OUTPUT_ROOT, str(task_id))
    )
    audio_state_key = runtime_options[1] if len(runtime_options) > 1 else "minimax_audio"
    file_name = os.path.basename(src_path)
    audio_name = os.path.splitext(file_name)[0] + ".wav"

    target_dir = os.path.join(
        output_task_root, node_id, "audios", f"{lang_key}-audio"
    )
    os.makedirs(target_dir, exist_ok=True)
    dst_path = os.path.join(target_dir, audio_name)

    # 1. 读取 JSON 配置并核对输入摘要；只凭 WAV 存在绝不跳过。
    try:
        with open(src_path, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    except Exception as e:
        return f"❌ [{batch_name}] 读取 JSON 失败: {file_name} -> {e}"

    input_digest = payload.get("_resume_input_digest")
    item_key = payload.get("_resume_item_key") or (
        f"{node_id}/{lang_key}/{os.path.splitext(file_name)[0]}"
    )
    state = ResumeState(output_task_root)
    if (
        input_digest
        and state.item_matches(audio_state_key, item_key, input_digest)
        and valid_wav(dst_path)
    ):
        os.remove(src_path)
        return f"⏭️ [{batch_name}][{index}/{total}] 已验收: {node_id}/{audio_name}"

    # 2. 单资产有限重试；耗尽后保留 JSON 给下一 execution/attempt。
    for attempt in range(1, MAX_ITEM_RETRIES + 1):
        _check_auth_failure()
        success, audio_data, err_msg = call_minimax_api(
            payload,
            lang_key,
            owner_check=owner_check,
            idempotency_key=stable_digest(
                task_id, "audio_producer", item_key, "minimax-speech", attempt,
            ),
        )

        if success:
            if _usage_tracker:
                try:
                    _usage_tracker.add_tts(
                        count_tts_chars(payload.get("text", "")),
                        "speech-2.8-hd",
                        provider="minimax",
                    )
                except Exception as exc:
                    logger.warning(f"⚠️ TTS 用量记录失败，音频仍按成功处理：{exc}")
            temp_path = f"{dst_path}.{os.getpid()}.{threading.get_ident()}.building"
            try:
                with open(temp_path, 'wb') as f:
                    f.write(audio_data)
                if not valid_wav(temp_path):
                    raise RuntimeError(f"MiniMax 返回的 WAV 不完整或无法解析：{audio_name}")
                owner_check()
                os.replace(temp_path, dst_path)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            # 成功后删除 JSON 配置文件
            if os.path.exists(src_path):
                owner_check()
                os.remove(src_path)
            if input_digest:
                state.mark_item(
                    audio_state_key, item_key, input_digest,
                    status="success", artifacts=[dst_path],
                )
            return f"✅ [{batch_name}][{index}/{total}] 成功: {node_id}/{audio_name}"

        # 错误分类处理
        # 修复 #6: 同时识别业务层限流码 (1002/1039) 和 HTTP 层 429/503 (RATE_LIMIT_TAG)
        if (RATE_LIMIT_TAG in err_msg
                or "限流" in err_msg
                or "1002" in err_msg
                or "1039" in err_msg):
            wait_time = random.uniform(2, 4) * attempt
            logger.warning(f"⏳ 限流避让 {wait_time:.1f}s: {audio_name} ({err_msg})")
            time.sleep(wait_time)
            continue

        logger.warning(f"⚠️ 尝试 {attempt} 失败: {audio_name} -> {err_msg}")
        time.sleep(1)

    return f"❌ [{batch_name}][{index}/{total}] 失败: {node_id}/{audio_name}"


# ================= 辅助函数 =================

def scan_json_files(scan_root, target_task_id, locales=None):
    tasks = []
    if not os.path.exists(scan_root):
        return tasks
    requested_locales = set(locales or MINIMAX_LOCALES)
    json_dirs = {f"{locale}-json" for locale in requested_locales}
    for root, dirs, files in os.walk(scan_root):
        dir_name = os.path.basename(root)
        if dir_name not in json_dirs:
            continue
        lang_key = dir_name[:-5]  # strip "-json"
        try:
            node_id = Path(root).parent.name
        except:
            node_id = "UnknownNode"
        for file in files:
            if file.endswith(".json"):
                tasks.append((os.path.join(root, file), target_task_id, node_id, lang_key))
    return tasks


def run_batch(
    file_list,
    target_task_id,
    batch_name,
    owner_check=None,
    *,
    output_task_root=None,
    state_key="minimax_audio",
):
    total = len(file_list)
    if total == 0: return 0
    owner_check = owner_check or (lambda: None)

    logger.info(f"\n🚀 启动 MiniMax 批次: {batch_name} | 任务数: {total}")
    start_time = time.time()

    task_args = [(
        i, total, path, tid, node, batch_name, lang, owner_check,
        output_task_root or os.path.join(OUTPUT_ROOT, str(target_task_id)),
        state_key,
    )
                 for i, (path, tid, node, lang) in enumerate(file_list, 1)]

    success_count = 0
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        futures = [executor.submit(process_one_minimax_task, arg) for arg in task_args]
        for future in as_completed(futures):
            try:
                res = future.result()
            except MiniMaxAuthenticationError:
                for pending in futures:
                    pending.cancel()
                logger.critical("💀 MiniMax API Key 鉴权失败，立即终止音频生成。")
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            logger.info(res)
            if "✅" in res or "⏭️" in res:
                success_count += 1
    except MiniMaxAuthenticationError:
        raise
    except BaseException:
        executor.shutdown(wait=True)
        raise
    else:
        executor.shutdown(wait=True)

    logger.info(f"⏱️ {batch_name} 完成，耗时: {time.time() - start_time:.2f}s")
    return success_count


# ================= 主程序 =================

def _main(
    target_task_id,
    owner_check=None,
    *,
    locales=None,
    input_task_root=None,
    output_task_root=None,
    state_key="minimax_audio",
):
    owner_check = owner_check or (lambda: None)
    requested_locales = tuple(locales or MINIMAX_LOCALES)
    unsupported = sorted(set(requested_locales) - set(LANGUAGE_BOOST_MAP))
    if unsupported:
        raise ValueError(f"MiniMax Step 5 不支持的语言：{', '.join(unsupported)}")
    input_task_root = input_task_root or os.path.join(
        INPUT_ROOT, str(target_task_id)
    )
    output_task_root = output_task_root or os.path.join(
        OUTPUT_ROOT, str(target_task_id)
    )
    _reset_auth_failure()
    if not MINIMAX_API_KEY:
        logger.error("⚠️ Step 5 缺少 MINIMAX_API_KEY，跳过 MiniMax 音频生成")
        return {
            "total": 0,
            "success": 0,
            "failed": 0,
            "rounds": 0,
            "skipped_reason": "MINIMAX_API_KEY missing",
        }
    if _usage_tracker and target_task_id:
        try:
            _usage_tracker.set_context(target_task_id, "audio_producer")
        except Exception as exc:
            logger.warning(f"⚠️ API 用量上下文设置失败，继续 Step 5：{exc}")

    scan_root = input_task_root
    if not os.path.exists(scan_root):
        logger.warning(f"⚠️ Step 5 找不到 JSON 输入目录，跳过：{scan_root}")
        return {"total": 0, "success": 0, "failed": 0, "rounds": 0}

    # Round 1: 主力进攻
    tasks = scan_json_files(scan_root, target_task_id, requested_locales)
    if not tasks:
        logger.info("🎉 所有音频已完成生成！")
        return {"total": 0, "success": 0, "failed": 0, "rounds": 0}

    rounds = 1
    owner_check()
    run_batch(
        tasks,
        target_task_id,
        "Main-Batch",
        owner_check=owner_check,
        output_task_root=output_task_root,
        state_key=state_key,
    )

    # 最终统计
    final_leftover = scan_json_files(scan_root, target_task_id, requested_locales)
    result = {
        "total": len(tasks),
        "success": len(tasks) - len(final_leftover),
        "failed": len(final_leftover),
        "rounds": rounds,
    }
    if not final_leftover:
        logger.info("🎉 [Audio] 任务圆满完成！")
    else:
        failed_preview = ", ".join(
            os.path.relpath(path, scan_root)
            for path, *_ in final_leftover[:5]
        )
        logger.error(
            f"❌ Step 5 有 {len(final_leftover)} 个任务在有限重试后仍失败，"
            f"已保留失败 JSON 供后续重试（示例: {failed_preview}）"
        )
        ResumeState(output_task_root).mark_step(
            state_key,
            f"remaining:{len(final_leftover)}",
            status="failed",
            error=f"仍有 {len(final_leftover)} 个失败资产",
        )
        raise StepIncompleteError(
            f"MiniMax Step 5 仍有 {len(final_leftover)} 个失败资产"
        )

    item_states = ResumeState(output_task_root).list_items(state_key)
    ResumeState(output_task_root).mark_step(
        state_key,
        json.dumps(
            sorted(
                (key, value.get("input_digest"), value.get("status"))
                for key, value in item_states.items()
            ),
            ensure_ascii=False,
        ),
        status="success",
    )
    return result


def main(
    target_task_id,
    owner_check=None,
    *,
    locales=None,
    input_task_root=None,
    output_task_root=None,
    state_key="minimax_audio",
):
    """运行 Step 5；无论成功或失败都尝试落盘本进程的 API 用量。"""
    try:
        return _main(
            target_task_id,
            owner_check=owner_check,
            locales=locales,
            input_task_root=input_task_root,
            output_task_root=output_task_root,
            state_key=state_key,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        logger.exception(
            "⚠️ MiniMax Step 5 异常，保留已生成音频和失败 JSON 并继续：%s",
            exc,
        )
        raise
    finally:
        if _usage_tracker:
            try:
                _usage_tracker.flush()
            except Exception as exc:
                logger.warning(f"⚠️ API 用量落库失败，不影响 Step 5：{exc}")


if __name__ == "__main__":
    TARGET_ID = ""
    main(TARGET_ID)
