"""
人物立绘顶部透明画布扩展（按身高）。

当前身高映射标准（人物原图高度占扩展后最终画布高度的比例）：
  - 身高 <= 0cm：视为无效数据，不扩展画布。
  - 身高 1cm <= height < 100cm：从 60% 线性映射到 65%。
      ratio = 0.60 + (height_cm - 1) / 99 * (0.65 - 0.60)
  - 身高 100cm <= height < 155cm：从 65% 线性映射到 80%。
      ratio = 0.65 + (height_cm - 100) / 55 * (0.80 - 0.65)
  - 身高 155cm <= height < 190cm：从 80% 线性映射到 100%。
      ratio = 0.80 + (height_cm - 155) / 35 * (1.00 - 0.80)
  - 身高 >= 190cm：按 100% 显示，不增加顶部透明画布。

其中 ratio = 人物原图高度 / 最终画布高度；画布只向人物上方扩展，人物底部
位置保持不变。最终画布高度按 round(人物原图高度 / ratio) 计算。

此步骤应在以下顺序中运行：
  Step 5: generate_calm.py        → 生成每角色基础组合 calm
  Step 6: generate_variant_calm.py → 图生图生成其他变体组合 calm
  Step 7: generate_emotion.py     → 生成情绪变体
  Step 8: ★ expand_canvas.py      ← 所有 calm + 情绪 LS 图都生成完后，统一扩展顶部透明像素

为什么放在最后：
- generate_variant_calm 走图生图时拿到的 base 必须是没扩过的原图，否则空白画布会被吃进 base。
- generate_emotion 同理基于未扩展的 calm 做表情迁移。
- 等所有图都生成完后再统一扩，对每张图独立操作互不影响。

用法：
  python char_designer/core/expand_canvas.py --task-id <task_id>
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

current_dir = Path(__file__).resolve().parent
char_designer_dir = current_dir.parent
workspace_root = char_designer_dir.parent
project_root = workspace_root.parent
for path in (str(workspace_root), str(char_designer_dir), str(current_dir), str(project_root)):
    if path not in sys.path:
        sys.path.insert(0, path)


def add_common_config_search_paths() -> None:
    # 本仓库自带 common/config.py 时，直接用它并返回，绝不去扫描兄弟目录副本。
    # 否则 candidates 命中的外部仓库 config.py 会被 insert(0) 抢占 sys.path，
    # 而部分旧副本的 DATABASE_URL 未对密码里的 '@' 做 %40 编码，导致连库失败。
    if (project_root / "common" / "config.py").exists():
        candidate_str = str(project_root)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)
        return

    candidates = [
        workspace_root,
        workspace_root.parent,
        workspace_root.parent / "novel-game-factory",
    ]
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
    load_dotenv(dotenv_path=Path(project_root) / "novel-game-factory" / ".env")
    load_dotenv(dotenv_path=workspace_root / ".env")
except ImportError:
    pass

from common.config import OUTPUTS_PATH
from common.platform_db import platform_engine
from common.visual_checkpoint import (
    canonical_digest,
    file_sha256,
    mark_failed as checkpoint_mark_failed,
    mark_running as checkpoint_mark_running,
    mark_success as checkpoint_mark_success,
    reusable_or_adopt_existing,
)
from services.char_designer.core.artifact_contract import (
    atomic_copy_image,
    atomic_image_output,
    validate_image_file,
)

try:
    from sqlalchemy import text
    HAS_SQLALCHEMY = True
except ImportError:
    HAS_SQLALCHEMY = False

# 文件名合同仍复用生图阶段；画布比例和像素写入由本模块负责。
try:
    # Prefer the canonical package name so the conversion cache initialized in
    # this subprocess is the same cache used by ``_combination_to_filename``.
    from services.char_designer.core.generate_calm import (
        _combination_to_filename,
        _init_conversion_cache,
    )
except ImportError:  # pragma: no cover - fallback for direct script invocation
    try:
        from char_designer.core.generate_calm import (  # type: ignore
            _combination_to_filename,
            _init_conversion_cache,
        )
    except ImportError:
        from generate_calm import (  # type: ignore
            _combination_to_filename,
            _init_conversion_cache,
        )

try:
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    HAS_PIL = True
except ImportError:
    Image = None  # type: ignore[assignment]
    PngInfo = None  # type: ignore[assignment]
    HAS_PIL = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]


def _report_progress(progress: Optional[ProgressCallback], message: str) -> None:
    if progress is not None:
        try:
            progress(message)
        except Exception:
            # Progress output must never interrupt image processing.
            logger.debug("画布扩展进度回调失败", exc_info=True)


class CharacterCanvasPartialError(RuntimeError):
    """Only some derived character canvases failed; parent may continue."""

EMOTIONS = ['calm', 'happy', 'sad', 'angry', 'fearful', 'disgusted', 'surprised']

EXPANDED_CANVAS_MARKER_KEY = "expanded_canvas_v"
EXPANDED_CANVAS_MARKER_VALUE = "2"
EXPANDED_CANVAS_HEIGHT_KEY = "expanded_canvas_height_cm"
EXPANDED_CANVAS_SOURCE_SHA_KEY = "expanded_canvas_source_sha256"
EXPAND_CANVAS_GENERATOR_VERSION = "character-expand-canvas-v2-resume-source"
MAX_CANVAS_HEIGHT_CM = 190.0
SMALL_CHARACTER_CUTOFF_CM = 100.0
MID_HEIGHT_ANCHOR_CM = 155.0
MIN_CHARACTER_HEIGHT_CM = 1.0
MIN_CHARACTER_RATIO = 0.60
ONE_METER_CHARACTER_RATIO = 0.65
MID_HEIGHT_CHARACTER_RATIO = 0.80


def _height_to_ratio(height_cm: float) -> float:
    """把身高映射为人物占最终画布高度的比例。"""
    if height_cm <= 0:
        return 0.0
    height_cm = float(height_cm)
    if height_cm < SMALL_CHARACTER_CUTOFF_CM:
        progress = (max(MIN_CHARACTER_HEIGHT_CM, height_cm) - MIN_CHARACTER_HEIGHT_CM) / (
            SMALL_CHARACTER_CUTOFF_CM - MIN_CHARACTER_HEIGHT_CM
        )
        return MIN_CHARACTER_RATIO + progress * (
            ONE_METER_CHARACTER_RATIO - MIN_CHARACTER_RATIO
        )
    if height_cm < MID_HEIGHT_ANCHOR_CM:
        progress = (
            height_cm - SMALL_CHARACTER_CUTOFF_CM
        ) / (MID_HEIGHT_ANCHOR_CM - SMALL_CHARACTER_CUTOFF_CM)
        return ONE_METER_CHARACTER_RATIO + progress * (
            MID_HEIGHT_CHARACTER_RATIO - ONE_METER_CHARACTER_RATIO
        )
    if height_cm >= MAX_CANVAS_HEIGHT_CM:
        return 1.0
    progress = (
        height_cm - MID_HEIGHT_ANCHOR_CM
    ) / (MAX_CANVAS_HEIGHT_CM - MID_HEIGHT_ANCHOR_CM)
    return MID_HEIGHT_CHARACTER_RATIO + progress * (
        1.0 - MID_HEIGHT_CHARACTER_RATIO
    )


def _is_canvas_expanded(image: "Image.Image") -> bool:
    return image.info.get(EXPANDED_CANVAS_MARKER_KEY) == EXPANDED_CANVAS_MARKER_VALUE


def _expand_top_by_height(
    image_path: Path,
    height_cm: float,
    *,
    output_path: Optional[Path] = None,
    source_sha256: str = "",
) -> bool:
    """按身高比例在图片顶部增加透明像素，并写入幂等标记。"""
    if not HAS_PIL:
        return False
    try:
        ratio = _height_to_ratio(height_cm)
        if ratio <= 0 or ratio >= 1.0:
            return False
        with Image.open(image_path) as image:
            image.load()
            if output_path is None and _is_canvas_expanded(image):
                return False
            if image.mode != "RGBA":
                image = image.convert("RGBA")
            width, height = image.size
            new_height = max(height + 1, int(round(height / ratio)))
            canvas = Image.new("RGBA", (width, new_height), (0, 0, 0, 0))
            canvas.paste(image, (0, new_height - height))
            pnginfo = PngInfo()
            pnginfo.add_text(
                EXPANDED_CANVAS_MARKER_KEY,
                EXPANDED_CANVAS_MARKER_VALUE,
            )
            pnginfo.add_text(EXPANDED_CANVAS_HEIGHT_KEY, f"{height_cm:.4f}")
            if source_sha256:
                pnginfo.add_text(EXPANDED_CANVAS_SOURCE_SHA_KEY, source_sha256)
            target = output_path or image_path
            with atomic_image_output(target) as temporary_path:
                canvas.save(temporary_path, format="PNG", pnginfo=pnginfo)
        return True
    except Exception as exc:
        logger.warning("扩展画布失败 %s: %s", image_path, exc)
        return False


def load_height_map(task_id: str) -> Dict[str, str]:
    """
    返回 {calm_filename: height_cm_str}，filename 与 generate_calm._combination_to_filename 输出一致。

    重点：character_prompts 表里每个 combination 都有自己的 height（来自该 combination
    对应的 whole_body_variant 的 height 字段），所以每个变体取的是自身变体身高，不会
    被基础组合的身高覆盖。
    """
    # The filename helper consults the task's authoritative
    # ``name_mappings.original_name.conversion`` cache.  Initialize it here as
    # this step is launched as an independent subprocess after generation;
    # otherwise Unicode IDs such as ``角色（别名）`` would be transliterated a
    # second time and the height entry would not match the generated file.
    _init_conversion_cache(task_id)

    if not HAS_SQLALCHEMY:
        logger.error("sqlalchemy 未安装，无法读取身高")
        return {}

    with platform_engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM character_prompts WHERE task_id = :tid"),
            {"tid": task_id},
        ).fetchone()
    if not row:
        logger.error(f"character_prompts 中未找到 task_id={task_id}")
        return {}

    height_map: Dict[str, str] = {}
    for item in row[0]:
        # 优先用 combination 作为键（保证每个变体拿到的是自身的身高），无 combination 时
        # 才回退到 speaker_id（角色没有激活组合的兜底场景）。
        key = item.get("combination") or item.get("speaker_id", "")
        if not key:
            continue
        height_str = str(item.get("height", "")).strip()
        if not height_str:
            continue
        filename = _combination_to_filename(key, "calm")
        height_map[filename] = height_str
        logger.debug(f"  height_map[{filename}] = {height_str} (key={key})")
    logger.info(f"加载身高表：共 {len(height_map)} 条 (含每个变体自身的身高)")
    return height_map


def _height_str_to_cm(height_str: str) -> float:
    try:
        return float(height_str) if height_str else 0.0
    except ValueError:
        return 0.0


def _calm_name_for(path: Path) -> Optional[str]:
    """
    把任意情绪文件名映射回对应的 calm 文件名，用来查身高表。
    'XiaoWuChen_1-1-1_happy.png' → 'XiaoWuChen_1-1-1_calm.png'
    'XiaoWuChen_1-1-1_calm.png'  → 'XiaoWuChen_1-1-1_calm.png'
    非情绪后缀返回 None。
    """
    stem = path.stem
    shot_suffix = ""
    for shot in ("CS", "MS", "LS"):
        suffix = f"_{shot}"
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            shot_suffix = suffix
            break
    for emotion in EMOTIONS:
        suffix = f"_{emotion}"
        if stem.endswith(suffix):
            return stem[: -len(suffix)] + f"_calm{shot_suffix}.png"
    return None


def _is_canvas_source_path(path: Path) -> bool:
    """Return whether *path* is an LS/source image rather than a derived shot.

    New files use the explicit ``*_LS.png`` suffix.  The legacy pipeline also
    produced ``*_calm.png``/``*_happy.png`` without a shot suffix, so those are
    accepted for compatibility; already-derived ``*_MS.png``/``*_CS.png`` are
    deliberately excluded.
    """
    if path.name.startswith("."):
        return False
    stem = path.stem
    if stem.endswith(("_MS", "_CS")):
        return False
    if stem.endswith("_LS"):
        return True
    return any(stem.endswith(f"_{emotion}") for emotion in EMOTIONS)


def expand_for_task(
    task_id: str,
    image_paths: Optional[Iterable[Path]] = None,
    *,
    progress: Optional[ProgressCallback] = None,
) -> int:
    """
    扫描 OUTPUTS_PATH/{task_id}/images/characters/ 下所有 calm + 情绪 LS PNG，
    按身高扩展顶部透明像素。返回成功扩展的图片数量。

    输入是未扩展的 ``*_LS.png``；扩展结果写回同一路径，同时把未扩展原图
    保存到 ``.resume_sources/char_designer/ls``，供后续图生图继续使用。通过
    ``image_paths`` 可只处理本次生成的 LS 集合；省略时保持原有全任务扫描行为。
    是否强制重绘由上游编排层负责清理冲突产物；本步骤始终按输入/输出
    checkpoint 判断是否可复用。
    """
    if not HAS_PIL:
        raise RuntimeError("Pillow 未安装，无法执行身高画布扩展")

    char_dir = Path(OUTPUTS_PATH) / task_id / "images" / "characters"
    if not char_dir.exists():
        raise RuntimeError(f"角色图片目录不存在: {char_dir}")

    height_map = load_height_map(task_id)
    if not height_map:
        # 读取不到身高数据 → 不追加顶部像素，正常跳过，程序照常运行。
        logger.warning("character_prompts 身高表为空，跳过画布扩展，不追加顶部像素")
        return 0

    expanded = 0
    skipped = 0
    invalid_height = 0
    already_expanded = 0
    missing_height = 0
    failed = 0
    eligible = 0

    from PIL import Image as _Image

    source_dir = (
        Path(OUTPUTS_PATH)
        / task_id
        / ".resume_sources"
        / "char_designer"
        / "ls"
    )
    source_dir.mkdir(parents=True, exist_ok=True)

    if image_paths is None:
        candidate_paths = sorted(
            path for path in char_dir.glob("*.png") if _is_canvas_source_path(path)
        )
    else:
        # 只接受当前任务角色目录内的路径，避免一个 redraw 调用误处理其它目录。
        char_dir_resolved = char_dir.resolve()
        candidate_paths = []
        for raw_path in image_paths:
            resolved = Path(raw_path).resolve()
            if resolved.parent == char_dir_resolved and _is_canvas_source_path(resolved):
                candidate_paths.append(resolved)
        candidate_paths = sorted(set(candidate_paths))

    for path in candidate_paths:
        calm_name = _calm_name_for(path)
        if not calm_name:
            continue  # 非 calm/情绪后缀文件
        eligible += 1

        _report_progress(progress, f"检查顶部身高画布: {path.name}")

        height_str = height_map.get(calm_name, "")
        if not height_str:
            # 没有身高值（如非实体角色）→ 不追加顶部像素，正常跳过。
            missing_height += 1
            logger.info(f"跳过 {path.name}: 未在 character_prompts 中找到身高，不追加顶部像素")
            continue

        height_cm = _height_str_to_cm(height_str)
        if height_cm <= 0:
            # 身高为 0 或无效 → 不追加顶部像素，正常跳过。
            invalid_height += 1
            logger.info(f"跳过 {path.name}: 身高为 0 或无效 ({height_str})，不追加顶部像素")
            continue

        ratio = _height_to_ratio(height_cm)
        source_path = source_dir / path.name
        current_is_expanded = False
        try:
            with _Image.open(path) as probe:
                current_is_expanded = _is_canvas_expanded(probe) or bool(
                    probe.info.get(EXPANDED_CANVAS_MARKER_KEY)
                )
        except Exception:
            current_is_expanded = False

        # 未扩展的当前文件是最新生图结果，必须覆盖旧恢复源；否则后续扩展
        # 可能把历史图片重新写回 public LS。
        if validate_image_file(path) and not current_is_expanded:
            try:
                atomic_copy_image(path, source_path)
            except Exception as exc:
                logger.warning("保存未扩展 LS 失败 %s: %s", source_path, exc)

        if validate_image_file(source_path):
            source_sha256 = file_sha256(source_path)
        else:
            source_sha256 = ""
        checkpoint_digest = canonical_digest(
            {
                "height_cm": height_cm,
                "ratio": ratio,
                "source_sha256": source_sha256,
            }
        )
        dependency_digest = canonical_digest(
            {"source_path": source_path.name, "source_sha256": source_sha256}
        )
        decision = reusable_or_adopt_existing(
            task_id,
            "char_designer",
            "expand_canvas",
            path.name,
            input_digest=checkpoint_digest,
            generator_version=EXPAND_CANVAS_GENERATOR_VERSION,
            dependency_digest=dependency_digest,
            output_paths=[path],
            validator=validate_image_file,
            allow_legacy_adoption=False,
        )
        if decision.reusable:
            already_expanded += 1
            _report_progress(progress, f"顶部身高画布已存在，跳过: {path}")
            continue
        if ratio >= 1.0:
            skipped += 1
            logger.info(f"跳过 {path.name}: 身高 {height_cm}cm 已占满画布 (ratio={ratio:.2%})")
            _report_progress(progress, f"顶部身高画布无需扩展: {path.name} (height={height_cm:g}cm)")
            checkpoint_mark_success(
                task_id,
                "char_designer",
                "expand_canvas",
                path.name,
                input_digest=checkpoint_digest,
                generator_version=EXPAND_CANVAS_GENERATOR_VERSION,
                dependency_digest=dependency_digest,
                output_paths=[path],
                validator=validate_image_file,
                result_mode="no_expansion_needed",
            )
            continue

        # 提前检测 marker，避免重跑时把幂等跳过当成失败
        try:
            with _Image.open(path) as probe:
                if _is_canvas_expanded(probe) and not source_sha256:
                    raise RuntimeError(
                        f"{path.name} 已扩展但缺少可复用原始 LS；"
                        "无法在输入版本不明时安全重算"
                    )
        except Exception as exc:
            failed += 1
            logger.warning(f"读取 {path.name} 失败: {exc}")
            continue

        try:
            if not source_sha256:
                raise RuntimeError(f"缺少有效原始 LS source: {source_path}")
            checkpoint_mark_running(
                task_id,
                "char_designer",
                "expand_canvas",
                path.name,
                input_digest=checkpoint_digest,
                generator_version=EXPAND_CANVAS_GENERATOR_VERSION,
                dependency_digest=dependency_digest,
            )
            if _expand_top_by_height(
                source_path,
                height_cm,
                output_path=path,
                source_sha256=source_sha256,
            ):
                checkpoint_mark_success(
                    task_id,
                    "char_designer",
                    "expand_canvas",
                    path.name,
                    input_digest=checkpoint_digest,
                    generator_version=EXPAND_CANVAS_GENERATOR_VERSION,
                    dependency_digest=dependency_digest,
                    output_paths=[path],
                    validator=validate_image_file,
                    result_mode="derived_from_source",
                )
                expanded += 1
                logger.info(f"扩展完成 {path.name}: height={height_cm}cm, 占比={ratio:.2%}")
                _report_progress(
                    progress,
                    f"顶部身高画布完成: {path.name} height={height_cm:g}cm",
                )
            else:
                raise RuntimeError("画布扩展函数未生成有效结果")
        except Exception as exc:
            failed += 1
            logger.warning(f"扩展失败 {path.name}: {exc}")
            try:
                checkpoint_mark_failed(
                    task_id,
                    "char_designer",
                    "expand_canvas",
                    path.name,
                    input_digest=checkpoint_digest,
                    generator_version=EXPAND_CANVAS_GENERATOR_VERSION,
                    dependency_digest=dependency_digest,
                    error=str(exc),
                )
            except Exception:
                logger.exception("记录画布扩展失败 checkpoint 时发生异常")

    logger.info(
        f"画布扩展统计: 已扩展={expanded}, 已扩过跳过={already_expanded}, "
        f"无需扩展(已占满)={skipped}, 身高为0/无效跳过={invalid_height}, "
        f"无身高跳过={missing_height}, 失败={failed}"
    )
    if eligible == 0:
        raise CharacterCanvasPartialError(
            f"角色图片目录中没有可处理的 calm/情绪 LS PNG: {char_dir}"
        )
    # 身高缺失/为0 是合法情形（非实体角色），不再视为失败；只有真正的读图/扩展失败才终止。
    if failed:
        raise CharacterCanvasPartialError(
            f"身高画布扩展不完整: eligible={eligible}, expanded={expanded}, "
            f"already_expanded={already_expanded}, missing_height={missing_height}, "
            f"invalid_height={invalid_height}, failed={failed}"
        )
    return expanded


def main():
    parser = argparse.ArgumentParser(description="按身高扩展角色立绘 (calm + 情绪) 顶部透明画布")
    parser.add_argument("--task-id", required=True)
    # 兼容 main.py 统一传入的 --image-provider 等参数（本步骤不使用）
    args, _ = parser.parse_known_args()
    try:
        expand_for_task(args.task_id)
    except CharacterCanvasPartialError as exc:
        logger.error("[char_designer][PARTIAL] %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
