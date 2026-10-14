#!/usr/bin/env python3
"""手动运行 Story Planner 并把产物交给下游模块的正式测试入口。

这个脚本和 ``run_chapter_branch_smoke.py`` 的定位不同：它使用真实数据库，
生成可供其它模块读取的真实 ``task_id``，但不投递下游 Celery 任务。当前
``_trigger_downstream`` 只负责完成 Planner 阶段，因此会保留该调用，让数据库
中的 ``story_planner_status`` 正确变为 COMPLETED。

Usage:
    python run_test.py
"""

import os
import sys
import time
import uuid
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

if not os.getenv("STORAGE_PATH"):
    os.environ["STORAGE_PATH"] = str(project_root / "storage")

# ============================================================
# 配置区域 —— 按需修改
# ============================================================

# 小说文件路径（绝对路径或相对于项目根目录）
NOVEL_FILE = project_root / "storage" / "raw_novels" / "莲花楼-青龙.txt"

# 任务 ID：留空则自动生成，填入已有 ID 则复用（会覆盖 DB 记录）
TASK_ID = ""

# 输出语言：当前平台上传接口支持 zh-CN | en-US
OUTPUT_LANGUAGE = "zh-CN"

# 画面风格：ink_chinese | cyberpunk | japanese | dark_european | modern_realism
ART_STYLE = "ink_chinese"

# 剧情模式：original | multi | chapter_branch
STORY_MODE = "original"

# 下游模块读取 novel_tasks 时需要这些生产契约字段。Planner 文本调用仍由
# STORY_PLANNER_LLM_* 环境变量统一配置为 GPT-5.6-SOL 中转模型。
IMAGE_MODEL = "gpt-image-2"
AUDIO_MODEL = "speech-2.8-hd"
RESOLUTION = "1080p"
GAMEPLAY = "interactive_story"

# ============================================================


def build_manual_task_values(
    *,
    novel_filename: str,
    project_name: str,
    output_language: str,
    art_style: str,
    story_mode: str,
) -> dict:
    """Build the task-row values required by the manual Planner handoff.

    The row is put in PROCESSING before the synchronous call. This deliberately
    does not claim the Redis stage lease (the script is a single-process manual
    runner), but it keeps the database state compatible with downstream services.
    """

    from common.task_models import (
        AudioModel,
        InfoConfirmStage,
        ImageModel,
        Resolution,
        TaskStatus,
        TextModel,
    )

    return {
        "novel_filename": novel_filename,
        "project_name": project_name,
        "output_language": output_language,
        "art_style": art_style,
        "story_mode": story_mode,
        "gameplay": GAMEPLAY,
        "text_model": TextModel.DEFAULT,
        "image_model": IMAGE_MODEL or ImageModel.DEFAULT,
        "audio_model": AUDIO_MODEL or AudioModel.DEFAULT,
        "resolution": RESOLUTION or Resolution.DEFAULT,
        "upload_type": "single",
        "info_confirm_stage": InfoConfirmStage.CONFIRMED,
        "story_planner_status": TaskStatus.PROCESSING,
        "info_extractor_status": TaskStatus.PENDING,
        "sound_arranger_status": TaskStatus.PENDING,
        "char_designer_status": TaskStatus.PENDING,
        "audio_producer_status": TaskStatus.PENDING,
        "scene_painter_status": TaskStatus.PENDING,
        "game_builder_status": TaskStatus.PENDING,
        "overall_status": TaskStatus.PROCESSING,
        "progress_percentage": 18,
        "error_message": None,
        "celery_chain_id": None,
        "completed_at": None,
        "generated_project_name": None,
        "is_published": False,
    }


def _story_tree_view(story_tree_data) -> dict:
    """Select one usable locale snapshot from a legacy or bundled Story Tree."""

    if not isinstance(story_tree_data, dict):
        return {}
    if isinstance(story_tree_data.get("nodes"), dict):
        return story_tree_data
    preferred = story_tree_data.get("en-US")
    if isinstance(preferred, dict) and isinstance(preferred.get("nodes"), dict):
        return preferred
    for value in story_tree_data.values():
        if isinstance(value, dict) and isinstance(value.get("nodes"), dict):
            return value
    return {}


def _task_field(task, name: str, default=None):
    if isinstance(task, dict):
        return task.get(name, default)
    return getattr(task, name, default)


def validate_manual_planner_handoff(
    *,
    story_mode: str,
    task,
    story_tree_data,
    md_names,
    summary,
    chapter_branch_plan,
    worldview=None,
) -> list[str]:
    """Validate the minimum database contract handed to other modules."""

    from common.task_models import ImageModel, TaskStatus

    errors: list[str] = []
    if _task_field(task, "story_planner_status") != TaskStatus.COMPLETED:
        errors.append(
            "story_planner_status 未完成: "
            f"{_task_field(task, 'story_planner_status')!r}"
        )
    image_model = str(_task_field(task, "image_model") or "").strip()
    if image_model not in ImageModel.VALID_MODELS:
        errors.append(
            "image_model 无效或为空: "
            f"{image_model or '(empty)'}"
        )

    tree = _story_tree_view(story_tree_data)
    node_map = tree.get("nodes") if isinstance(tree, dict) else None
    if not isinstance(node_map, dict) or not node_map:
        errors.append("story_tree_json.nodes 缺失或为空")
        node_ids: set[str] = set()
    else:
        node_ids = {str(node_id) for node_id in node_map if str(node_id).strip()}

    md_id_set = {
        str(name).strip()
        for name in (md_names or [])
        if str(name).strip()
    }
    if not md_id_set:
        errors.append("story_tree_md 没有任何章节正文")
    if node_ids and md_id_set != node_ids:
        missing = sorted(node_ids - md_id_set)
        extra = sorted(md_id_set - node_ids)
        errors.append(
            "story_tree_md 与 story_tree_json.nodes 不一致: "
            f"missing={missing}, extra={extra}"
        )

    summary_status = _task_field(summary, "status")
    summary_data = _task_field(summary, "data", {})
    if (
        summary_status != 1
        or not isinstance(summary_data, dict)
        or not all(str(summary_data.get(field) or "").strip() for field in (
            "outline", "background", "one_liner"
        ))
    ):
        errors.append("story_summary 未完成或字段不完整")

    if story_mode == "chapter_branch":
        worldview_status = _task_field(worldview, "status")
        worldview_data = _task_field(worldview, "data", {})
        if (
            worldview_status != 1
            or not isinstance(worldview_data, dict)
            or not isinstance(worldview_data.get("worldview"), dict)
            or not str(worldview_data.get("char_cards") or "").strip()
            or not str(worldview_data.get("relationship_map") or "").strip()
            or not str(worldview_data.get("world_rules") or "").strip()
            or not str(worldview_data.get("narrative_arc") or "").strip()
        ):
            errors.append("world_bible 未完成或字段不完整")
        if not isinstance(chapter_branch_plan, dict):
            errors.append("chapter_branch_plan 缺失")
        elif not isinstance(chapter_branch_plan.get("nodes"), dict):
            errors.append("chapter_branch_plan.nodes 缺失或格式错误")
        quality_gate = tree.get("quality_gate") if isinstance(tree, dict) else None
        if not isinstance(quality_gate, dict):
            errors.append("story_tree.json quality_gate 缺失")
        elif quality_gate.get("passed") is not True:
            errors.append("story_tree.json quality_gate 未通过")

    return errors


def _reset_existing_task(existing, values: dict) -> None:
    """Reset a reusable, non-active test row without touching other task IDs."""

    from common.task_models import TaskStatus

    if bool(getattr(existing, "is_published", False)):
        raise RuntimeError(
            f"拒绝覆盖已发布 task_id={existing.task_id}；请使用新的 task_id"
        )
    if bool(getattr(existing, "is_deleted", False)) or bool(
        getattr(existing, "is_purged", False)
    ):
        raise RuntimeError(
            f"拒绝覆盖已删除/已清除 task_id={existing.task_id}；请使用新的 task_id"
        )
    active_status_fields = (
        "story_planner_status",
        "info_extractor_status",
        "sound_arranger_status",
        "char_designer_status",
        "audio_producer_status",
        "scene_painter_status",
        "game_builder_status",
    )
    if any(
        getattr(existing, field, None) == TaskStatus.PROCESSING
        for field in active_status_fields
    ):
        raise RuntimeError(
            f"拒绝覆盖仍有阶段运行中的 task_id={existing.task_id}；请等待或停止旧任务"
        )

    for field, value in values.items():
        setattr(existing, field, value)


def _print_handoff_report(task_id: str, story_mode: str) -> list[str]:
    """Print and validate the database contract handed to downstream modules."""

    from common.task_models import NovelTask, TaskStatus
    from common.platform_db import platform_engine
    from services.story_planner.table.models import (
        ChapterBranchPlan,
        StorySummary,
        StoryTreeJson,
        StoryTreeMd,
        Worldview,
        engine as business_engine,
    )
    from sqlmodel import Session, select

    try:
        with Session(platform_engine) as session:
            task = session.exec(
                select(NovelTask).where(NovelTask.task_id == task_id)
            ).first()
            story_tree = session.exec(
                select(StoryTreeJson).where(StoryTreeJson.task_id == task_id)
            ).first()
            md_rows = list(
                session.exec(
                    select(StoryTreeMd).where(StoryTreeMd.task_id == task_id)
                ).all()
            )
        with Session(business_engine) as session:
            summary = session.exec(
                select(StorySummary).where(StorySummary.task_id == task_id)
            ).first()
            chapter_branch_plan = session.exec(
                select(ChapterBranchPlan).where(ChapterBranchPlan.task_id == task_id)
            ).first()
            worldview = session.exec(
                select(Worldview).where(Worldview.task_id == task_id)
            ).first()
    except Exception as exc:
        error = f"数据库交接验收查询失败: {exc}"
        print(f"❌ {error}")
        return [error]

    story_tree_data = getattr(story_tree, "data", None) if story_tree else None
    plan_data = (
        getattr(chapter_branch_plan, "topology_json", None)
        if chapter_branch_plan
        else None
    )
    errors = validate_manual_planner_handoff(
        story_mode=story_mode,
        task=task,
        story_tree_data=story_tree_data,
        md_names=[getattr(row, "name", "") for row in md_rows],
        summary=summary,
        chapter_branch_plan=plan_data,
        worldview=worldview,
    )

    task_status = _task_field(task, "story_planner_status")
    overall_status = _task_field(task, "overall_status")
    quality_tree = _story_tree_view(story_tree_data)
    quality_gate = quality_tree.get("quality_gate") if quality_tree else None
    print("\n" + "-" * 60)
    print("Planner 数据库交接验收")
    print(f"  task_id              : {task_id}")
    print(
        "  story_planner_status : "
        f"{TaskStatus.get_description(task_status)} ({task_status!r})"
    )
    print(
        "  overall_status       : "
        f"{TaskStatus.get_description(overall_status)} ({overall_status!r})"
    )
    print(f"  story_tree_json      : {'已存在' if story_tree else '缺失'}")
    print(f"  story_tree_md        : {len(md_rows)} 条")
    print(f"  story_summary        : {'已存在' if summary else '缺失'}")
    print(
        "  chapter_branch_plan  : "
        f"{'已存在' if chapter_branch_plan else '不适用/缺失'}"
    )
    if isinstance(quality_gate, dict):
        print(
            "  quality_gate         : "
            f"{'通过' if quality_gate.get('passed') else '未通过'} "
            f"(score={quality_gate.get('score', '?')})"
        )
    if errors:
        print("  交接结论             : ❌ 不通过")
        for error in errors:
            print(f"    - {error}")
    else:
        print("  交接结论             : ✅ 通过，可将 task_id 交给下游模块")
    print("-" * 60)
    return errors


def main():
    from common.task_models import NovelTask, ArtStyle, StoryMode, TaskStatus
    from common.platform_db import platform_engine as engine  # novel_tasks 已迁平台库
    from sqlmodel import Session, select

    # 参数校验
    supported_languages = ["zh-CN", "en-US"]
    if OUTPUT_LANGUAGE not in supported_languages:
        print(f"❌ OUTPUT_LANGUAGE 不支持: {OUTPUT_LANGUAGE}，可选: {supported_languages}")
        sys.exit(1)

    if ART_STYLE not in ArtStyle.VALID_STYLES:
        print(f"❌ ART_STYLE 不支持: {ART_STYLE}，可选: {ArtStyle.VALID_STYLES}")
        sys.exit(1)

    if STORY_MODE not in StoryMode.VALID_MODES:
        print(f"❌ STORY_MODE 不支持: {STORY_MODE}，可选: {StoryMode.VALID_MODES}")
        sys.exit(1)

    novel_path = Path(NOVEL_FILE)
    if not novel_path.exists():
        print(f"❌ 小说文件不存在: {novel_path}")
        sys.exit(1)

    # 确定 task_id
    task_id = TASK_ID.strip() if TASK_ID.strip() else f"test_{uuid.uuid4().hex[:8]}"

    # 把小说文件复制到 raw_novels/<task_id>.txt（如果路径不同）
    storage_dir = Path(os.environ["STORAGE_PATH"]) / "raw_novels"
    storage_dir.mkdir(parents=True, exist_ok=True)
    target_path = storage_dir / f"{task_id}.txt"
    if novel_path.resolve() != target_path.resolve():
        import shutil
        shutil.copy2(novel_path, target_path)
        print(f"📄 已复制小说文件 → {target_path}")

    task_values = build_manual_task_values(
        novel_filename=novel_path.name,
        project_name=novel_path.stem,
        output_language=OUTPUT_LANGUAGE,
        art_style=ART_STYLE,
        story_mode=STORY_MODE,
    )

    # 写入 / 覆盖 novel_tasks 记录。该脚本不申请 Redis lease，但将数据库状态
    # 置为 PROCESSING，保证手动生成结束后可交给 info_extractor/视觉模块。
    with Session(engine) as session:
        existing = session.exec(
            select(NovelTask).where(NovelTask.task_id == task_id)
        ).first()
        if existing:
            existing.novel_filename = novel_path.name
            _reset_existing_task(existing, task_values)
            session.add(existing)
        else:
            session.add(NovelTask(task_id=task_id, **task_values))
        session.commit()
    print(f"✅ novel_tasks 记录已写入")

    # 手动脚本只跳过 Redis stage lease；不屏蔽当前 _trigger_downstream，因为它
    # 现在只负责将 Planner 阶段安全写为 COMPLETED，不会投递下游 Celery 任务。
    import services.story_planner.tasks as sp_tasks
    # Redis stage lease 用于 Celery/K8s 多 Worker 防重复执行；本脚本是单进程
    # 直接调用，既没有并发抢占，也不需要故障接管，因此本地测试跳过该锁。
    sp_tasks.is_stage_already_running = lambda task_id, stage: False

    print("=" * 60)
    print(f"Story Planner 正式数据库测试（仅 Planner，不启动下游）")
    print(f"  task_id       : {task_id}")
    print(f"  novel_file    : {novel_path.name}")
    print(f"  output_language: {OUTPUT_LANGUAGE}")
    print(f"  art_style     : {ART_STYLE}")
    print(f"  story_mode    : {STORY_MODE}")
    print("=" * 60)

    start_time = time.perf_counter()
    try:
        result = sp_tasks.parse_novel_to_draft(task_id)
    except Exception:
        print("\n❌ Planner 执行异常，开始读取数据库失败状态和已有产物...")
        _print_handoff_report(task_id, STORY_MODE)
        raise
    elapsed = time.perf_counter() - start_time

    handoff_errors = _print_handoff_report(task_id, STORY_MODE)
    if handoff_errors:
        raise RuntimeError(
            "Planner 执行返回成功，但数据库交接验收失败："
            + "；".join(handoff_errors)
        )

    print("\n" + "=" * 60)
    print(f"执行结果: {result}")
    mins, secs = divmod(elapsed, 60)
    print(f"⏱  总耗时: {elapsed:.2f} 秒 ({int(mins)} 分 {secs:.1f} 秒)")
    print("=" * 60)


if __name__ == "__main__":
    main()
