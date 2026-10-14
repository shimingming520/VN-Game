"""
人物景别裁切：从远景（LS）立绘裁出中景（MS）与近景（CS）。

运行顺序（必须在 expand_canvas.py 之后）：
  Step 1: generate_calm.py         → 基础组合 calm 立绘（远景 LS）
  Step 2: generate_variant_calm.py → 变体组合 calm（远景 LS）
  Step 3: generate_emotion.py      → 情绪变体（远景 LS）
  Step 4: expand_canvas.py         → 按身高在头顶扩展透明画布
  Step 5: ★ generate_shots.py      ← 复制每张 LS，按身高裁切生成 MS / CS

景别定义（裁切像素以“文生图原始高度”为基准，不含 expand_canvas 在头顶加的透明像素）：
  - 远景 LS：整图（原始立绘 + 顶部透明扩展），本步骤不处理，直接保留。
  - 中景 MS：100cm及以下底裁384px；100–155cm从384px线性增加到512px；
    155cm及以上底裁512px（以gpt原始1536px为基准）。
  - 近景 CS：从底部裁掉 原始高度 * 1/2（去掉下半身），保留头肩胸。

“固定像素”说明：
  原始文生图高度按 provider 取固定值（gpt=1536，qwen/wan=1472），可用环境变量
  CHAR_BASE_IMAGE_HEIGHT 覆盖。裁切量 = 该固定值 * 比例，从图片“底部”裁掉。
  因为 expand_canvas 只在头顶（图片顶部）追加透明像素，人物始终贴着图片底部，
  所以“从底部裁掉固定像素”等价于“以原始立绘尺寸为基准裁掉腿脚”，与头顶加了多少
  透明像素无关——正好满足“裁掉的像素不算角色头上添加的高度”。

每个角色 / 每个变体 / 每个情绪最终都会有 LS / MS / CS 三张图。

用法：
  python char_designer/core/generate_shots.py --task-id <task_id> [--image-provider gpt]
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

current_dir = Path(__file__).resolve().parent
char_designer_dir = current_dir.parent
workspace_root = char_designer_dir.parent
project_root = workspace_root.parent
for path in (str(workspace_root), str(char_designer_dir), str(current_dir), str(project_root)):
    if path not in sys.path:
        sys.path.insert(0, path)


def add_common_config_search_paths() -> None:
    # 本仓库自带 common/config.py 时直接用它，避免扫描到其它仓库的旧副本
    # （旧副本 DATABASE_URL 未对密码里的 '@' 做 %40 编码，会连库失败）。
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

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(project_root) / "novel-game-factory" / ".env")
    load_dotenv(dotenv_path=workspace_root / ".env")
except ImportError:
    pass

from common.config import OUTPUTS_PATH
from common.visual_checkpoint import (
    canonical_digest,
    file_sha256,
    mark_failed as checkpoint_mark_failed,
    mark_running as checkpoint_mark_running,
    mark_success as checkpoint_mark_success,
    reusable_or_adopt_existing,
)
from services.char_designer.core.artifact_contract import (
    atomic_image_output,
    validate_image_file,
)

try:
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    HAS_PIL = True
except ImportError:
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
            logger.debug("景别裁切进度回调失败", exc_info=True)


class CharacterShotsPartialError(RuntimeError):
    """The available LS set was processed, but some shots are unavailable."""
# The crop contract changed from a fixed MS ratio to the height-aware
# 384px→512px rule.  Bump the generator version so old checkpoints cannot be
# reused under the new product contract.
SHOT_GENERATOR_VERSION = "character-shots-v3-height-aware-resume"

# 原始文生图高度（不含 expand_canvas 头顶透明扩展）。按 provider 取固定值，
# 可用环境变量 CHAR_BASE_IMAGE_HEIGHT 覆盖成一个统一固定值。
PROVIDER_BASE_HEIGHT = {"gpt": 1536, "qwen": 1472, "wan": 1472}
DEFAULT_BASE_HEIGHT = 1536

# 从底部裁掉的比例（以原始文生图高度为基准）。
MS_CUT_RATIO = 1.0 / 3.0   # 中景：底部裁掉 1/3
CS_CUT_RATIO = 1.0 / 2.0   # 近景：底部裁掉 1/2

# 支持的景别集合，及各派生景别（非 LS）从底部裁掉的比例。
# LS 是 AI 文生图产出的远景原图，也是 MS/CS 的裁切源，本身不裁。
SHOT_CHOICES = ("LS", "MS", "CS")
DERIVED_SHOT_CUT_RATIO = {"MS": MS_CUT_RATIO, "CS": CS_CUT_RATIO}
MS_REDUCED_CUT_RATIO = 1.0 / 4.0
MS_NO_CROP_HEIGHT_CM = 100.0
MS_FULL_CROP_HEIGHT_CM = 155.0


def _height_to_ms_cut_pixels(height_cm: float, base_height: int) -> int:
    """按身高计算MS底裁：384px→512px，CS仍使用固定比例。"""
    reduced_cut = int(round(base_height * MS_REDUCED_CUT_RATIO))
    full_cut = int(round(base_height * MS_CUT_RATIO))
    if height_cm <= 0 or height_cm <= MS_NO_CROP_HEIGHT_CM:
        return reduced_cut
    if height_cm >= MS_FULL_CROP_HEIGHT_CM:
        return full_cut
    progress = (height_cm - MS_NO_CROP_HEIGHT_CM) / (
        MS_FULL_CROP_HEIGHT_CM - MS_NO_CROP_HEIGHT_CM
    )
    return int(round(reduced_cut + (full_cut - reduced_cut) * progress))


def _resolve_base_height(image_provider: str) -> int:
    """返回原始文生图高度（固定像素基准）。环境变量优先，其次按 provider 取值。"""
    raw = os.environ.get("CHAR_BASE_IMAGE_HEIGHT")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
            logger.warning(f"CHAR_BASE_IMAGE_HEIGHT={raw!r} 必须大于 0，改用 provider 默认值")
        except ValueError:
            logger.warning(f"CHAR_BASE_IMAGE_HEIGHT={raw!r} 不是有效整数，改用 provider 默认值")
    return PROVIDER_BASE_HEIGHT.get(image_provider, DEFAULT_BASE_HEIGHT)


def _crop_bottom(src_path: Path, dst_path: Path, cut_pixels: int) -> bool:
    """
    复制 src_path，从底部裁掉 cut_pixels 像素后写到 dst_path。

    - 至少保留 1px 高度，避免裁空。
    - 保留源图的 PNG 文本元数据（含 expand_canvas 的 expanded_canvas_v 标记），
      避免裁出的 MS/CS 被后续步骤误当成未扩展图再次扩展。
    """
    try:
        with Image.open(src_path) as im:
            im.load()
            if im.mode != "RGBA":
                im = im.convert("RGBA")
            w, h = im.size
            cut = max(0, min(cut_pixels, h - 1))
            new_h = h - cut
            cropped = im.crop((0, 0, w, new_h))

            pnginfo = PngInfo()
            for key, value in (im.info or {}).items():
                if isinstance(value, str):
                    pnginfo.add_text(key, value)
            with atomic_image_output(dst_path) as temporary_path:
                cropped.save(temporary_path, format="PNG", pnginfo=pnginfo)
        return True
    except Exception as exc:
        logger.warning(f"裁切失败 {src_path.name} -> {dst_path.name}: {exc}")
        return False


def crop_shots_for_ls(
    ls_path: Path,
    shots,
    image_provider: str = "gpt",
    height_cm: float | None = None,
) -> list:
    """
    从单张远景 *_LS.png 裁出所选的派生景别（MS/CS），与批量流程用同一套裁切基准。

    Args:
        ls_path: 远景 LS 图路径（文件名须以 _LS 结尾）。
        shots: 目标景别集合，如 ("MS", "CS")；LS 会被忽略（它是裁切源，不裁）。
        image_provider: 仅用于确定原始文生图高度基准（gpt=1536, qwen/wan=1472）。

    Returns:
        错误信息列表；为空表示全部成功（或无派生景别可裁）。
    """
    if not HAS_PIL:
        return ["Pillow 未安装，无法裁切景别图"]
    if not validate_image_file(ls_path) or not ls_path.stem.endswith("_LS"):
        return [f"景别裁切源不是有效的 LS 图: {ls_path}"]

    base_height = _resolve_base_height(image_provider)
    stem = ls_path.stem[:-len("_LS")]
    errors = []
    for shot in shots:
        if shot == "LS":
            continue
        ratio = DERIVED_SHOT_CUT_RATIO.get(shot)
        if ratio is None:
            errors.append(f"不支持的景别: {shot}")
            continue
        dst_path = ls_path.parent / f"{stem}_{shot}.png"
        cut_pixels = (
            _height_to_ms_cut_pixels(height_cm, base_height)
            if shot == "MS" and height_cm is not None
            else int(round(base_height * ratio))
        )
        if not _crop_bottom(ls_path, dst_path, cut_pixels):
            errors.append(f"景别裁切失败: {dst_path.name}")
    return errors


def generate_shots_for_task(
    task_id: str,
    image_provider: str = "gpt",
    image_paths: Optional[Iterable[Path]] = None,
    *,
    shots: Optional[Iterable[str]] = None,
    progress: Optional[ProgressCallback] = None,
) -> dict:
    """
    扫描 OUTPUTS_PATH/{task_id}/images/characters/ 下所有 *_LS.png，
    为每张裁出对应的 *_MS.png（按身高底裁384–512px）和 *_CS.png（底部裁1/2）。

    输入是已经完成顶部画布扩展的 ``*_LS.png``；输出为同目录下对应的
    ``*_MS.png`` / ``*_CS.png``。通过 ``image_paths`` 可只处理指定的 LS 集合；
    省略时保持原有全任务扫描行为。``shots`` 可限制派生景别，默认生成 MS 与
    CS。是否强制重绘由上游编排层负责清理冲突产物；本步骤始终按 LS 输入和
    输出 checkpoint 判断是否可复用。
    """
    if not HAS_PIL:
        raise RuntimeError("Pillow 未安装，无法裁切景别图。请安装: pip install Pillow")

    char_dir = Path(OUTPUTS_PATH) / task_id / "images" / "characters"
    if not char_dir.exists():
        raise CharacterShotsPartialError(f"角色图片目录不存在: {char_dir}")

    requested_shots = {
        str(shot).strip().upper()
        for shot in (shots if shots is not None else ("MS", "CS"))
        if str(shot).strip().upper() in DERIVED_SHOT_CUT_RATIO
    }
    if image_paths is None:
        ls_files = sorted(char_dir.glob("*_LS.png"))
    else:
        char_dir_resolved = char_dir.resolve()
        ls_files = []
        for raw_path in image_paths:
            resolved = Path(raw_path).resolve()
            if resolved.parent == char_dir_resolved and resolved.name.endswith("_LS.png"):
                ls_files.append(resolved)
        ls_files = sorted(set(ls_files))
    if not ls_files:
        raise CharacterShotsPartialError(f"角色图片目录中没有远景 LS 图: {char_dir}")

    if not requested_shots:
        _report_progress(progress, f"景别裁切跳过: 未请求 MS/CS，LS={len(ls_files)}")
        return {"ls": len(ls_files), "ms": 0, "cs": 0, "failed": 0}

    base_height = _resolve_base_height(image_provider)
    cs_cut = int(round(base_height * CS_CUT_RATIO))
    from services.char_designer.core.expand_canvas import (
        _calm_name_for,
        _height_str_to_cm,
        load_height_map,
    )
    height_map = load_height_map(task_id)
    logger.info(
        f"景别裁切基准：原始高度={base_height}px（provider={image_provider}），"
        f"中景 MS 按身高底裁384–{int(round(base_height * MS_CUT_RATIO))}px，"
        f"近景 CS 底裁={cs_cut}px(1/2)"
    )

    ms_done = cs_done = failed = 0
    for ls_path in ls_files:
        _report_progress(progress, f"开始处理景别: {ls_path.name}")
        stem = ls_path.stem[:-len("_LS")]  # 去掉末尾 _LS
        ms_path = char_dir / f"{stem}_MS.png"
        cs_path = char_dir / f"{stem}_CS.png"

        calm_name = _calm_name_for(ls_path)
        height_cm = _height_str_to_cm(height_map.get(calm_name, "")) if calm_name else 0.0
        ms_cut = _height_to_ms_cut_pixels(height_cm, base_height)

        source_sha256 = file_sha256(ls_path) if validate_image_file(ls_path) else ""
        dependency_digest = canonical_digest(
            {"ls_path": ls_path.name, "ls_sha256": source_sha256}
        )
        shot_specs = {
            "MS": (ms_path, ms_cut),
            "CS": (cs_path, cs_cut),
        }
        for shot_name in ("MS", "CS"):
            if shot_name not in requested_shots:
                continue
            target_path, cut_pixels = shot_specs[shot_name]
            asset_id = f"{ls_path.stem}:{shot_name}"
            input_digest = canonical_digest(
                {
                    "shot": shot_name,
                    "cut_pixels": cut_pixels,
                    "base_height": base_height,
                    "provider": image_provider,
                }
            )
            try:
                decision = reusable_or_adopt_existing(
                    task_id,
                    "char_designer",
                    "generate_shots",
                    asset_id,
                    input_digest=input_digest,
                    generator_version=SHOT_GENERATOR_VERSION,
                    dependency_digest=dependency_digest,
                    output_paths=[target_path],
                    validator=validate_image_file,
                )
                if decision.reusable:
                    if shot_name == "MS":
                        ms_done += 1
                    else:
                        cs_done += 1
                    _report_progress(progress, f"景别已存在，跳过: {target_path}")
                    continue
                checkpoint_mark_running(
                    task_id,
                    "char_designer",
                    "generate_shots",
                    asset_id,
                    input_digest=input_digest,
                    generator_version=SHOT_GENERATOR_VERSION,
                    dependency_digest=dependency_digest,
                )
                if not _crop_bottom(ls_path, target_path, cut_pixels):
                    raise RuntimeError(f"景别裁切失败: {target_path.name}")
                checkpoint_mark_success(
                    task_id,
                    "char_designer",
                    "generate_shots",
                    asset_id,
                    input_digest=input_digest,
                    generator_version=SHOT_GENERATOR_VERSION,
                    dependency_digest=dependency_digest,
                    output_paths=[target_path],
                    validator=validate_image_file,
                    result_mode="local_crop",
                )
                if shot_name == "MS":
                    ms_done += 1
                else:
                    cs_done += 1
                _report_progress(progress, f"景别完成: {target_path}")
            except Exception as exc:
                failed += 1
                logger.warning("景别生成失败 %s: %s", target_path, exc)
                try:
                    checkpoint_mark_failed(
                        task_id,
                        "char_designer",
                        "generate_shots",
                        asset_id,
                        input_digest=input_digest,
                        generator_version=SHOT_GENERATOR_VERSION,
                        dependency_digest=dependency_digest,
                        error=str(exc),
                    )
                except Exception:
                    logger.exception("记录景别失败 checkpoint 时发生异常")

    logger.info(
        f"景别裁切统计: LS 源图={len(ls_files)}, 生成 MS={ms_done}, 生成 CS={cs_done}, 失败={failed}"
    )
    if failed:
        raise CharacterShotsPartialError(
            f"景别裁切不完整: LS={len(ls_files)}, MS={ms_done}, CS={cs_done}, 失败={failed}"
        )
    return {"ls": len(ls_files), "ms": ms_done, "cs": cs_done, "failed": failed}


def main():
    parser = argparse.ArgumentParser(description="从远景 LS 立绘裁出中景 MS 与近景 CS")
    parser.add_argument("--task-id", required=True)
    parser.add_argument(
        "--image-provider",
        choices=["qwen", "wan", "gpt"],
        default="gpt",
        help="仅用于确定原始文生图高度基准（gpt=1536, qwen/wan=1472）",
    )
    # 兼容 main.py 统一传入的 --model 等参数（本步骤不使用）
    args, _ = parser.parse_known_args()
    try:
        generate_shots_for_task(args.task_id, image_provider=args.image_provider)
    except CharacterShotsPartialError as exc:
        logger.error("[char_designer][PARTIAL] %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
