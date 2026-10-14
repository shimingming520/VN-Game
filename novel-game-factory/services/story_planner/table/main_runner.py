# main_runner.py
import asyncio
import datetime
import random
import string
from sqlmodel import Session
from models import engine, StoryRelease, init_db
# 引入上面的两个提取函数
from extract_plots_to_db import run_plot_extraction
from extract_scenes_to_db import run_scene_extraction

# ================= 配置 =================
# ⚠️ 这里修改你要处理的文件夹路径
INPUT_DIR = "/Users/liu/pycharmpt/jz1.0ProjectV3.9.1/story_tree/test"


# =======================================

def generate_run_id():
    """生成唯一运行标识: YYYYMMDD_HHMMSS_xxxx"""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{timestamp}_{suffix}"


async def main():
    # 1. 确保数据库表存在
    init_db()

    # 2. 生成本次运行的"身份证"
    current_run_id = generate_run_id()
    print("=" * 50)
    print(f"🚀 启动新任务 | RunID: {current_run_id}")
    print("=" * 50)

    # 3. 运行情节提取 -> 获取 Plot ID
    plot_db_id = await run_plot_extraction(INPUT_DIR)

    # 4. 运行场景提取 -> 获取 Scene ID
    scene_db_id = run_scene_extraction(INPUT_DIR)

    print("-" * 30)

    # 5. 【关键步骤】在关联表里把它们绑在一起
    if plot_db_id and scene_db_id:
        with Session(engine) as session:
            release = StoryRelease(
                run_id=current_run_id,  # 身份证
                plot_id=plot_db_id,  # 刚生成的 Plot
                scene_id=scene_db_id  # 刚生成的 Scene
            )
            session.add(release)
            session.commit()
            print(f"🎉 关联成功！")
            print(f"   RunID: {current_run_id}")
            print(f"   Plot #{plot_db_id} <---> Scene #{scene_db_id}")
    else:
        print("❌ 提取不完整，未生成关联记录。")
        if not plot_db_id: print("   - Plot提取失败")
        if not scene_db_id: print("   - Scene提取失败")


if __name__ == "__main__":
    asyncio.run(main())