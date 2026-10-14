"""Scene painter tasks - generate scene assets."""
import os
import sys
import time
import json
import inspect
from pathlib import Path

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.celery_app import celery_app
from common.global_style import (
    ensure_global_style_generated,
    get_current_global_style,
)
from common.task_editors import record_task_editor_if_success
from common.style_anchor_generation import (
    ensure_llm_style_anchor,
    generate_anchor,
)
from common.visual_input_preflight import validate_visual_inputs
from common.task_models import TaskStatus
from services.scene_painter.core.finalize_character_asset_names import (
    finalize_character_asset_names,
    synchronize_replayed_character_assets,
    validate_character_asset_name_finalization,
)
from services.scene_painter.core.asset_failure_report import (
    load_failure_report,
    write_failure_report,
)
from services.scene_painter.core.resume_contract import (
    publish_scene_stage_checkpoint,
    validate_scene_stage_checkpoint,
)
from common.visual_style import (
    ANCHOR_STATUS_FAILED,
    ANCHOR_STATUS_GENERATING,
    ANCHOR_STATUS_READY,
    ASSET_CHARACTER,
    ASSET_SCENE,
    STYLE_MODE_CUSTOM,
    STYLE_MODE_LLM,
    load_visual_style_config,
    save_visual_style_config,
    style_anchor_path,
    visual_style_timestamp,
)


def _task_image_model(task_id: str):
    """读取创建任务时选择的生图模型；查询失败时保留服务环境默认值。"""
    try:
        from sqlmodel import Session, select
        from common.task_models import NovelTask
        from common.platform_db import platform_engine
        with Session(platform_engine) as session:
            task = session.exec(select(NovelTask).where(NovelTask.task_id == task_id)).first()
            return task.image_model if task else None
    except Exception as exc:
        print(f"⚠️  读取任务生图模型失败，使用 scene_painter 默认配置: {exc}")
        return None


def _resolve_api_model(model_name: str):
    """归一成 scene_painter 的 API_MODEL 值。
    识别不到的模型名（含 None/空/脏数据）强制回退到 gpt-image-2，避免脏值
    被原样传给生图接口。"""
    if model_name == "wan2.5-t2i-preview":
        return model_name
    if model_name and (model_name.startswith("qwen-image") or model_name.startswith("gpt-image")):
        return model_name
    return "gpt-image-2"

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
from common.progress_utils import (
    StageLeaseLostError,
    complete_stage,
    claim_stage_for_validated_resume,
    ensure_stage_lease_owned,
    fail_pending_stage_claim,
    fail_stage,
    get_task_progress,
    start_stage,
    update_task_progress,
    wait_for_stage_completion,
)
PROGRESS_ENABLED = True


def _call_with_optional_partial(function, task_id: str, *, allow_partial: bool):
    """Use partial-aware helpers without breaking direct legacy test hooks."""
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "allow_partial" in parameters:
        return function(task_id, allow_partial=allow_partial)
    return function(task_id)


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _wait_for_character_owned_global_style(task_id: str) -> str:
    """Wait for char_designer's normal style publication without blocking images.

    Direct scene-only replays remain supported: if no character execution starts
    during the bounded pending grace, the same idempotent locked ensure function
    is allowed to publish the historical/fallback value.
    """
    timeout = _positive_int_env("SCENE_GLOBAL_STYLE_WAIT_SECONDS", 1200)
    pending_grace = _positive_int_env(
        "SCENE_GLOBAL_STYLE_PENDING_GRACE_SECONDS", 60
    )
    poll = _positive_int_env("SCENE_GLOBAL_STYLE_POLL_SECONDS", 2)
    allow_direct_fallback = _env_flag("SCENE_ALLOW_GLOBAL_STYLE_FALLBACK")
    started = time.monotonic()
    last_status = None

    while time.monotonic() - started < timeout:
        ensure_stage_lease_owned(task_id, "scene_painter")
        try:
            style = get_current_global_style(task_id)
        except Exception:
            style = ""
        if style:
            print(
                f"🎨 scene_painter 复用 char_designer 已发布的 global_style: "
                f"task_id={task_id}"
            )
            return style

        progress = get_task_progress(task_id) or {}
        status = (progress.get("stages") or {}).get("char_designer")
        if status != last_status:
            print(
                f"⏳ scene_painter 等待 global_style 发布: "
                f"task_id={task_id} char_designer_status={status}"
            )
            last_status = status
        if status == TaskStatus.FAILED:
            raise RuntimeError(
                "char_designer 在发布 global_style 前失败，scene_painter 停止生图"
            )
        if status == TaskStatus.COMPLETED:
            print(
                "⚠️ char_designer 已完成但缺少当前版本 global_style，"
                "按历史/直接重跑兼容路径幂等生成"
            )
            return ensure_global_style_generated(task_id)
        if (
            allow_direct_fallback
            and status in {None, TaskStatus.PENDING}
            and time.monotonic() - started >= pending_grace
        ):
            print(
                "⚠️ 未观测到 char_designer 启动，按直接重跑 scene_painter "
                "兼容路径幂等生成 global_style"
            )
            return ensure_global_style_generated(task_id)
        time.sleep(poll)

    raise TimeoutError(
        f"scene_painter 等待 global_style 超时: task_id={task_id} timeout={timeout}s"
    )


def _wait_for_character_assets(task_id: str) -> None:
    """Join the character branch immediately before CG and final renaming."""
    timeout = _positive_int_env("SCENE_CHARACTER_JOIN_TIMEOUT_SECONDS", 172800)
    print(f"🤝 CG 依赖汇合：等待 char_designer 完成 task_id={task_id}")
    wait_for_stage_completion(
        task_id,
        "char_designer",
        poll_interval=2,
        timeout=timeout,
    )
    ensure_stage_lease_owned(task_id, "scene_painter")
    task_root = os.path.join(os.getenv("STORAGE_PATH", "/storage"), "outputs", task_id)
    report_path = os.path.join(task_root, "reports", "char_designer_asset_failures.json")
    report = {}
    try:
        with open(report_path, "r", encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, UnicodeError, ValueError):
        report = {}

    from services.scene_painter.core.artifact_contract import validate_image_file

    valid_files = []
    for relative_dir in ("images/characters", "images/references"):
        directory = os.path.join(task_root, relative_dir)
        if os.path.isdir(directory):
            valid_files.extend(
                path
                for path in Path(directory).glob("*.png")
                if validate_image_file(path)
            )
    if report.get("status") == "partial":
        print(
            f"⚠️ [scene_painter][PARTIAL_JOIN] char_designer 存在缺失资产，"
            f"可用参考={len(valid_files)}，继续生成可用 CG: task_id={task_id}"
        )
        return
    if valid_files:
        print(f"✅ CG 依赖汇合完成：发现 {len(valid_files)} 个有效人物参考 task_id={task_id}")
        return

    # No-character tasks are valid only when neither normal prompts nor
    # required structured-CG identities exist.
    try:
        from sqlalchemy import text
        from common.platform_db import platform_engine

        with platform_engine.connect() as connection:
            prompt_row = connection.execute(
                text("SELECT data FROM character_prompts WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id},
            ).first()
            cg_row = connection.execute(
                text("SELECT data FROM cg WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id},
            ).first()
        prompts = prompt_row[0] if prompt_row else None
        cg_data = cg_row[0] if cg_row else None
        if isinstance(prompts, str):
            prompts = json.loads(prompts)
        if isinstance(cg_data, str):
            cg_data = json.loads(cg_data)
        required_identity = any(
            isinstance(spec, dict)
            and any(
                isinstance(item, dict)
                and item.get("role") == "character"
                and item.get("required")
                for item in spec.get("reference_requirements") or []
            )
            for spec in (cg_data or {}).values()
        ) if isinstance(cg_data, dict) else False
        if not prompts and not required_identity:
            print(f"✅ CG 依赖汇合完成：任务无人物视觉资产 task_id={task_id}")
            return
    except Exception as exc:
        raise RuntimeError(f"读取人物资产合同失败: {exc}") from exc
    raise RuntimeError("char_designer 状态已完成，但没有任何可用人物资产")


def _update_style_anchor_job_error(
    task_id: str,
    asset_kind: str,
    job_id: str,
    error: str,
    *,
    terminal: bool,
) -> None:
    """Persist job failure only when this job is still the active generation."""
    from sqlmodel import Session, select
    from common.platform_db import platform_engine
    from common.task_models import NovelTask

    with Session(platform_engine) as session:
        task = session.exec(
            select(NovelTask)
            .where(NovelTask.task_id == task_id)
            .with_for_update()
        ).first()
        if task is None:
            return
        config = load_visual_style_config(task_id, fetch_remote=False)
        section = config.get(asset_kind) or {}
        if section.get("job_id") != job_id:
            return
        section["status"] = ANCHOR_STATUS_FAILED if terminal else ANCHOR_STATUS_GENERATING
        section["error"] = str(error or "Anchor 生成失败")
        section["updated_at"] = visual_style_timestamp()
        save_visual_style_config(task_id, config, mirror=False)
        session.commit()


@celery_app.task(
    bind=True,
    name="services.scene_painter.tasks.generate_style_anchor",
    time_limit=900,
    soft_time_limit=840,
    max_retries=2,
)
def generate_style_anchor_task(
    self,
    task_id: str,
    asset_kind: str,
    global_style: str,
    image_model: str,
):
    """Generate one user-reviewable style Anchor with the project Provider."""
    if asset_kind not in {ASSET_CHARACTER, ASSET_SCENE}:
        raise ValueError("asset_kind 只支持 character / scene")

    job_id = str(self.request.id or "").strip()
    if not job_id:
        raise ValueError("缺少 Celery job_id")

    final_target = style_anchor_path(task_id, asset_kind)
    job_dir = final_target.parent / ".jobs"
    job_dir.mkdir(parents=True, exist_ok=True)
    generated_target = job_dir / f"{job_id}.png"

    try:
        metadata, prompt = generate_anchor(
            task_id,
            asset_kind,
            global_style,
            image_model,
            generated_target,
        )

        from sqlmodel import Session, select
        from common.platform_db import platform_engine
        from common.task_models import NovelTask

        accepted = False
        with Session(platform_engine) as session:
            task = session.exec(
                select(NovelTask)
                .where(NovelTask.task_id == task_id)
                .with_for_update()
            ).first()
            if task is None:
                raise ValueError(f"任务不存在: {task_id}")

            config = load_visual_style_config(task_id, fetch_remote=False)
            section = config.get(asset_kind) or {}
            if (
                config.get("mode") == STYLE_MODE_CUSTOM
                and section.get("job_id") == job_id
                and str(section.get("global_style") or "").strip() == str(global_style or "").strip()
            ):
                final_target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(generated_target, final_target)
                section.update(
                    source="generated",
                    confirmed=False,
                    stale=False,
                    sha256=metadata["sha256"],
                    model=metadata["model"],
                    prompt=prompt,
                    status=ANCHOR_STATUS_READY,
                    error="",
                    updated_at=visual_style_timestamp(),
                )
                save_visual_style_config(task_id, config, mirror=False)
                accepted = True
            session.commit()

        if not accepted:
            return {
                "task_id": task_id,
                "asset_kind": asset_kind,
                "job_id": job_id,
                "status": "obsolete",
            }
        return {
            "task_id": task_id,
            "asset_kind": asset_kind,
            "job_id": job_id,
            "status": ANCHOR_STATUS_READY,
            **metadata,
            "prompt": prompt,
        }
    except Exception as exc:
        if self.request.retries < self.max_retries:
            _update_style_anchor_job_error(
                task_id,
                asset_kind,
                job_id,
                f"生成失败，正在重试: {exc}",
                terminal=False,
            )
            raise self.retry(exc=exc, countdown=min(10 * (2 ** self.request.retries), 120))
        _update_style_anchor_job_error(
            task_id,
            asset_kind,
            job_id,
            str(exc),
            terminal=True,
        )
        raise
    finally:
        generated_target.unlink(missing_ok=True)


@celery_app.task(
    bind=True,
    name="services.scene_painter.tasks.regenerate_scene_materials",
    time_limit=900,
    soft_time_limit=840,
    autoretry_for=(Exception,),
    max_retries=2,
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
)
def regenerate_scene_materials(
    self,
    task_id: str,
    material_type: str = "scene",
    scene_ids: list = None,
    background_node_ids: list = None,
    new_prompts: dict = None,
    force: bool = False,
    session_no: int = None,
    user_id: str = None,
    user_name: str = None,
    model_name: str = None,
):
    """
    重生成单个 scene_painter 素材。

    Args:
        task_id: 任务 ID
        material_type: "scene" | "background"（暂不支持 "all"）
        scene_ids: scene_id 列表，必须包含 1 个元素（material_type=scene 时）
        background_node_ids: node_id 列表，必须包含 1 个元素（material_type=background 时）
        new_prompts: {material_id: new_prompt}，可选
        force: True=跳过存在性检查强制重生成
        session_no: 操作序号（None=自动分配）
        model_name: 图片模型（gpt-image-2 / wan2.5-t2i-preview / qwen-image-plus），None=环境默认

    Returns:
        dict，包含 total/success/failed/session_no/items/errors
    """
    print(f"🔄 Starting scene materials regenerate for task_id: {task_id}, type: {material_type} "
          f"(尝试 {self.request.retries + 1}/{self.max_retries + 1})")

    if _usage_tracker:
        _usage_tracker.set_context(task_id, "scene_painter")

    # 参数校验：业务错误不重试
    if material_type == "scene":
        if not scene_ids:
            return {"total": 0, "success": 0, "failed": 1, "items": [], "errors": ["scene_ids 不能为空"]}
        material_ids = [scene_ids[0]]
    elif material_type == "background":
        if not background_node_ids:
            return {"total": 0, "success": 0, "failed": 1, "items": [], "errors": ["background_node_ids 不能为空"]}
        material_ids = [background_node_ids[0]]
    else:
        return {"total": 0, "success": 0, "failed": 1, "items": [], "errors": [f"不支持的 material_type: {material_type}"]}

    try:
        from services.scene_painter.core.regenerate import regenerate_scene_materials as _regen

        result = _regen(
            task_id=task_id,
            material_ids=material_ids,
            new_prompts=new_prompts,
            force=force,
            session_no=session_no,
            user_id=user_id,
            user_name=user_name,
            model_name=model_name,
        )
        # _regen 正常返回：业务级失败不重试（参数错误、找不到记录等永久性错误，重试无意义）
        if result.get("failed", 0) > 0:
            print(f"❌ Scene regenerate 业务失败: {result.get('errors')}")
        else:
            print(f"✅ Scene regenerate completed: {result['success']}/{result['total']} success")
        if _usage_tracker:
            _usage_tracker.flush()
        try:
            record_task_editor_if_success(result, task_id, user_id, user_name)
        except Exception as exc:
            print(f"⚠️  场景重生成成功，但记录编辑者失败: {exc}")
        return result

    except Exception as e:
        # 真异常（超时/网络/SDK 错误）：autoretry_for=(Exception,) 会触发 Celery 重试
        print(f"❌ Scene regenerate exception (尝试 {self.request.retries + 1}): {str(e)}")
        import traceback
        traceback.print_exc()
        if _usage_tracker:
            _usage_tracker.flush()
        raise


@celery_app.task(
    name="services.scene_painter.tasks.generate_scenes",
    acks_late=True,
)
def generate_scenes(task_id: str):
    """
    Generate scene/background assets for a given task_id.

    Args:
        task_id: Task ID (game_project identifier)

    Returns:
        str: task_id for next step in chain

    Raises:
        ValueError: If task_id is invalid
        Exception: If scene generation fails
    """
    # 参数验证
    if not task_id or not isinstance(task_id, str):
        raise ValueError(f"Invalid task_id: {task_id}")

    try:
        claim_action = claim_stage_for_validated_resume(
            task_id,
            "scene_painter",
            completion_validator=lambda: validate_scene_stage_checkpoint(task_id),
        )
    except Exception as exc:
        # No stage owner exists when the claim itself fails, so fail_stage cannot
        # pass the fence.  Preserve a terminal failure instead of leaving a
        # previously PENDING stage forever.
        fail_pending_stage_claim(
            task_id,
            "scene_painter",
            60,
            str(exc),
        )
        raise
    if claim_action == "complete":
        print(f"✅ scene_painter 断点合同完整，跳过全部 Provider 调用: {task_id}")
        return task_id
    if claim_action == "wait":
        wait_for_stage_completion(task_id, 'scene_painter')
        if not validate_scene_stage_checkpoint(task_id, allow_partial=True):
            raise RuntimeError(
                f"scene_painter 等待中的执行已结束，但断点合同仍不完整: {task_id}"
            )
        return task_id

    print(f"🖼️  Starting scene generation for task_id: {task_id}")

    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)

        from main_to_db import main

        if _usage_tracker:
            _usage_tracker.set_context(task_id, "scene_painter")

        style_config = load_visual_style_config(task_id, fetch_remote=True)
        llm_anchor_route = style_config.get("mode") == STYLE_MODE_LLM

        # LLM 路线固定在 scene_anchor 或正式图片 Provider 调用前
        # 校验上游数据；custom 路线保持用户确认 Anchor 逻辑。
        preflight = (
            validate_visual_inputs(task_id, stage="scene_painter")
            if llm_anchor_route
            else None
        )

        update_task_progress(task_id, progress=70)

        api_model = _resolve_api_model(_task_image_model(task_id))
        if llm_anchor_route:
            global_style = _wait_for_character_owned_global_style(task_id)
            scene_anchor = ensure_llm_style_anchor(
                task_id,
                ASSET_SCENE,
                global_style,
                api_model,
                provider_scope="scene_painter",
                usage_service="scene_painter",
            )
            print(
                f"🎨 prepare_scene_style 完成: global_style={global_style} "
                f"scene_anchor={scene_anchor} (independent_from_character_anchor)"
            )

        previous_model = os.environ.get("API_MODEL")
        os.environ["API_MODEL"] = api_model
        try:
            # 基础场景与封面只依赖场景提示词/global_style/scene_anchor，
            # 与人物立绘同时执行；此阶段绝不读取人物图片。
            scene_result = main(task_id=task_id, mode="scene_assets")
            if scene_result == 1:
                raise RuntimeError(
                    f"scene_painter scene-assets pipeline returned non-zero exit code: {scene_result}"
                )
            if scene_result == 2:
                print(
                    f"⚠️ [scene_painter][PARTIAL] 基础场景/封面存在缺失，继续等待人物并生成可用 CG: {task_id}"
                )
            print(f"✅ 基础场景/封面阶段完成，即将等待人物资产生成 CG: {task_id}")

            _wait_for_character_assets(task_id)

            char_report = {}
            try:
                char_report_path = (
                    Path(os.getenv("STORAGE_PATH", "/storage"))
                    / "outputs"
                    / task_id
                    / "reports"
                    / "char_designer_asset_failures.json"
                )
                char_report = json.loads(
                    char_report_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError) as exc:
                print(f"⚠️ 读取 char_designer 部分报告失败，继续按可用文件处理: {exc}")
            # Final name synchronization is an asset-level operation.  Missing
            # pairs are reported and skipped only when char_designer explicitly
            # published a partial report; conflicting files still raise.
            allow_partial = char_report.get("status") == "partial"

            # 只在 CG 真正需要消费人物图时校验旧 ID 资产和最终
            # 改名合同，避免该检查阻断前面的场景并行生成。
            rename_map = _call_with_optional_partial(
                synchronize_replayed_character_assets,
                task_id,
                allow_partial=allow_partial,
            )
            try:
                validate_character_asset_name_finalization(
                    task_id,
                    allow_equivalent_duplicates=True,
                    allow_partial=allow_partial,
                )
            except TypeError:
                validate_character_asset_name_finalization(
                    task_id,
                    allow_equivalent_duplicates=True,
                )
            if rename_map:
                print(
                    f"🧭 CG 人物资源预检通过: "
                    f"{len(rename_map)} 个 ID 映射"
                )

            cg_result = main(task_id=task_id, mode="cg")
            if cg_result == 1:
                raise RuntimeError(
                    f"scene_painter CG pipeline returned non-zero exit code: {cg_result}"
                )
            if cg_result == 2:
                print(
                    f"⚠️ [scene_painter][PARTIAL] 部分 CG 缺失，继续发布可用场景资源: {task_id}"
                )
        finally:
            if previous_model is None:
                os.environ.pop("API_MODEL", None)
            else:
                os.environ["API_MODEL"] = previous_model

        scene_report = load_failure_report(task_id)
        allow_partial_scene = scene_report.get("status") == "partial"
        allow_partial_finalization = allow_partial or allow_partial_scene
        from core.artifact_contract import validate_scene_output_contract
        if not allow_partial_scene:
            validate_scene_output_contract(task_id)

        # 场景和 CG 已经完成对旧名称人物图的消费，现在才统一发布新人物拼音
        # 文件名，并机械同步 Manifest、CG 文件候选和本地调试记录。
        finalization = _call_with_optional_partial(
            finalize_character_asset_names,
            task_id,
            allow_partial=allow_partial_finalization,
        )
        if finalization.rename_map:
            print(
                "🏷️  人物资源最终改名完成: "
                f"renamed={len(finalization.renamed_files)} "
                f"json={len(finalization.updated_json_files)} "
                f"cg_updated={finalization.cg_references_updated}"
            )

        report_path = None
        task_output_root = (
            Path(os.getenv("STORAGE_PATH", "/storage"))
            / "outputs"
            / task_id
        )
        if task_output_root.exists():
            report_path = write_failure_report(task_id)
            print(f"⚠️ [scene_painter][REPORT] task_id={task_id} report={report_path}")

        # 产物镜像到 OSS（本地保留，OSS 多存一份；失败不影响主流程）
        ensure_stage_lease_owned(task_id, 'scene_painter')
        oss_required = (
            os.getenv("VISUAL_OUTPUT_DURABILITY", "pvc").strip().lower()
            == "oss_required"
        )
        try:
            from common.output_mirror import mirror_outputs_with_policy
            mirror_outputs_with_policy(
                task_id,
                [
                    "images/scenes",
                    "images/backgrounds",
                    "images/cg",
                    "reports/scene_painter_asset_failures.json",
                    "style_anchors",
                    "story_tree.json",
                ],
            )
        except Exception as _e:
            if oss_required:
                raise
            print(f"⚠️  scene_painter 产物镜像 OSS 失败（PVC 模式不影响主流程）: {_e}")

        _call_with_optional_partial(
            publish_scene_stage_checkpoint,
            task_id,
            allow_partial=allow_partial_scene,
        )
        if allow_partial_scene:
            print(
                f"⚠️ [scene_painter][PARTIAL] 场景阶段 checkpoint 已发布为部分合同，"
                f"后续恢复仍会逐资产补跑: task_id={task_id}"
            )
        complete_stage(task_id, 'scene_painter', 80)

        print(f"✅ Scene painter task completed for task_id: {task_id}")
        return task_id

    except StageLeaseLostError:
        # 旧 owner 已被永久 fence；不能写 FAILED 覆盖接管者的状态。
        print(f"⚠️  Scene generation lost stage lease: task_id={task_id}")
        raise
    except Exception as e:
        fail_stage(task_id, 'scene_painter', 60, str(e))
        print(f"❌ Scene generation error: {str(e)}")
        import traceback
        traceback.print_exc()
        # 抛出异常，中断任务链（并行 chord 下：该分支失败则 game_builder 不触发）
        raise Exception(f"Scene generation failed for task_id {task_id}: {str(e)}")
