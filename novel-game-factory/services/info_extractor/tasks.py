"""Info extractor tasks - 从故事树/剧情提取角色、剧情、场景、封面、氛围等数据写入数据库。"""
import os
import sys

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.celery_app import celery_app

# 进度更新（解耦，不影响主逻辑）
# 注意：此处不再使用 try/except ImportError fallback，导入失败会直接报错，
# 避免 fail_stage 等函数变成空函数导致任务状态永远卡在"处理中"。
from common.progress_utils import start_stage, complete_stage, fail_stage, update_task_progress, is_stage_already_running, wait_for_stage_completion
PROGRESS_ENABLED = True


@celery_app.task(name="services.info_extractor.tasks.extract_info")
def extract_info(task_id: str):
    """
    运行信息提取流水线：character（含 plot）-> scene -> atmosphere。
    所有产物写入数据库（characters / plots / character_prompts / scenes /
    backgrounds / atmosphere / story_tree_json），供下游服务读取。

    Args:
        task_id: Task ID (game_project identifier)

    Returns:
        str: task_id for next step in chain

    Raises:
        ValueError: If task_id is invalid
        Exception: If extraction fails
    """
    if not task_id or not isinstance(task_id, str):
        raise ValueError(f"Invalid task_id: {task_id}")

    if is_stage_already_running(task_id, 'info_extractor'):
        wait_for_stage_completion(task_id, 'info_extractor')
        return task_id

    print(f"🧩 Starting info extraction for task_id: {task_id}")

    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)

        from main import run_pipeline

        update_task_progress(task_id, progress=25)

        result = run_pipeline(task_id=task_id)
        if result is None:
            raise Exception("run_pipeline returned None — check info_extractor logs for details")

        # 产物镜像到 OSS（本地保留，OSS 多存一份；失败不影响主流程）
        # info_extractor 落地 game_builder 需要的 JSON：story_tree / name_mappings / 主调度表
        try:
            from common.output_mirror import mirror_outputs
            mirror_outputs(task_id, ["story_tree.json", "name_mappings.json",
                                     "atmophere.json", f"{task_id}.json"])
        except Exception as _e:
            print(f"⚠️  info_extractor 产物镜像 OSS 失败（不影响主流程）: {_e}")

        complete_stage(task_id, 'info_extractor', 30)

        print(f"✅ Info extraction completed for task_id: {task_id}")
        return task_id

    except Exception as e:
        fail_stage(task_id, 'info_extractor', 15, str(e))
        print(f"❌ Info extraction failed: {str(e)}")
        import traceback
        traceback.print_exc()
        raise Exception(f"Info extraction failed for task_id {task_id}: {str(e)}")
