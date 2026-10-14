"""Character designer tasks - generate character assets."""
import os
import sys
import inspect

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.celery_app import celery_app
from common.task_editors import record_task_editor_if_success
from common.global_style import (
    ensure_global_style_generated,
)
from common.style_anchor_generation import ensure_llm_style_anchor
from common.visual_input_preflight import validate_visual_inputs
from common.visual_style import (
    ASSET_CHARACTER,
    STYLE_MODE_LLM,
    load_visual_style_config,
)
from services.char_designer.core.character_manifest import write_character_image_manifest
from services.char_designer.core.character_manifest import CharacterManifestError
from services.char_designer.core.artifact_contract import CharacterImageArtifactError
from services.char_designer.core.resume_contract import (
    publish_character_stage_checkpoint,
    validate_character_stage_checkpoint,
    validate_character_stage_checkpoint_partial,
)
from services.char_designer.core.asset_failure_report import load_failure_report, write_failure_report


def _task_image_model(task_id: str):
    """读取创建任务时选择的生图模型；查询失败时保留服务默认值。"""
    try:
        from sqlmodel import Session, select
        from common.task_models import NovelTask
        from common.platform_db import platform_engine
        with Session(platform_engine) as session:
            task = session.exec(select(NovelTask).where(NovelTask.task_id == task_id)).first()
            return task.image_model if task else None
    except Exception as exc:
        print(f"⚠️  读取任务生图模型失败，使用 char_designer 默认配置: {exc}")
        return None


def _resolve_image_model(model_name: str):
    """把 novel_tasks.image_model 归一成 (provider, model)。
    识别不到的模型名（含 None/空/脏数据）强制回退到 gpt：provider=gpt、
    model=None，下游 generate_calm.py 的 ``args.model or "gpt-image-2"`` 会落到 gpt-image-2。"""
    if model_name == "wan2.5-t2i-preview":
        return "wan", model_name
    if model_name and model_name.startswith("qwen-image"):
        return "qwen", model_name
    if model_name and model_name.startswith("gpt-image"):
        return "gpt", model_name
    return "gpt", None

try:
    from common.usage_tracker import tracker as _usage_tracker
except ImportError:
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
from common.progress_utils import (
    StageLeaseLostError,
    complete_stage,
    claim_stage_for_validated_resume,
    ensure_stage_lease_owned,
    fail_pending_stage_claim,
    fail_stage,
    start_stage,
    update_task_progress,
    wait_for_stage_completion,
)
PROGRESS_ENABLED = True


def _call_with_optional_partial(function, task_id: str, *, allow_partial: bool):
    """Call new partial-aware helpers while keeping direct legacy test hooks."""
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "allow_partial" in parameters:
        return function(task_id, allow_partial=allow_partial)
    return function(task_id)

@celery_app.task(
    bind=True,
    name="services.char_designer.tasks.regenerate_characters",
    time_limit=900,             # 硬超时 15 分钟
    soft_time_limit=840,        # 软超时 14 分钟
    autoretry_for=(Exception,), # 失败/超时后自动重试整个 task（新进程、新连接，规避卡死的 DashScope 节点）
    max_retries=2,              # 最多再试 2 次（含原始执行共 3 次）
    retry_backoff=10,           # 退避起始 10s（10s → 20s → 40s）
    retry_backoff_max=120,
    retry_jitter=True,
)
def regenerate_characters(
    self,
    task_id: str,
    node_id: str = None,
    material_id: str = None,
    new_prompt: str = None,
    force: bool = True,
    session_no: int = None,
    user_id: str = None,
    user_name: str = None,
    model_name: str = None,
):
    """
    重生成单张角色图片。

    Args:
        task_id:     任务 ID
        node_id:     OSS 格式 node_id
        material_id: 文件名，如 Liang_1-1-4_calm.png
        new_prompt:  新的 positive_prompt
        force:       True=强制重生成
        session_no:  操作序号（None=自动分配）
        model_name:  图片模型（gpt-image-2 / qwen-image-plus / wan2.5-t2i-preview），None=系统默认

    Returns:
        dict，包含 total/success/failed/session_no/items/errors
    """
    print(f"🔄 Starting character regenerate for task_id={task_id}, material_id={material_id} "
          f"(尝试 {self.request.retries + 1}/{self.max_retries + 1})")

    # 参数校验：业务错误不重试，直接返回 failed
    if not node_id or not material_id:
        return {"total": 0, "success": 0, "failed": 1, "items": [],
                "errors": ["node_id 和 material_id 不能为空"]}

    if _usage_tracker:
        _usage_tracker.set_context(task_id, "char_designer")

    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)

        from core.regenerate import regenerate_character as _regen

        def _publish_main_done(main_item, main_session_no, emotion_pending):
            """calm/主图一出结果就发布中间态，让 API 立即按 calm 自身结果返回 success/failed，
            不用等 emotion 联动跑完。emotion 仍在本任务内继续跑，最终结果照常覆盖。"""
            self.update_state(
                state="PROGRESS",
                meta={
                    "phase": "main_done",
                    "session_no": main_session_no,
                    "items": [main_item],
                    "total": 1,
                    "success": 1 if main_item.get("status") == "success" else 0,
                    "failed": 1 if main_item.get("status") == "failed" else 0,
                    "errors": [main_item["error"]] if main_item.get("error") else [],
                    "emotion_pending": emotion_pending,
                },
            )
            print(f"📤 已发布主图中间态: {main_item.get('material_id')} "
                  f"status={main_item.get('status')} emotion_pending={emotion_pending}")

        result = _regen(
            task_id=task_id,
            node_id=node_id,
            material_id=material_id,
            new_prompt=new_prompt,
            force=force,
            session_no=session_no,
            user_id=user_id,
            user_name=user_name,
            model_name=model_name,
            on_main_done=_publish_main_done,
        )
        # _regen 正常返回（不论 success/failed）：直接返回，业务级失败不触发重试
        # 这里的失败通常是参数错误、找不到记录、OSS 文件不存在等永久性错误，重试无意义
        if result.get("failed", 0) > 0:
            print(f"❌ Character regenerate 业务失败: {result.get('errors')}")
        else:
            print(f"✅ Character regenerate completed: {result['success']}/{result['total']} success")
        if _usage_tracker:
            _usage_tracker.flush()
        try:
            record_task_editor_if_success(result, task_id, user_id, user_name)
        except Exception as exc:
            print(f"⚠️  角色重生成成功，但记录编辑者失败: {exc}")
        return result

    except Exception as e:
        # 兜底：包括 SoftTimeLimitExceeded、网络异常、SDK 超时、connection error 等
        # 这些是"真异常"——通常是临时性故障，autoretry_for=(Exception,) 会触发重试
        # 重试耗尽后异常依然抛出 → Celery 标记 task 为 FAILURE，API 状态查询返回 failed
        print(f"❌ Character regenerate exception (尝试 {self.request.retries + 1}): {str(e)}")
        import traceback
        traceback.print_exc()
        if _usage_tracker:
            _usage_tracker.flush()
        raise


@celery_app.task(
    name="services.char_designer.tasks.generate_characters",
    acks_late=True,             # 子进程丢失/超时后消息重新投递，避免任务人间蒸发
)
def generate_characters(task_id: str):
    """
    Generate character assets for a given task_id.

    Args:
        task_id: Task ID (game_project identifier)

    Returns:
        str: task_id for next step in chain

    Raises:
        ValueError: If task_id is invalid
        Exception: If character generation fails
    """
    if not task_id or not isinstance(task_id, str):
        raise ValueError(f"Invalid task_id: {task_id}")

    try:
        claim_action = claim_stage_for_validated_resume(
            task_id,
            "char_designer",
            completion_validator=lambda: validate_character_stage_checkpoint(task_id),
        )
    except Exception as exc:
        fail_pending_stage_claim(
            task_id,
            "char_designer",
            20,
            str(exc),
        )
        raise
    if claim_action == "complete":
        print(f"✅ char_designer 断点合同完整，跳过全部 Provider 调用: {task_id}")
        return task_id
    if claim_action == "wait":
        wait_for_stage_completion(task_id, 'char_designer')
        if not validate_character_stage_checkpoint_partial(task_id):
            raise RuntimeError(
                f"char_designer 等待中的执行已结束，但断点合同仍不完整: {task_id}"
            )
        return task_id

    print(f"🎨 Starting character generation for task_id: {task_id}")

    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)

        from main import _is_cg_identity_only_task, run_pipeline
        from core.generate_cg_identity_references import (
            validate_required_identity_references,
        )

        if _usage_tracker:
            _usage_tracker.set_context(task_id, "char_designer")

        style_config = load_visual_style_config(task_id, fetch_remote=True)
        llm_anchor_route = style_config.get("mode") == STYLE_MODE_LLM

        # LLM 路线固定使用 global_style + 自动 character Anchor；
        # custom 路线仍由用户确认的 Anchor 驱动。
        preflight = (
            validate_visual_inputs(task_id, stage="char_designer")
            if llm_anchor_route
            else None
        )

        update_task_progress(task_id, progress=30)

        image_provider, image_model = _resolve_image_model(_task_image_model(task_id))
        resolved_image_model = image_model or "gpt-image-2"
        if llm_anchor_route:
            global_style = ensure_global_style_generated(task_id)
            if preflight.needs_character_anchor:
                character_anchor = ensure_llm_style_anchor(
                    task_id,
                    ASSET_CHARACTER,
                    global_style,
                    resolved_image_model,
                    provider_scope="char_designer",
                    usage_service="char_designer",
                )
                print(
                    f"🎨 prepare_character_style 完成: "
                    f"global_style={global_style} anchor={character_anchor}"
                )
            else:
                print("🎨 小说无人物及 CG 身份参考需求，跳过 character_anchor")

        if llm_anchor_route and not preflight.needs_character_anchor:
            ensure_stage_lease_owned(task_id, 'char_designer')
            publish_character_stage_checkpoint(task_id)
            complete_stage(task_id, 'char_designer', 40)
            print(f"✅ 无人物视觉资产需求，char_designer 已安全跳过: {task_id}")
            return task_id

        result = run_pipeline(
            task_id=task_id,
            model=image_model,
            image_provider=image_provider,
        )
        if result is None:
            raise Exception("run_pipeline returned None — check char_designer logs for details")

        # CG-only 与普通角色任务都可能包含 required identity reference。
        # 单个身份参考缺失时由 scene_painter 跳过受影响 CG；系统级读取错误
        # 仍然抛出，避免把整个输入合同故障伪装成 partial。
        try:
            validate_required_identity_references(task_id)
        except CharacterImageArtifactError as exc:
            report = write_failure_report(
                task_id,
                extra_items=[
                    {
                        "stage": "generate_cg_identity",
                        "step": "generate_cg_identity",
                        "asset_id": "__required_identity__",
                        "asset_type": "cg_identity",
                        "status": "missing",
                        "reason_code": "required_reference_missing",
                        "attempts": 0,
                        "max_attempts": max(
                            1,
                            int(os.getenv("VISUAL_ASSET_MAX_FAILED_ATTEMPTS", "8")),
                        ),
                        "paths": [],
                        "last_error": str(exc),
                    }
                ],
            )
            print(
                f"⚠️ [char_designer][PARTIAL] required CG identity 不完整，"
                f"继续下游；report={report}"
            )

        # manifest 是普通人物资源的最终公共合同；CG-only 任务则以已校验的
        # identity reference 为权威产物。发布任一合同前必须仍是当前 owner。
        ensure_stage_lease_owned(task_id, 'char_designer')

        identity_only = _is_cg_identity_only_task(task_id)
        manifest_path = None
        if identity_only:
            print("📦 CG-only 任务没有普通人物立绘，跳过普通人物 Manifest")
        else:
            # 角色资产生产者负责输出公共合同。manifest 构造、原子写入、读回或
            # 本地文件校验失败时不得把 char_designer 标记为完成。
            try:
                manifest_path = _call_with_optional_partial(
                    write_character_image_manifest,
                    task_id,
                    allow_partial=True,
                )
                print(f"📦 Public character manifest generated: {manifest_path}")
            except CharacterManifestError as exc:
                # 部分角色没有 calm 基础图时，严格 v1 Manifest 无法完整发布；
                # game_builder 直接扫描实际文件，缺失项由报告交付人工补图。
                print(f"⚠️ [char_designer][PARTIAL] Manifest 不完整，继续: {exc}")

        # 产物镜像到 OSS（本地保留，OSS 多存一份；失败不影响主流程）
        ensure_stage_lease_owned(task_id, 'char_designer')
        oss_required = (
            os.getenv("VISUAL_OUTPUT_DURABILITY", "pvc").strip().lower()
            == "oss_required"
        )
        try:
            from common.output_mirror import mirror_outputs_with_policy
            mirror_subpaths = [
                "images/references",
                "style_anchors",
                "reports/char_designer_asset_failures.json",
                "story_tree.json",
            ]
            if oss_required:
                mirror_subpaths.append(".resume_sources/char_designer")
            if manifest_path is not None:
                mirror_subpaths.extend(
                    ["images/characters", "manifests/character_image_manifest.json"]
                )
            mirror_outputs_with_policy(task_id, mirror_subpaths)
        except Exception as _e:
            if oss_required:
                raise
            print(f"⚠️  char_designer 产物镜像 OSS 失败（PVC 模式不影响主流程）: {_e}")

        report = load_failure_report(task_id)
        _call_with_optional_partial(
            publish_character_stage_checkpoint,
            task_id,
            allow_partial=True,
        )
        complete_stage(task_id, 'char_designer', 40)

        if report.get("status") == "partial":
            print(
                f"⚠️ [char_designer][PARTIAL] Character assets completed with missing files: "
                f"task_id={task_id} report={report}"
            )
        else:
            print(f"✅ Character assets generated successfully for task_id: {task_id}")
        return task_id

    except StageLeaseLostError:
        # 旧 owner 已被永久 fence；不能写 FAILED 覆盖接管者的状态。
        print(f"⚠️  Character generation lost stage lease: task_id={task_id}")
        raise
    except Exception as e:
        fail_stage(task_id, 'char_designer', 20, str(e))
        print(f"❌ Character generation failed: {str(e)}")
        import traceback
        traceback.print_exc()
        raise Exception(f"Character generation failed for task_id {task_id}: {str(e)}")


@celery_app.task(name="services.char_designer.tasks.redraw_character")
def redraw_character_task(
    task_id: str,
    char: str,
    combination: str = None,
    redraw_mode: str = "calm_only",
    emotions: list = None,
    shots: list = None,
    force: bool = False,
):
    """
    Redraw a single character by char identifier (speaker_id or name).

    Args:
        task_id: Task ID
        char: Character identifier (speaker_id or name)
        combination: Target variant combination string, e.g. "TangSan-1-2-3"; None = base combination
        redraw_mode: One of calm_only | all_except_base_calm | all_from_anchor | variant_emotions | emotions_only
        emotions: For emotions_only mode, list of emotions to generate; None = all emotions
        shots: Shot types to produce; subset of LS/MS/CS. None = all three. LS is the AI-generated
            source; MS/CS are cropped from LS to match the batch pipeline.
        force: True=先删除当前模式冲突的本地 LS/MS/CS 与未扩展恢复源，再执行完整
            后处理；False=复用已有图片，仅补生成缺失文件
    """
    if not task_id or not isinstance(task_id, str):
        raise ValueError(f"Invalid task_id: {task_id}")
    if not char and not combination:
        raise ValueError("Must provide char or combination parameter")

    print(
        f"🎨 Starting character redraw for task_id: {task_id}, char: {char}, "
        f"mode: {redraw_mode}, force: {force}",
        flush=True,
    )

    try:
        start_stage(task_id, 'char_redraw', 10)

        if _usage_tracker:
            _usage_tracker.set_context(task_id, "char_designer")

        import sys
        import os
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)

        from core.redraw_character import redraw_single_character

        result = redraw_single_character(
            task_id=task_id,
            char=char,
            combination=combination,
            redraw_mode=redraw_mode,
            emotions=emotions,
            shots=shots,
            force=force,
        )

        if result.success:
            complete_stage(task_id, 'char_redraw', 20)
            print(f"✅ Character redraw completed for {result.speaker_id}")
            if _usage_tracker:
                _usage_tracker.flush()
            return {
                "task_id": task_id,
                "speaker_id": result.speaker_id,
                "success": True,
                "calm_generated": result.calm_generated,
                "emotion_generated": result.emotion_generated,
                "emotion_count": result.emotion_count,
                "shot_count": result.shot_count,
                "calm_image_path": result.calm_image_path,
            }
        else:
            fail_stage(task_id, 'char_redraw', 10, result.error_message)
            raise Exception(f"Character redraw failed: {result.error_message}")

    except Exception as e:
        fail_stage(task_id, 'char_redraw', 10, str(e))
        print(f"❌ Character redraw error: {str(e)}")
        import traceback
        traceback.print_exc()
        if _usage_tracker:
            _usage_tracker.flush()
        raise Exception(f"Character redraw failed for task_id {task_id}: {str(e)}")
