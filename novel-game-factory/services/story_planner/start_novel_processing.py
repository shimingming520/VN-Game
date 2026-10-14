"""
启动脚本：从 storage/raw_novels 读取小说并触发处理任务
"""
import os
import uuid
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from common.config import RAW_NOVELS_PATH
from services.story_planner.tasks import parse_novel_to_draft


def start_novel_processing():
    """
    从环境变量读取小说文件名，生成 game_project ID，并触发处理任务
    """
    # 从环境变量读取小说文件名
    novel_name = os.getenv("NOVEL_NAME")

    if not novel_name:
        print("❌ 错误: 未设置 NOVEL_NAME 环境变量")
        print("   请在 docker-compose.yml 或 .env 中设置 NOVEL_NAME")
        sys.exit(1)

    # 检查文件是否存在
    novel_path = Path(RAW_NOVELS_PATH) / novel_name
    if not novel_path.exists():
        print(f"❌ 错误: 小说文件不存在: {novel_path}")
        print(f"   可用文件列表:")
        raw_novels_dir = Path(RAW_NOVELS_PATH)
        if raw_novels_dir.exists():
            for file in raw_novels_dir.iterdir():
                if file.is_file():
                    print(f"   - {file.name}")
        sys.exit(1)

    # 生成 game_project ID: 小说名（不含扩展名）+ UUID
    novel_basename = Path(novel_name).stem
    unique_id = str(uuid.uuid4())[:8]
    game_project = f"{novel_basename}_{unique_id}"

    print("="*60)
    print("🚀 启动小说处理流程")
    print("="*60)
    print(f"📖 小说文件: {novel_name}")
    print(f"🎮 游戏项目ID: {game_project}")
    print(f"📂 文件路径: {novel_path}")
    print("="*60)

    # 触发 Celery 任务
    try:
        result = parse_novel_to_draft.apply_async(
            args=[novel_name, game_project],
            queue='story_queue'
        )

        print(f"✅ 任务已提交到队列")
        print(f"   任务ID: {result.id}")
        print(f"   队列: story_queue")
        print("="*60)

        return {
            "task_id": result.id,
            "game_project": game_project,
            "novel_name": novel_name
        }

    except Exception as e:
        print(f"❌ 任务提交失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    start_novel_processing()
