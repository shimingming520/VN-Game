"""Audio producer tasks - generate audio assets."""
import os
import sys
import asyncio
import threading
import time

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.celery_app import celery_app
from common.task_editors import record_task_editor_if_success

# 进度更新（解耦，不影响主逻辑）
# 注意：此处不再使用 try/except ImportError fallback，导入失败会直接报错，
# 避免 fail_stage 等函数变成空函数导致任务状态永远卡在"处理中"。
# 原 fallback 代码如下，仅供参考：
#   except ImportError:
#       PROGRESS_ENABLED = False
#       def start_stage(*args, **kwargs): pass
#       def complete_stage(*args, **kwargs): pass
#       def fail_stage(*args, **kwargs): pass
#       def update_task_progress(*args, **kwargs): pass
#       def is_stage_already_running(*args, **kwargs): return False
#       def wait_for_stage_completion(*args, **kwargs): pass
from common.progress_utils import (
    StageLeaseLostError,
    complete_stage,
    fail_stage,
    is_stage_already_running,
    renew_stage_lease,
    start_stage,
    update_task_progress,
    wait_for_stage_completion,
)
PROGRESS_ENABLED = True


@celery_app.task(
    bind=True,
    name="services.audio_producer.tasks.regenerate_audio",
    time_limit=900,
    soft_time_limit=840,
    autoretry_for=(Exception,),
    max_retries=2,
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
)
def regenerate_audio(self, task_id: str, node_id: str, material_id: str = None, new_content: str = None, force: bool = False, session_no: int = None, user_id: str = None, user_name: str = None, language: str = "zh-CN"):
    """
    重生成单条 TTS 音频。

    Args:
        task_id: 任务 ID
        node_id: 节点 ID，如 chapter_1、node_14beaa8b
        material_id: 字幕文件名，如 scene_alley-1.txt 或 UnknownScene-3.txt；None=该节点下所有音频
        new_content: 新内容（若提供则先更新 plots 表再重生成）
        force: True=跳过存在性检查强制重生成
        session_no: 操作序号（None=自动分配）
        language: 语言（zh-CN/en-US/ja-JP），默认 zh-CN

    Returns:
        dict，包含 total/success/failed/session_no/items/errors
    """
    print(f"🔄 Starting audio regenerate for task_id={task_id}, node_id={node_id}, material_id={material_id} language={language} "
          f"(尝试 {self.request.retries + 1}/{self.max_retries + 1})")

    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)

        from regenerate_audio import regenerate_audio as _regen

        result = _regen(task_id=task_id, node_id=node_id, material_id=material_id, new_content=new_content, force=force, session_no=session_no, user_id=user_id, user_name=user_name, language=language)
        if result.get("failed", 0) > 0:
            print(f"❌ Audio regenerate 业务失败: {result.get('errors')}")
        else:
            print(f"✅ Audio regenerate completed: {result['success']}/{result['total']} success")
        try:
            record_task_editor_if_success(result, task_id, user_id, user_name)
        except Exception as exc:
            print(f"⚠️  音频重生成成功，但记录编辑者失败: {exc}")
        return result

    except Exception as e:
        print(f"❌ Audio regenerate exception (尝试 {self.request.retries + 1}): {str(e)}")
        import traceback
        traceback.print_exc()
        raise


@celery_app.task(
    bind=True,
    name="services.audio_producer.tasks.regenerate_character_voice",
    time_limit=3600,
    soft_time_limit=3540,
    autoretry_for=(Exception,),
    max_retries=2,
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
)
def regenerate_character_voice(self, task_id: str, speaker_id: str, voice_id: str,
                               user_id: str = None, user_name: str = None, language: str = "zh-CN"):
    """
    角色级换音色：更新 voice_mappings 中该角色的 voice_id，并联动重生成该角色在该语言下的全部台词音频。

    Args:
        task_id:    任务 ID
        speaker_id: 角色 speaker_id（与字幕前缀一致）
        voice_id:   新音色 ID
        language:   语言（默认 zh-CN，只改中文）

    Returns:
        dict，包含 total/success/failed/session_no/voice_id/speaker_id/items/errors
    """
    print(f"🔄 Starting character voice regenerate: task_id={task_id}, speaker_id={speaker_id}, "
          f"voice_id={voice_id} language={language} (尝试 {self.request.retries + 1}/{self.max_retries + 1})")

    if not task_id or not speaker_id or not voice_id:
        return {"total": 0, "success": 0, "failed": 1, "items": [],
                "errors": ["task_id / speaker_id / voice_id 不能为空"]}

    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)

        from regenerate_audio import regenerate_character_voice as _regen

        result = _regen(task_id=task_id, speaker_id=speaker_id, voice_id=voice_id,
                        user_id=user_id, user_name=user_name, language=language)
        if result.get("failed", 0) > 0:
            print(f"❌ Character voice regenerate 部分失败: {result.get('errors')}")
        else:
            print(f"✅ Character voice regenerate completed: {result.get('success')}/{result.get('total')} success")
        try:
            record_task_editor_if_success(result, task_id, user_id, user_name)
        except Exception as exc:
            print(f"⚠️  角色换音色成功，但记录编辑者失败: {exc}")
        return result

    except Exception as e:
        print(f"❌ Character voice regenerate exception (尝试 {self.request.retries + 1}): {str(e)}")
        import traceback
        traceback.print_exc()
        raise


@celery_app.task(name="services.audio_producer.tasks.generate_audio")
def generate_audio(task_id: str):
    """
    Generate audio assets for a given task_id.

    Args:
        task_id: Task ID (game_project identifier)

    Returns:
        str: task_id for next step in chain

    Raises:
        ValueError: If task_id is invalid
        Exception: If audio generation fails
    """
    # 参数验证
    if not task_id or not isinstance(task_id, str):
        raise ValueError(f"Invalid task_id: {task_id}")

    if is_stage_already_running(task_id, 'audio_producer'):
        wait_for_stage_completion(task_id, 'audio_producer')
        # 被动等待的 Worker 也必须验收真实产物，不能仅凭数据库的
        # COMPLETED 状态继续下游；这正是换 Worker 后最容易漏掉的路径。
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)
        import main as main_module
        validator = getattr(main_module, "_validate_audio_asset_snapshot", None)
        if validator is not None:
            try:
                validator(task_id)
            except Exception as exc:
                # 旧 Worker 可能已经把阶段标成 COMPLETED，但没有发布英语
                # WAV。不要把这个状态当成终点，重新抢占并补跑缺失产物。
                print(
                    f"⚠️ 阶段状态已完成但产物验收失败，重新接管补跑: {exc}"
                )
                start_stage(task_id, "audio_producer", 50)
            else:
                return task_id
        else:
            return task_id

    print(f"🎵 Starting audio generation for task_id: {task_id}")

    def owner_check():
        if not renew_stage_lease(task_id, 'audio_producer'):
            raise StageLeaseLostError(
                f"[audio_producer] task_id={task_id} stage lease ownership lost"
            )

    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)

        from main import run_pipeline

        update_task_progress(task_id, progress=50)

        asyncio.run(
            run_pipeline(manual_task_id=task_id, owner_check=owner_check)
        )

        # 产物镜像到 OSS。按语言分别严格校验，避免日语有文件时掩盖英语
        # 目录缺失；没有预期输入的语言才允许跳过。
        try:
            import main as main_module
            from common.output_mirror import mirror_outputs
            # 上传线程会在每个文件完成后回调，但不能为每个文件都打一次
            # 数据库续租请求；这里做进程内节流，默认最多每 20 秒续一次。
            mirror_lease_lock = threading.Lock()
            mirror_lease_last_refresh = [time.monotonic()]

            def mirror_progress():
                now = time.monotonic()
                with mirror_lease_lock:
                    if now - mirror_lease_last_refresh[0] < 20:
                        return
                    mirror_lease_last_refresh[0] = now
                owner_check()

            # The pipeline may spend time finalising files before the first
            # mirror request; verify ownership immediately before publishing.
            owner_check()
            snapshot_fn = getattr(main_module, "_audio_asset_snapshot", None)
            snapshot = snapshot_fn(task_id) if snapshot_fn is not None else {}
            for locale in ("en-US", "ja-JP"):
                values = snapshot.get(locale, {})
                if values.get("audio", 0) > 0:
                    mirror_outputs(
                        task_id,
                        [f"*/audios/{locale}-audio"],
                        strict=True,
                        progress_callback=mirror_progress,
                    )
                if values.get("subtitles", 0) > 0:
                    mirror_outputs(
                        task_id,
                        [f"*/audios/{locale}-subtitle"],
                        strict=True,
                        progress_callback=mirror_progress,
                    )
            # 仅镜像仍在使用的硬校验报告；翻译质量审校已停用，不再生成/上传
            # translation-quality-report-* 文件。
            mirror_outputs(
                task_id,
                [
                    "translation-hard-violations-*.json",
                ],
                force=True,
                progress_callback=mirror_progress,
            )
        except Exception as _e:
            print(f"❌ audio_producer 产物镜像 OSS 失败: {_e}")
            raise

        owner_check()
        complete_stage(task_id, 'audio_producer', 60)

        print(f"✅ Audio assets generated successfully for task_id: {task_id}")
        return task_id

    except StageLeaseLostError:
        # 已失去执行权的旧 delivery 不能发布成功或失败终态。
        raise
    except Exception as e:
        fail_stage(task_id, 'audio_producer', 40, str(e))
        print(f"❌ Audio generation failed: {str(e)}")
        import traceback
        traceback.print_exc()
        # 抛出异常，中断任务链
        raise Exception(f"Audio generation failed for task_id {task_id}: {str(e)}")
