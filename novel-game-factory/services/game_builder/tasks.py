"""Game builder tasks - assemble final game package."""
import sys
import os
import json
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Add project root to path for imports
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from common.celery_app import celery_app
from common.config import OUTPUTS_PATH, BUILT_GAMES_PATH
from common.local_runtime import local_pipeline_enabled, oss_disabled
from services.game_builder.converter import FinalConverter
from services.game_builder.converter import (
    load_local_visual_plot_data,
    materialize_local_visual_sources,
)
from services.game_builder.converter import normalize_story_tree
from services.game_builder.cleanup_storage import cleanup_old_outputs

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


def _cleanup_expired_local_outputs(current_task_id: str) -> None:
    """Apply the online builder's seven-day local-output retention policy.

    Cleanup is deliberately best-effort: an unavailable Celery inspector or a
    transient filesystem error must never prevent a build that can be restored
    from OSS.  The current task is always protected, and the cleanup helper
    also protects active/reserved/scheduled builder tasks when available.
    """
    if str(os.getenv("GAME_BUILDER_OUTPUT_CLEANUP", "1")).strip().lower() in {
        "0", "false", "no", "off",
    }:
        print("  本地 outputs 清理已由 GAME_BUILDER_OUTPUT_CLEANUP 关闭")
        return

    raw_days = os.getenv("GAME_BUILDER_OUTPUT_RETENTION_DAYS", "7")
    try:
        days = float(raw_days)
    except (TypeError, ValueError):
        days = 7.0
    if days <= 0:
        print(f"⚠️ GAME_BUILDER_OUTPUT_RETENTION_DAYS={raw_days!r} 无效，跳过本地清理")
        return

    try:
        result = cleanup_old_outputs(
            Path(OUTPUTS_PATH).parent,
            days=days,
            protected_task_ids={str(current_task_id)},
            include_active_tasks=True,
        )
        deleted = result.get("deleted") or []
        candidates = result.get("candidates") or []
        print(
            f"  本地 outputs 保留策略: {days:g} 天；"
            f"扫描 {len(candidates)} 个过期条目，已删除 {len(deleted)} 个"
        )
        for warning in result.get("warnings") or []:
            print(f"  ⚠️ outputs 清理警告: {warning}")
    except Exception as exc:
        print(f"⚠️ 本地 outputs 清理失败，继续构建（OSS 可恢复）: {exc}")

@celery_app.task(name="services.game_builder.tasks.build_game_package")
def build_game_package(task_id: str = None, domain: str = None):
    """
    Build final game package from all generated assets.

    Args:
        task_id: 游戏项目ID (game_project identifier)
        domain: 域名配置 (optional, 默认从环境变量读取或使用 localhost)
               可以是单个域名: "example.com"
               或多个域名: "example.com www.example.com"

    Returns:
        dict: Status and build info (最后一个任务可以返回详细信息)

    Raises:
        ValueError: If task_id is invalid
        Exception: If build fails
    """
    # 参数验证
    if not task_id:
        # 尝试从环境变量获取
        task_id = os.getenv('GAME_PROJECT')
        if not task_id:
            raise ValueError("task_id is required but not provided and GAME_PROJECT env var is not set")

    if not isinstance(task_id, str):
        raise ValueError(f"Invalid task_id type: {type(task_id)}, expected str")

    # 先清理超过保留期的本地 outputs；当前 task 与活动任务会被保护。
    _cleanup_expired_local_outputs(task_id)

    if is_stage_already_running(task_id, 'game_builder'):
        wait_for_stage_completion(task_id, 'game_builder')
        return task_id

    print(f"🎮 Starting game package build for task_id: {task_id}")

    try:
        game_project = task_id
        domain = domain or os.getenv('DOMAIN', 'localhost')

        # 使用统一的配置路径
        script_dir = Path(__file__).resolve().parent
        source_dir = Path(OUTPUTS_PATH) / game_project
        template_dir = script_dir / "game_template" / "shijuexiaoshuo"
        output_dir = Path(BUILT_GAMES_PATH) / game_project

        print(f"开始构建游戏包...")
        print(f"  游戏项目ID: {game_project}")
        print(f"  源目录: {source_dir}")
        print(f"  模板目录: {template_dir}")
        print(f"  输出目录: {output_dir}")
        print(f"  域名: {domain}")

        # PVC 本地 outputs 是本次构建的权威输入：只要对应任务目录已经存在，
        # 就直接使用本地素材，不能再从 OSS 拉取覆盖。这样运维/上游在 PVC
        # 中修正素材后重跑 game_builder 时，不会被旧 OSS 版本覆盖。
        _prepare_source_assets(task_id, source_dir, replace_existing=True)

        # 验证源目录是否存在
        if not source_dir.exists():
            fail_stage(task_id, 'game_builder', 80, f"Source directory not found: {source_dir}")
            raise FileNotFoundError(f"Source directory not found: {source_dir}")

        if local_pipeline_enabled():
            story_tree_path = source_dir / "story_tree.json"
            if story_tree_path.is_file():
                try:
                    with open(story_tree_path, "r", encoding="utf-8") as f:
                        local_story_tree = normalize_story_tree(json.load(f))[0]
                    local_plot_data = load_local_visual_plot_data(source_dir, task_id)
                    local_sources = materialize_local_visual_sources(
                        source_dir,
                        local_plot_data,
                        local_story_tree,
                    )
                    print(
                        "  本地视觉模式：已物化 "
                        f"{len(local_sources)} 个无音频章节字幕源目录"
                    )
                except Exception as exc:
                    print(f"⚠️ 本地视觉章节素材物化失败: {exc}")

        # 创建转换器实例
        converter = FinalConverter(
            source_dir=str(source_dir),
            template_dir=str(template_dir),
            output_dir=str(output_dir),
            domain=domain,
            task_id=task_id,
            enable_oss_upload=not oss_disabled(),
            delete_local_assets=not oss_disabled(),
        )

        # 1. 转换所有节点生成游戏代码
        # 注：ultimate 数据由上游 info_extractor 整合并入库/落地，game_builder 直接读取
        print("步骤 1/2: 转换节点生成游戏代码...")
        converter.create_per_node_projects()

        # 进度更新：转换完成
        update_task_progress(task_id, progress=90)

        # 2. 生成 Nginx 配置
        print("步骤 2/2: 生成 Nginx 配置...")
        converter.generate_nginx_config_only()

        # 进度更新：完成
        complete_stage(task_id, 'game_builder', 100)

        print("✅ 游戏包构建完成!")

        return {
            "status": "success",
            "task_id": game_project,
            "game_project": game_project,
            "build_path": str(output_dir),
            "domain": domain,
            "message": "Game package built successfully"
        }

    except Exception as e:
        fail_stage(task_id, 'game_builder', 80, str(e))
        print(f"❌ 构建失败: {e}")
        import traceback
        traceback.print_exc()
        # 抛出异常，标记任务失败
        raise Exception(f"Game package build failed for task_id {task_id}: {str(e)}")

def _paths_for(task_id: str):
    script_dir = Path(__file__).resolve().parent
    return (
        Path(OUTPUTS_PATH) / task_id,
        script_dir / "game_template" / "shijuexiaoshuo",
        Path(BUILT_GAMES_PATH) / task_id,
    )


def _pull_oss_assets(task_id: str, replace_existing: bool = False) -> None:
    """OSS 有则覆盖本地，失败退化为纯本地构建。"""
    if oss_disabled():
        print("  OSS_DISABLED=1，跳过 OSS 素材恢复")
        return
    try:
        from common.output_mirror import fetch_task_from_oss
        print(
            f"  从 OSS 拉取素材: "
            f"{fetch_task_from_oss(task_id, replace_existing=replace_existing)} 个文件"
        )
    except Exception as e:
        print(f"⚠️  从 OSS 拉取素材失败（改用本地素材）: {e}")


def _prepare_source_assets(
    task_id: str,
    source_dir: Path,
    replace_existing: bool = False,
) -> bool:
    """准备 game_builder 输入，并返回是否使用了本地任务目录。

    PVC 上已有 ``outputs/<task_id>`` 时，本地目录是本次构建的权威输入，
    完全跳过 OSS 下载，避免旧 OSS 素材覆盖本地修正版。只有本地目录不
    存在时才从 OSS 恢复；恢复失败仍交给后续的本地目录校验报错。
    """
    if source_dir.is_dir():
        print(f"  检测到本地 PVC 素材目录: {source_dir}")
        print("  ⏭️ 跳过 OSS 拉取，直接使用本地素材构建")
        return True

    print(f"  本地素材目录不存在，尝试从 OSS 恢复: {source_dir}")
    _pull_oss_assets(task_id, replace_existing=replace_existing)
    return False


@celery_app.task(name="services.game_builder.tasks.rebuild_online_package")
def rebuild_online_package(task_id: str, art_style: str = None, domain: str = None):
    """按选定 art_style 重跑在线包。

    和 build_game_package 的区别：这是任务完成后用户在导出页手动触发的重建，
    不碰 progress / stage —— 任务早已 COMPLETED，把进度打回 90 会让前端倒退。
    """
    if not task_id:
        raise ValueError("task_id is required")

    source_dir, template_dir, output_dir = _paths_for(task_id)
    domain = domain or os.getenv('DOMAIN', 'localhost')

    _cleanup_expired_local_outputs(task_id)

    # 在线重建与首次构建使用同一条本地优先规则；本地目录存在时不拉 OSS。
    _prepare_source_assets(task_id, source_dir, replace_existing=True)
    if not source_dir.exists():
        raise FileNotFoundError(f"Source directory not found: {source_dir}")

    if art_style:
        import json
        story_tree_path = source_dir / "story_tree.json"
        if not story_tree_path.exists():
            raise FileNotFoundError(f"story_tree.json 不存在: {story_tree_path}")
        with open(story_tree_path, "r", encoding="utf-8") as f:
            story_tree = json.load(f)
        normalized_tree, layers = normalize_story_tree(story_tree)
        if layers:
            # 保留语言 wrapper；重建风格写入默认英语层，并同步已有语言层
            # 以便后续导出/读取不会因外层字段位置不同而丢失。
            default_lang = "en-US" if "en-US" in layers else next(iter(layers))
            layers[default_lang]["art_style"] = art_style
            for lang, layer in layers.items():
                if isinstance(layer, dict):
                    layer.setdefault("art_style", art_style)
        else:
            story_tree["art_style"] = art_style
        with open(story_tree_path, "w", encoding="utf-8") as f:
            json.dump(story_tree, f, ensure_ascii=False, indent=2)

    converter = FinalConverter(
        source_dir=str(source_dir),
        template_dir=str(template_dir),
        output_dir=str(output_dir),
        domain=domain,
        task_id=task_id,
        enable_oss_upload=not oss_disabled(),
        delete_local_assets=not oss_disabled(),
    )
    converter.create_per_node_projects()
    converter.generate_nginx_config_only()

    print(f"✅ 在线包重建完成: {output_dir}")
    return {
        "status": "success",
        "task_id": task_id,
        "output_dir": str(output_dir),
        "art_style": art_style,
        "domain": domain,
    }


@celery_app.task(name="services.game_builder.tasks.export_offline_package")
def export_offline_package(task_id: str):
    """导出单机包并打成 zip。art_style 取 story_tree.json 里的现值。"""
    if not task_id:
        raise ValueError("task_id is required")

    import json
    import shutil
    import zipfile

    source_dir, template_dir, _ = _paths_for(task_id)
    output_dir = Path(BUILT_GAMES_PATH) / f"{task_id}_steam"

    _pull_oss_assets(task_id)
    if not source_dir.exists():
        raise FileNotFoundError(f"Source directory not found: {source_dir}")

    story_tree_path = source_dir / "story_tree.json"
    if not story_tree_path.exists():
        raise FileNotFoundError(f"story_tree.json 不存在: {story_tree_path}")
    with open(story_tree_path, "r", encoding="utf-8") as f:
        raw_story_tree = json.load(f)
    normalized_tree, _layers = normalize_story_tree(raw_story_tree)
    art_style = normalized_tree.get("art_style", "modern_realism")

    from services.game_builder.converter_steam import FinalConverter as SteamConverter
    converter = SteamConverter(
        source_dir=str(source_dir),
        template_dir=str(template_dir),
        output_dir=str(output_dir),
        art_style=art_style,
    )
    converter.create_per_node_projects()
    converter.create_run_scripts()

    # 先写 .tmp 再 rename：下载方按 .zip 存在与否判断就绪，半个包不能顶着最终名字
    archive_tmp = output_dir.parent / f"{output_dir.name}.zip.tmp"
    archive_final = output_dir.parent / f"{output_dir.name}.zip"
    try:
        with zipfile.ZipFile(archive_tmp, 'w', zipfile.ZIP_STORED, allowZip64=True) as zf:
            for p in output_dir.rglob('*'):
                if p.is_file():
                    zf.write(p, p.relative_to(output_dir.parent))
        archive_tmp.replace(archive_final)
    except Exception:
        archive_tmp.unlink(missing_ok=True)
        raise

    shutil.rmtree(output_dir, ignore_errors=True)

    print(f"✅ 单机包导出完成: {archive_final}")
    return {
        "status": "success",
        "task_id": task_id,
        "output_dir": str(archive_final),
        "art_style": art_style,
    }


if __name__ == "__main__":
    # ⚠️ 仅用于开发测试！生产环境通过 Celery worker 调度
    # 直接运行此文件进行快速测试和调试
    print("=" * 60)
    print("⚠️  测试模式：直接运行任务（非 Celery 调度）")
    print("=" * 60)

    result = build_game_package()

    print("\n" + "=" * 60)
    print("最终结果:")
    print("=" * 60)
    print(result)
