#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键执行脚本 - 按顺序执行场景/封面图片生成全流程

scene_painter 仅负责图片生成。提示词等数据由 info_extractor 服务预先写入数据库
（scene_prompts / backgrounds 表），本脚本直接读库生成图片。

执行顺序：
1. scene_generator.py      - 从 scene_prompts 表生成场景图片（一级底图文生图 + 二级变体图生图）
2. retry_failed_images.py  - 补生成缺失的场景图片
3. cover_generator.py      - 从 backgrounds 表生成封面图片
4. retry_failed_images.py  - 补生成缺失的封面图片


# 全流程（场景图片 + 封面图片），后面填写具体 task-id
python novel-game-factory/services/scene_painter/main_to_db.py --task-id

# 仅生成封面图片（游戏封面 + 章节封面）
python novel-game-factory/services/scene_painter/main_to_db.py --mode cover --task-id

# 仅场景流程
python novel-game-factory/services/scene_painter/main_to_db.py --mode scene --task-id

# 仅生成 CG 图片
python novel-game-factory/services/scene_painter/main_to_db.py --mode cg --task-id

# 仅补生成缺失图片
python novel-game-factory/services/scene_painter/main_to_db.py --mode retry --task-id

"""

import sys
import os
import signal
import subprocess
import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

_SERVICE_DIR = Path(__file__).parent.resolve()
_PROJECT_ROOT = _SERVICE_DIR.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from common.progress_utils import ensure_stage_lease_owned, stage_execution_environment

_LOGS_DIR = _SERVICE_DIR / 'logs'
_LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOGS_DIR / 'main_to_db.log', encoding='utf-8'),
    ]
)

logger = logging.getLogger(__name__)


_DEFAULT_SUBPROCESS_TIMEOUT = max(
    1,
    int(os.environ.get("MAIN_TASK_TIME_LIMIT", "172800")),
)


def _read_positive_float(env_name: str, default: float) -> float:
    raw_value = os.environ.get(env_name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


def _read_positive_int(env_name: str, default: int) -> int:
    raw_value = os.environ.get(env_name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


def _signal_process_group(process: subprocess.Popen, sig: int) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, sig)
            return
        except ProcessLookupError:
            return
        except OSError as exc:
            logger.warning("向场景子进程组发送信号失败，改用单进程清理: %s", exc)

    try:
        if sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _terminate_process_group(process: subprocess.Popen, description: str) -> None:
    """先 TERM、超出宽限后 KILL，并始终回收子进程。"""
    if process.poll() is not None:
        process.wait()
        return

    grace_seconds = _read_positive_float(
        "SCENE_SUBPROCESS_TERM_GRACE_SECONDS",
        10.0,
    )
    logger.warning("正在终止场景子进程组: %s", description)
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        logger.error(
            "%s 在 %.1fs 宽限期内未退出，发送 SIGKILL",
            description,
            grace_seconds,
        )

    _signal_process_group(process, signal.SIGKILL)
    process.wait()


def _wait_for_subprocess(
    process: subprocess.Popen,
    *,
    task_id: str | None,
    timeout_seconds: int,
) -> int:
    poll_interval = _read_positive_float(
        "SCENE_SUBPROCESS_POLL_INTERVAL_SECONDS",
        1.0,
    )
    deadline = time.monotonic() + timeout_seconds

    while True:
        if task_id:
            ensure_stage_lease_owned(task_id, "scene_painter")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout_seconds)
        try:
            return_code = process.wait(timeout=min(poll_interval, remaining))
        except subprocess.TimeoutExpired:
            continue
        if task_id:
            ensure_stage_lease_owned(task_id, "scene_painter")
        return return_code


def _stream_output(
    pipe,
    description: str,
    output_lines: deque,
    task_id: str = "",
) -> None:
    try:
        for line in iter(pipe.readline, ""):
            line = line.rstrip()
            output_lines.append(line)
            print(f"[scene_painter][task_id={task_id}][{description}] {line}")
    finally:
        pipe.close()


def _finish_output_reader(reader: threading.Thread, pipe) -> None:
    reader.join(timeout=5)
    if reader.is_alive():
        try:
            pipe.close()
        except Exception:
            pass
        reader.join(timeout=5)


def run_command(
    cmd,
    description,
    cwd=None,
    *,
    task_id=None,
    timeout_seconds=None,
):
    logger.info(f"开始执行: {description}")
    logger.info(f"执行命令: {' '.join(str(c) for c in cmd)}")
    print(f"\n【开始执行】{description}")

    try:
        subprocess_env = os.environ.copy()
        if task_id:
            subprocess_env.update(
                stage_execution_environment(task_id, "scene_painter")
            )
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            bufsize=1,
            universal_newlines=True,
            cwd=cwd,
            start_new_session=True,
            env=subprocess_env,
        )
    except Exception as e:
        logger.error(f"❌ {description} 执行异常: {e}")
        print(f"❌ {description} 执行异常: {e}")
        return -1, "", str(e)

    tail_lines = _read_positive_int("SCENE_SUBPROCESS_LOG_TAIL_LINES", 500)
    output_lines = deque(maxlen=tail_lines)
    reader = threading.Thread(
        target=_stream_output,
        args=(process.stdout, description, output_lines, task_id or ""),
        daemon=True,
    )
    reader.start()
    effective_timeout = timeout_seconds or _read_positive_int(
        "SCENE_SUBPROCESS_TIMEOUT_SECONDS",
        _DEFAULT_SUBPROCESS_TIMEOUT,
    )

    try:
        return_code = _wait_for_subprocess(
            process,
            task_id=task_id,
            timeout_seconds=effective_timeout,
        )
    except subprocess.TimeoutExpired:
        _terminate_process_group(process, description)
        error = f"{description} 执行超时（>{effective_timeout}s）"
        logger.error("❌ %s", error)
        print(f"❌ {error}")
        return -1, '\n'.join(output_lines), error
    except BaseException:
        _terminate_process_group(process, description)
        raise
    finally:
        _finish_output_reader(reader, process.stdout)

    if return_code == 0:
        logger.info(f"✅ {description} 执行成功")
        print(f"✅ {description} 执行成功\n")
    elif return_code == 2:
        logger.warning(f"⚠️ {description} 部分资产失败，但阶段可继续")
        print(f"⚠️ {description} 部分资产失败，但阶段可继续\n")
    else:
        logger.error(f"❌ {description} 执行失败，返回码: {return_code}")
        print(f"❌ {description} 执行失败，返回码: {return_code}\n")

    return return_code, '\n'.join(output_lines), ""


STEP_GROUPS = {
    "all": [
        ("core/scene_generator.py",        "1. 生成场景图片",      []),
        ("core/retry_failed_images.py",    "2. 补生成缺失场景图片",  ["--type", "scenes"]),
        ("core/cover_generator.py",        "3. 生成封面图片",      []),
        ("core/retry_failed_images.py",    "4. 补生成缺失封面图片",  ["--type", "backgrounds"]),
        ("core/cg_generator.py",           "5. 生成 CG 图片",      []),
    ],
    "scene_assets": [
        ("core/scene_generator.py",        "1. 生成场景图片",      []),
        ("core/retry_failed_images.py",    "2. 补生成缺失场景图片",  ["--type", "scenes"]),
        ("core/cover_generator.py",        "3. 生成封面图片",      []),
        ("core/retry_failed_images.py",    "4. 补生成缺失封面图片",  ["--type", "backgrounds"]),
    ],
    "scene": [
        ("core/scene_generator.py",        "1. 生成场景图片",      []),
        ("core/retry_failed_images.py",    "2. 补生成缺失场景图片",  ["--type", "scenes"]),
        ("core/cg_generator.py",           "3. 生成 CG 图片",      []),
    ],
    "cg": [
        ("core/cg_generator.py",           "1. 生成 CG 图片",      []),
    ],
    "cover": [
        ("core/cover_generator.py",        "1. 生成封面图片",      []),
        ("core/retry_failed_images.py",    "2. 补生成缺失封面图片",  ["--type", "backgrounds"]),
    ],
    "retry": [
        ("core/retry_failed_images.py",    "1. 补生成缺失图片",    ["--type", "all"]),
    ],
}


def main(task_id=None, mode="all"):
    steps = [((_SERVICE_DIR / s), d, extra) for s, d, extra in STEP_GROUPS.get(mode, STEP_GROUPS["all"])]

    logger.info("=" * 60)
    logger.info(f"开始执行 [{mode}] 流程")
    if task_id:
        logger.info(f"指定 Task ID: {task_id}")
    logger.info(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    success_count = 0
    failed_modules = []
    partial_modules = []

    for script, description, extra_args in steps:
        cmd = [sys.executable, str(script)]
        if task_id:
            cmd.extend(["--task-id", task_id])
        cmd.extend(extra_args)
        return_code, stdout, stderr = run_command(
            cmd,
            description,
            cwd=_SERVICE_DIR,
            task_id=task_id,
        )
        if return_code == 0:
            success_count += 1
        elif return_code == 2:
            partial_modules.append(script)
            logger.warning(
                "资产级部分完成: %s（继续执行后续步骤）", script
            )
        else:
            failed_modules.append(script)
            logger.error(f"错误详情: {stderr}")

    logger.info("=" * 60)
    logger.info(
        "执行总结: 完整=%s, 部分=%s, 失败=%s, 总步骤=%s",
        success_count,
        len(partial_modules),
        len(failed_modules),
        len(steps),
    )
    if failed_modules:
        logger.error(f"失败模块: {', '.join(str(m) for m in failed_modules)}")
    if partial_modules:
        logger.warning(
            "部分资产缺失模块: %s；将继续执行后续步骤",
            ', '.join(str(m) for m in partial_modules),
        )
    else:
        logger.info("🎉 所有模块执行成功！")
        if mode in ("all", "scene", "scene_assets"):
            logger.info(f"- 场景图片: outputs/{task_id}/images/scenes/")
        if mode in ("all", "scene", "cg"):
            logger.info(f"- CG 图片: outputs/{task_id}/images/cg/")
        if mode in ("all", "scene_assets", "cover", "cover-extract", "cover-generate"):
            logger.info(f"- 封面图片: outputs/{task_id}/images/backgrounds/")
    logger.info(f"结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    report_path = None
    task_output_root = Path(os.getenv("STORAGE_PATH", "/storage")) / "outputs" / str(task_id or "")
    if task_id and task_output_root.exists():
        try:
            from services.scene_painter.core.asset_failure_report import write_failure_report

            report_path = write_failure_report(
                task_id,
                include_cg=(mode not in {"scene_assets", "scene"}),
            )
            logger.info(
                "[scene_painter][REPORT] task_id=%s report=%s",
                task_id,
                report_path,
            )
        except Exception as exc:
            # 报告写失败属于共享产物不可持久化，不能伪装成 partial 成功。
            logger.error("场景资产失败报告写入失败: %s", exc)
            return 1

    if mode in {"all", "scene_assets"} and task_id:
        try:
            try:
                from services.scene_painter.core.artifact_contract import (
                    validate_scene_output_contract,
                )
            except ImportError:
                from core.artifact_contract import validate_scene_output_contract

            try:
                gaps = validate_scene_output_contract(
                    task_id,
                    include_cg=(mode == "all"),
                    allow_partial=True,
                )
            except TypeError:
                # Backward-compatible test doubles and direct utilities may
                # still expose the old strict validator signature.
                validate_scene_output_contract(task_id)
                gaps = []
            if failed_modules:
                logger.error(
                    "存在阶段级失败，不能伪装成资产级 partial: %s",
                    ', '.join(str(item) for item in failed_modules),
                )
                return 1
            if gaps:
                execution_id = os.getenv("NOVEL_GAME_EXECUTION_ID", "") or "-"
                platform_attempt = os.getenv("NOVEL_GAME_PLATFORM_ATTEMPT", "0") or "0"
                logger.warning(
                    "[scene_painter][PARTIAL] task_id=%s execution_id=%s "
                    "platform_attempt=%s 合同缺口=%s，继续下游；报告已写入",
                    task_id,
                    execution_id,
                    platform_attempt,
                    len(gaps),
                )
                return 2
            if partial_modules:
                logger.info(
                    "部分步骤曾出现资产级失败，但补图后最终合同完整，按成功收口"
                )
            return 0
        except Exception as exc:
            logger.error("最终场景产物合同校验失败: %s", exc)
            return 1

    if failed_modules:
        return 1
    return 2 if partial_modules else 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="scene_painter 流程")
    parser.add_argument("--task-id", type=str, default=None, help="指定task_id")
    parser.add_argument(
        "--mode",
        type=str,
        default="all",
        choices=list(STEP_GROUPS.keys()),
        help="执行模式: all(全流程) / scene(场景图片) / cover(封面图片) / retry(仅补生成缺失图片)"
    )
    args = parser.parse_args()
    sys.exit(main(task_id=args.task_id, mode=args.mode))
