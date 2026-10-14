"""
角色设计器主程序 (Character Designer Main Pipeline)
职责：图片生成。从数据库读取角色提示词数据（由 info_extractor 服务写入），
依次生成角色立绘 -> 变体立绘 -> 情绪变体。

================================================================================
使用说明 (Usage)
================================================================================

【方式一】通过 main.py 管道运行（推荐）

# 1. 自动处理最新任务
python main.py
python novel-game-factory/services/char_designer/main.py

# 2. 指定任务 ID
python novel-game-factory/services/char_designer/main.py --task-id your_task_id

# 3. 指定图片模型提供方
python novel-game-factory/services/char_designer/main.py --image-provider gpt --task-id
python novel-game-factory/services/char_designer/main.py --image-provider wan --model wan2.5-t2i-preview --task-id your_task_id

【方式二】单独运行各图片生成模块

  # 生成角色立绘
  python core/generate_calm.py --task-id your_task_id

  # 生成情绪变体
  python core/generate_emotion.py --task-id your_task_id

【方式三】配置模式
  修改脚本顶部的 MANUAL_TASK_ID 变量，然后直接运行：
  python main.py

================================================================================
命令行参数说明
================================================================================
  --task-id            任务ID（如果不提供则自动查找最新任务）
  --image-provider     图片模型提供方 (qwen/wan/gpt，默认: gpt)
  --model              模型名称（默认根据提供商选择）
  --verbose            显示详细日志

================================================================================
"""

import os
import signal
import sys
import time
import logging
import argparse
import subprocess
import threading
from typing import Optional
from pathlib import Path

# 添加项目根目录到路径
current_dir = Path(__file__).resolve().parent
services_dir = current_dir.parent
project_root_path = services_dir.parent
for path in (str(project_root_path), str(services_dir), str(current_dir)):
    if path not in sys.path:
        sys.path.insert(0, path)


def add_common_config_search_paths() -> None:
    # 本仓库自带 common/config.py 时，直接用它并返回，绝不去扫描其它仓库副本。
    # 否则 glob 命中的外部仓库 config.py 会被 insert(0) 抢占 sys.path，
    # 而部分旧副本的 DATABASE_URL 未对密码里的 '@' 做 %40 编码，导致连库失败。
    if (project_root_path / "common" / "config.py").exists():
        candidate_str = str(project_root_path)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)
        return

    candidates = [
        services_dir,
        services_dir.parent,
        services_dir.parent / "novel-game-factory",
        services_dir.parent / "sj_novel" / "novel-game-factory",
    ]
    search_roots = [services_dir.parent, Path.home() / "Documents" / "code", Path.home() / "Documents" / "gitlab"]
    for search_root in search_roots:
        if search_root.exists():
            candidates.extend(path.parent.parent for path in search_root.glob("*/novel-game-factory/common/config.py"))
            candidates.extend(path.parent.parent for path in search_root.glob("*/*/novel-game-factory/common/config.py"))

    for candidate in candidates:
        if (candidate / "common" / "config.py").exists():
            candidate_str = str(candidate)
            if candidate_str not in sys.path:
                sys.path.insert(0, candidate_str)


add_common_config_search_paths()

os.environ.setdefault("DB_HOST", "172.16.50.17")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_NAME", "extreme")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "change_me")

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=project_root_path / "novel-game-factory" / ".env")
    load_dotenv(dotenv_path=services_dir / ".env")
except ImportError:
    pass

from common.config import OUTPUTS_PATH, STORAGE_PATH
from common.cg_data import load_cg_contract
from common.platform_db import platform_engine
from common.progress_utils import (
    StageLeaseLostError,
    ensure_stage_lease_owned,
    stage_execution_environment,
)
from services.char_designer.core.asset_failure_report import write_failure_report

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================= 配置变量 =================
# 如果不使用命令行参数，可以在这里配置默认值
# 设置为 None 以自动查找最新任务
# 设置为具体字符串 (e.g., "chongsheng_123") 可针对该任务重跑
MANUAL_TASK_ID = None
CONFIG_IMAGE_PROVIDER = "gpt"
CONFIG_MODEL = None  # 使用默认模型
# ===========================================

_DEFAULT_STEP_TIMEOUT = max(
    1,
    int(os.environ.get("MAIN_TASK_TIME_LIMIT", "172800")),
)


def _read_stage_timeout(env_name: str, default: int) -> int:
    """读取有限的子阶段总超时；非法配置回退到主任务硬上限。"""
    raw_value = os.environ.get(env_name)
    if raw_value is None:
        return default

    try:
        timeout = int(raw_value)
    except ValueError:
        logger.warning(
            f"{env_name}={raw_value!r} 不是有效整数，回退到 {default}s"
        )
        return default

    if timeout <= 0:
        logger.warning(
            f"{env_name}={raw_value!r} 不是正数，回退到 {default}s"
        )
        return default
    return timeout


def _read_positive_float(env_name: str, default: float) -> float:
    raw_value = os.environ.get(env_name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


def _signal_process_group(process: subprocess.Popen, sig: int) -> None:
    """向独立子进程组发信号；非 POSIX 环境退回 Popen 方法。"""
    if os.name == "posix":
        try:
            os.killpg(process.pid, sig)
            return
        except ProcessLookupError:
            return
        except OSError as exc:
            logger.warning("向人物子进程组发送信号失败，改用单进程清理: %s", exc)

    try:
        if sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _terminate_process_group(process: subprocess.Popen, script: str) -> None:
    """先 TERM、超出宽限后 KILL，并始终回收子进程。"""
    if process.poll() is not None:
        process.wait()
        return

    grace_seconds = _read_positive_float(
        "CHAR_SUBPROCESS_TERM_GRACE_SECONDS",
        10.0,
    )
    logger.warning("正在终止人物子进程组: %s", script)
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        logger.error(
            "%s 在 %.1fs 宽限期内未退出，发送 SIGKILL",
            script,
            grace_seconds,
        )

    _signal_process_group(process, signal.SIGKILL)
    process.wait()


def _wait_for_subprocess(
    process: subprocess.Popen,
    *,
    task_id: str,
    timeout: int,
) -> int:
    """等待子进程，同时定期确认当前 Worker 仍拥有阶段租约。"""
    poll_interval = _read_positive_float(
        "CHAR_SUBPROCESS_POLL_INTERVAL_SECONDS",
        1.0,
    )
    deadline = time.monotonic() + timeout

    while True:
        ensure_stage_lease_owned(task_id, "char_designer")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            return_code = process.wait(timeout=min(poll_interval, remaining))
        except subprocess.TimeoutExpired:
            continue
        ensure_stage_lease_owned(task_id, "char_designer")
        return return_code


def _stream_subprocess_output(pipe, script: str, task_id: str = "") -> None:
    try:
        for line in iter(pipe.readline, ""):
            print(
                f"[char_designer][task_id={task_id}][{script}] {line}",
                end="",
                flush=True,
            )
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

core_dir = current_dir / "core"


def _load_character_generation_data(task_id: str):
    """读取普通人物提示词与 CG 契约；读取失败时保留原流水线行为。"""
    if not HAS_SQLALCHEMY:
        return None, None

    try:
        with platform_engine.connect() as conn:
            prompt_row = conn.execute(
                text(
                    "SELECT data FROM character_prompts "
                    "WHERE task_id = :task_id ORDER BY id DESC LIMIT 1"
                ),
                {"task_id": task_id},
            ).fetchone()
        return prompt_row[0] if prompt_row else None, load_cg_contract(task_id)
    except Exception as exc:
        logger.warning("读取人物提示词/CG 契约失败，保留完整人物流水线: %s", exc)
        return None, None


def _has_required_cg_identity_references(cg_data) -> bool:
    if not isinstance(cg_data, dict):
        return False

    for cg_spec in cg_data.values():
        if not isinstance(cg_spec, dict):
            continue
        if cg_spec.get("reference_contract_schema") != "cg-reference-v1":
            continue
        requirements = cg_spec.get("reference_requirements") or []
        if any(
            isinstance(item, dict)
            and item.get("role") == "character"
            and item.get("required") is True
            and item.get("reference_policy") == "required_identity"
            for item in requirements
        ):
            return True
    return False


def _is_cg_identity_only_task(task_id: str) -> bool:
    character_prompts, cg_data = _load_character_generation_data(task_id)
    return not character_prompts and _has_required_cg_identity_references(cg_data)


def _build_character_generation_steps(task_id: str, common_step_timeout, emotion_step_timeout):
    full_steps = [
        ("generate_calm.py", "🎨 生成角色立绘（每角色最小组合）", common_step_timeout),
        (
            "generate_cg_identity_references.py",
            "🪪 生成或复用 CG-only 人物身份参考",
            common_step_timeout,
        ),
        ("generate_variant_calm.py", "🔄 生成变体立绘（图生图）", common_step_timeout),
        ("generate_emotion.py", "😊 生成情绪变体", emotion_step_timeout),
        ("expand_canvas.py", "📐 按身高扩展所有 calm/情绪图顶部透明画布", common_step_timeout),
        ("generate_shots.py", "🎬 从远景 LS 裁出中景 MS / 近景 CS", common_step_timeout),
    ]

    if _is_cg_identity_only_task(task_id):
        logger.warning(
            "任务 %s 没有普通人物提示词，但存在 required CG 身份参考；"
            "跳过普通立绘派生阶段，仅执行 CG-only 身份参考生成",
            task_id,
        )
        return [full_steps[1]]
    return full_steps

# 信息提取已迁移至 info_extractor 服务，char_designer 仅负责图片生成，
# 直接从数据库读取 info_extractor 写入的 characters / character_prompts 等表。

# 数据库配置
try:
    from sqlalchemy import text
    HAS_SQLALCHEMY = True
except ImportError:
    HAS_SQLALCHEMY = False
    logger.warning("未安装 sqlalchemy，无法访问数据库")


def generate_character_images_from_db(task_id: str, image_provider: str = CONFIG_IMAGE_PROVIDER, model: Optional[str] = None) -> bool:
    """从数据库生成角色图片（包括完整情绪变体）"""
    try:
        logger.info("🖼️  开始生成角色图片（立绘 + 情绪变体）...")
        return generate_images_from_db_fallback(task_id, image_provider=image_provider, model=model)
    except StageLeaseLostError:
        raise
    except Exception as e:
        logger.error(f"图片生成失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def generate_images_from_db_fallback(task_id: str, image_provider: str = CONFIG_IMAGE_PROVIDER, model: Optional[str] = None) -> bool:
    """生成原生透明角色图，再扩展画布并生成后续资产。"""
    try:
        core_dir = Path(__file__).resolve().parent / "core"
        env = os.environ.copy()
        env['STORAGE_PATH'] = STORAGE_PATH
        env.update(stage_execution_environment(task_id, "char_designer"))

        common_step_timeout = _read_stage_timeout(
            "CHAR_STEP_TIMEOUT",
            _DEFAULT_STEP_TIMEOUT,
        )
        emotion_step_timeout = _read_stage_timeout(
            "CHAR_EMOTION_STEP_TIMEOUT",
            common_step_timeout,
        )
        steps = _build_character_generation_steps(
            task_id,
            common_step_timeout,
            emotion_step_timeout,
        )

        failed_steps = []
        for script, label, step_timeout in steps:
            logger.info(f"{label}（阶段超时上限: {step_timeout}s）")
            # 实时透传子进程输出：合并 stderr 到 stdout，逐行转发到终端，
            # 让"处理角色 i/n: xxx"这类进度像阶段名一样即时显示，
            # 而不是等子脚本整体结束后才批量 dump。
            step_provider = image_provider
            step_model = model
            if script not in {
                "generate_calm.py",
                "generate_cg_identity_references.py",
            } and image_provider in {"qwen", "wan"}:
                step_provider = "qwen"
                # 文生图模型不能传给变体/情绪图生图；统一使用 QWEN_VARIANT_MODEL。
                step_model = None
            command = [sys.executable, "-u", str(core_dir / script), "--task-id", task_id]
            command.extend(["--image-provider", step_provider])
            if step_model:
                command.extend(["--model", step_model])

            proc = subprocess.Popen(
                command,
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                start_new_session=True,
            )

            reader = threading.Thread(
                target=_stream_subprocess_output,
                args=(proc.stdout, script, task_id),
                daemon=True,
            )
            reader.start()

            try:
                returncode = _wait_for_subprocess(
                    proc,
                    task_id=task_id,
                    timeout=step_timeout,
                )
            except subprocess.TimeoutExpired:
                _terminate_process_group(proc, script)
                logger.error(
                    f"❌ {script} 阶段执行超时（>{step_timeout}s），已终止该子进程。"
                    "这是整个批处理阶段的累计时间上限，不是单张图片请求超时。"
                )
                failed_steps.append(
                    {
                        "stage": script,
                        "step": script,
                        "asset_id": "__stage__",
                        "asset_type": "character_stage",
                        "status": "warning",
                        "reason_code": "provider_timeout",
                        "attempts": 1,
                        "max_attempts": 1,
                        "paths": [],
                        "last_error": f"{script} stage timed out",
                    }
                )
                report = write_failure_report(task_id, extra_items=failed_steps)
                logger.error(
                    "[char_designer][FATAL] task_id=%s stage=%s timeout report=%s",
                    task_id,
                    script,
                    report,
                )
                return False
            except BaseException:
                _terminate_process_group(proc, script)
                raise
            finally:
                _finish_output_reader(reader, proc.stdout)

            if returncode == 1:
                logger.error(f"❌ {script} 失败，返回码: {returncode}")
                report = write_failure_report(
                    task_id,
                    extra_items=[
                        {
                            "stage": script,
                            "step": script,
                            "asset_id": "__stage__",
                            "asset_type": "character_stage",
                            "status": "warning",
                            "reason_code": "stage_error",
                            "attempts": 1,
                            "max_attempts": 1,
                            "paths": [],
                            "last_error": f"{script} returned exit code {returncode}",
                        }
                    ],
                )
                logger.error(
                    "[char_designer][FATAL] task_id=%s stage=%s report=%s",
                    task_id,
                    script,
                    report,
                )
                return False
            if returncode not in (0, 2):
                logger.error(f"❌ {script} 失败，未知返回码: {returncode}")
                return False
            if returncode == 2:
                logger.warning(f"⚠️ {script} 完成但存在资产级失败（返回码 2）")
                failed_steps.append(
                    {
                        "stage": script,
                        "step": script,
                        "asset_id": "__stage__",
                        "asset_type": "character_stage",
                        "status": "missing",
                        "reason_code": "asset_generation_error",
                        "attempts": 1,
                        "max_attempts": 1,
                        "paths": [],
                        "last_error": f"{script} completed with asset-level failures",
                    }
                )
            logger.info(f"✅ {script} 完成")

        task_output_root = (
            Path(os.getenv("STORAGE_PATH", STORAGE_PATH))
            / "outputs"
            / task_id
        )
        report = None
        if failed_steps or task_output_root.exists():
            report = write_failure_report(task_id, extra_items=failed_steps)
        if failed_steps:
            execution_id = os.getenv("NOVEL_GAME_EXECUTION_ID", "") or "-"
            platform_attempt = os.getenv("NOVEL_GAME_PLATFORM_ATTEMPT", "0") or "0"
            logger.warning(
                "[char_designer][PARTIAL] task_id=%s execution_id=%s platform_attempt=%s "
                "failed_steps=%s report=%s",
                task_id,
                execution_id,
                platform_attempt,
                len(failed_steps),
                report,
            )
        elif report is not None:
            logger.info("[char_designer][REPORT] task_id=%s report=%s", task_id, report)
        return True

    except StageLeaseLostError:
        raise
    except Exception as e:
        logger.error(f"图片生成执行失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def find_latest_task_from_db():
    """从数据库查找最新任务ID"""
    if not HAS_SQLALCHEMY:
        logger.warning("未安装 sqlalchemy，无法从数据库查找任务")
        return None

    try:
        with platform_engine.connect() as conn:
            sql = text("SELECT task_id FROM story_tree_json ORDER BY id DESC LIMIT 1")
            res = conn.execute(sql).fetchone()
            if res:
                task_id = res[0]
                logger.info(f"🔍 从数据库找到最新任务ID: {task_id}")
                return task_id
            else:
                logger.info("🔍 数据库中未找到任何任务")
                return None
    except Exception as e:
        logger.error(f"❌ 从数据库查找任务失败: {e}")
        return None


def run_pipeline(
    task_id: Optional[str] = None,
    model: Optional[str] = None,
    image_provider: str = CONFIG_IMAGE_PROVIDER,
    generate_images: bool = True,
) -> Optional[str]:
    """
    运行图片生成管道：从数据库读取角色提示词数据 -> 生成立绘/变体/情绪图片。
    角色提取、提示词构造等数据由 info_extractor 服务预先写入数据库。

    Args:
        task_id: 任务ID，如果为 None 则自动查找最新任务
        model: 模型名称
        image_provider: 图片模型提供方
        generate_images: 是否生成角色图片（默认：True）

    Returns:
        成功返回 task_id，失败返回 None
    """
    pipeline_start = time.time()

    # 加载环境变量（从项目根目录）
    try:
        from dotenv import load_dotenv
        # 指向 novel-game-factory/.env（仅在本地开发时使用）
        dotenv_path = current_dir.parent.parent / ".env"
        if dotenv_path.exists():
            load_dotenv(dotenv_path=dotenv_path)
            logger.info(f"已加载环境变量文件: {dotenv_path}")
        # Docker 容器内环境变量由 docker-compose 传入，不需要 .env 文件
    except ImportError:
        pass  # Docker 容器内环境变量已配置，不需要 dotenv

    print("\n" + "#" * 60)
    print("🎨 [Pipeline] 启动角色设计器自动化流水线")
    if task_id:
        print(f"🔧 [Mode] 指定任务模式: {task_id}")
    else:
        print(f"🆕 [Mode] 自动最新任务模式")
    print("#" * 60)

    # 如果没有提供 task_id，从数据库查找最新任务
    if task_id is None:
        task_id = find_latest_task_from_db()
        if task_id is None:
            logger.error("未找到任何任务，流程终止")
            return None

    print(f"🌟 [Context] 当前流水线 Task ID: {task_id}")

    if _usage_tracker:
        _usage_tracker.set_context(task_id, "char_designer")

    # ==========================================
    # 生成角色立绘、变体立绘和情绪变体
    # 提示词等数据由 info_extractor 服务预先写入数据库
    # ==========================================
    if generate_images:
        print("\n" + "=" * 60)
        print("🔹 Step 1: 生成角色立绘（每角色最小组合）")
        print("🔹 Step 2: 生成或复用 CG-only 人物身份参考")
        print("🔹 Step 3: 生成变体立绘（图生图）")
        print("🔹 Step 4: 生成情绪变体")
        print("🔹 Step 5: 按身高扩展 calm/情绪图顶部透明画布")
        print("🔹 Step 6: 从远景 LS 裁出中景 MS / 近景 CS")
        print("=" * 60)

        image_success = generate_character_images_from_db(task_id, image_provider=image_provider, model=model)
        if not image_success:
            logger.error("角色图片流水线失败，具体失败阶段请查看上一条错误日志")
            return None
    else:
        print("\n⏭️  跳过图片生成步骤")

    # ==========================================
    # 总结
    # ==========================================
    duration = time.time() - pipeline_start
    print("\n" + "#" * 60)
    print(f"🎉 [Success] 角色设计器流水线执行完毕！")
    print(f"⏱️  总耗时: {duration:.2f} 秒")
    print(f"📂 产物路径: {OUTPUTS_PATH}/{task_id}/images/characters/")
    print(f"📝 日志路径: {current_dir}/log/")
    print("#" * 60)

    if _usage_tracker:
        _usage_tracker.flush()

    return task_id


def main():
    """命令行入口"""
    # 清理代理环境变量
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
    os.environ.pop("all_proxy", None)

    # 禁用 OpenMP 嵌套警告
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["KMP_WARNINGS"] = "0"

    parser = argparse.ArgumentParser(
        description="角色设计器 - 从数据库读取角色提示词数据，生成立绘、变体与情绪图片",
    )

    parser.add_argument(
        "--task-id",
        default=MANUAL_TASK_ID,
        help="任务ID（如果不提供则自动查找最新任务）"
    )
    parser.add_argument(
        "--image-provider",
        choices=["qwen", "wan", "gpt"],
        default=CONFIG_IMAGE_PROVIDER,
        help="图片模型提供方 (qwen/wan/gpt，默认: gpt)"
    )
    parser.add_argument(
        "--model",
        default=CONFIG_MODEL,
        help="模型名称（默认根据提供商选择）"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="显示详细日志"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # 运行图片生成管道
    try:
        result_task_id = run_pipeline(
            task_id=args.task_id,
            model=args.model,
            image_provider=args.image_provider,
        )

        if result_task_id:
            print("\n✅ 流水线执行成功!")
            sys.exit(0)
        else:
            print("\n❌ 流水线执行失败")
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n🛑 用户手动中止流程")
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ 流水线执行异常: {e}")
        import traceback
        logger.error(traceback.format_exc())
        print(f"\n❌ 流水线执行异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
