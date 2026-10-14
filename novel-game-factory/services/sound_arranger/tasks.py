"""Sound arranger tasks - generate BGM/SFX sound assets."""
import os
import sys
import asyncio

# Add project root to path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.celery_app import celery_app
from common.task_editors import record_task_editor_if_success

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

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
from common.progress_utils import start_stage, complete_stage, fail_stage, update_task_progress, is_stage_already_running, wait_for_stage_completion
PROGRESS_ENABLED = True


@celery_app.task(
    bind=True,
    name="services.sound_arranger.tasks.regenerate_sound",
    time_limit=900,
    soft_time_limit=840,
    autoretry_for=(Exception,),
    max_retries=2,
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
)
def regenerate_sound(self, task_id: str, node_id: str, material_id: str = None, new_prompt: str = None, sound_type: str = "all", force: bool = False, session_no: int = None, user_id: str = None, user_name: str = None):
    """
    重生成 BGM/SFX 音频。

    Args:
        task_id: 任务 ID
        node_id: 节点 ID，如 chapter_1、node_14beaa8b
        material_id: bgm_id 或 sfx_id；None=该节点下所有匹配 sound_type 的音频
        new_prompt: 新提示词（若提供则先更新 sound 表再重生成）
        sound_type: "bgm" | "sfx" | "all"，仅在 material_id=None 时生效
        force: True=跳过存在性检查强制重生成
        session_no: 操作序号（None=自动分配）

    Returns:
        dict，包含 total/success/failed/session_no/items/errors
    """
    print(f"🔄 Starting sound regenerate for task_id={task_id}, node_id={node_id}, material_id={material_id} "
          f"(尝试 {self.request.retries + 1}/{self.max_retries + 1})")

    if _usage_tracker:
        _usage_tracker.set_context(task_id, "sound_arranger")

    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)

        from regenerate_sound import regenerate_sound as _regen

        result = _regen(task_id=task_id, node_id=node_id, material_id=material_id, new_prompt=new_prompt, sound_type=sound_type, force=force, session_no=session_no, user_id=user_id, user_name=user_name)
        if result.get("failed", 0) > 0:
            print(f"❌ Sound regenerate 业务失败: {result.get('errors')}")
        else:
            print(f"✅ Sound regenerate completed: {result['success']}/{result['total']} success")
        if _usage_tracker:
            _usage_tracker.flush()
        try:
            record_task_editor_if_success(result, task_id, user_id, user_name)
        except Exception as exc:
            print(f"⚠️  音效重生成成功，但记录编辑者失败: {exc}")
        return result

    except Exception as e:
        print(f"❌ Sound regenerate exception (尝试 {self.request.retries + 1}): {str(e)}")
        import traceback
        traceback.print_exc()
        if _usage_tracker:
            _usage_tracker.flush()
        raise


@celery_app.task(name="services.sound_arranger.tasks.generate_sound")
def generate_sound(task_id: str):
    """
    Generate sound assets for a given task_id.

    Two-step process:
    1. Extract BGM/SFX descriptions from plots and save to sound table
    2. Generate audio files via Stable Audio API

    Args:
        task_id: Task ID (game_project identifier)

    Returns:
        str: task_id for next step in chain

    Raises:
        ValueError: If task_id is invalid
        Exception: If sound generation fails
    """
    # Parameter validation
    if not task_id or not isinstance(task_id, str):
        raise ValueError(f"Invalid task_id: {task_id}")

    if is_stage_already_running(task_id, 'sound_arranger'):
        wait_for_stage_completion(task_id, 'sound_arranger')
        return task_id

    print(f"🎼 Starting sound arrangement for task_id: {task_id}")

    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)

        from main import run_pipeline

        update_task_progress(task_id, progress=65)

        asyncio.run(run_pipeline(manual_task_id=task_id))

        # 产物镜像到 OSS（本地保留，OSS 多存一份；失败不影响主流程）
        try:
            from common.output_mirror import mirror_outputs
            mirror_outputs(task_id, ["*/audios/bgm", "*/audios/sfx", "*/audios/ambience"])
        except Exception as _e:
            print(f"⚠️  sound_arranger 产物镜像 OSS 失败（不影响主流程）: {_e}")

        complete_stage(task_id, 'sound_arranger', 68)

        print(f"✅ Sound assets generated successfully for task_id: {task_id}")
        return task_id

    except Exception as e:
        fail_stage(task_id, 'sound_arranger', 62, str(e))
        print(f"❌ Sound arrangement failed: {str(e)}")
        import traceback
        traceback.print_exc()
        # Raise exception to interrupt task chain
        raise Exception(f"Sound arrangement failed for task_id {task_id}: {str(e)}")
