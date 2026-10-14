"""Main pipeline for sound_arranger service（纯音频生成）.

提示词等数据由 info_extractor 服务预先写入数据库：
  - atmosphere 表（氛围特效）  ← info_extractor
  - sound 表（BGM/SFX 描述）   ← info_extractor

本服务只负责读 sound 表，调用 Stable Audio API 生成实际音频文件。
"""
import asyncio
import time
import sys
import traceback
from logger_config import setup_logger

logger = setup_logger("ScriptDirector", "pipeline_main.log")

try:
    from generate_sound import main as step_generate_sound
except ImportError as e:
    logger.error(f"❌ Failed to import modules: {e}")
    sys.exit(1)


async def run_pipeline(manual_task_id=None):
    """
    纯音频生成流程。

    读取 info_extractor 预先写入 sound 表的 BGM/SFX 描述，
    通过 Stable Audio API 生成实际音频文件。
    """
    pipeline_start = time.time()

    logger.info("#" * 60)
    logger.info("🎬 [Sound Arranger] Starting sound generation pipeline")
    if manual_task_id:
        logger.info(f"🔧 [Mode] Manual task mode: {manual_task_id}")
    else:
        logger.info(f"🆕 [Mode] Auto-detect latest task")
    logger.info("#" * 60)

    # 生成音频文件（提示词数据已由 info_extractor 写入 sound 表）
    logger.info("🔹 Generate audio files via Stable Audio API")
    try:
        step_generate_sound(target_task_id=manual_task_id)
    except Exception as e:
        logger.error(f"❌ [Failure] Sound generation pipeline failed: {e}")
        logger.error(traceback.format_exc())
        raise

    duration = time.time() - pipeline_start
    logger.info("#" * 60)
    logger.info(f"🎉 [Success] Sound generation pipeline complete!")
    logger.info(f"⏱️  Total time: {duration:.2f} seconds")
    logger.info(f"📂 Output: audio files, task_id: {manual_task_id}")
    logger.info("#" * 60)

    return manual_task_id


if __name__ == "__main__":
    MANUAL_TASK_ID = ""
    try:
        asyncio.run(run_pipeline(MANUAL_TASK_ID))
    except KeyboardInterrupt:
        logger.warning("\n🛑 Pipeline manually interrupted.")
    except Exception as e:
        logger.critical(f"💀 [CRITICAL] Uncaught exception: {e}")
        logger.error(traceback.format_exc())
