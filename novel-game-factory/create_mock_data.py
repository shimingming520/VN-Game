#!/usr/bin/env python3
"""
创建模拟数据用于测试整个流程
"""
import sys
import os
from datetime import datetime

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from sqlmodel import Session, select
from services.story_planner.table.models import (
    engine,
    Worldview,
    Outline,
    StoryNode,
    Character,
    Scene,
    Plot,
    WorldviewStatus,
    OutlineStatus
)
from common.platform_db import platform_engine  # plots 已迁平台库

def create_mock_worldview(task_id: str, project_name: str = "测试项目"):
    """创建模拟世界观数据"""
    worldview = Worldview(
        task_id=task_id,
        project_name=project_name,
        world_bible={
            "world_name": "测试世界",
            "time_period": "现代",
            "location": "虚构城市",
            "magic_system": "无魔法",
            "technology_level": "现代科技",
            "social_structure": "现代社会",
            "key_rules": ["遵循物理定律", "现实主义风格"],
            "atmosphere": "轻松愉快"
        },
        status=WorldviewStatus.CONFIRMED,
        created_at=datetime.now(),
        updated_at=datetime.now()
    )
    return worldview

def create_mock_outline(task_id: str):
    """创建模拟大纲数据"""
    outline = Outline(
        task_id=task_id,
        title="测试故事",
        summary="这是一个用于测试的简单故事",
        themes=["友情", "成长"],
        target_audience="全年龄",
        estimated_length="短篇",
        key_plot_points=[
            "主角遇到困难",
            "主角寻求帮助",
            "主角克服困难",
            "圆满结局"
        ],
        character_arcs=["主角从懦弱到勇敢"],
        world_building_notes="现代都市背景",
        tone_and_style="轻松幽默",
        status=OutlineStatus.CONFIRMED,
        created_at=datetime.now(),
        updated_at=datetime.now()
    )
    return outline

def create_mock_story_nodes(task_id: str, depth: int = 3):
    """创建模拟故事节点（简化版，只创建主线）"""
    nodes = []

    # 根节点
    root = StoryNode(
        task_id=task_id,
        node_id="root",
        parent_id=None,
        depth=0,
        title="故事开始",
        content="这是故事的开始。主角小明是一个普通的高中生。",
        choices=[],
        is_ending=False,
        created_at=datetime.now()
    )
    nodes.append(root)

    # 创建简单的线性故事树
    story_templates = [
        {
            "title": "遇到困难",
            "content": "小明在学校遇到了一个难题，他不知道该如何解决。",
            "choices": ["向老师求助", "自己思考", "向同学请教"]
        },
        {
            "title": "寻求帮助",
            "content": "小明决定向他人寻求帮助，他找到了好朋友小红。",
            "choices": ["一起讨论", "听取建议", "继续探索"]
        },
        {
            "title": "找到方法",
            "content": "在朋友的帮助下，小明找到了解决问题的方法。",
            "choices": ["立即行动", "再次确认", "寻求更多意见"]
        },
        {
            "title": "克服困难",
            "content": "小明成功地解决了问题，他感到非常开心。",
            "choices": ["分享经验", "继续前进", "感谢朋友"]
        },
        {
            "title": "圆满结局",
            "content": "通过这次经历，小明变得更加自信和勇敢了。",
            "choices": [],
            "is_ending": True
        }
    ]

    # 根据depth创建节点
    for i in range(min(depth, len(story_templates))):
        template = story_templates[i]
        node = StoryNode(
            task_id=task_id,
            node_id=f"node_{i+1}",
            parent_id=f"node_{i}" if i > 0 else "root",
            depth=i + 1,
            title=template["title"],
            content=template["content"],
            choices=template["choices"],
            is_ending=template.get("is_ending", False),
            created_at=datetime.now()
        )
        nodes.append(node)

    return nodes

def create_mock_characters(task_id: str):
    """创建模拟角色数据"""
    characters = [
        Character(
            task_id=task_id,
            name="小明",
            role="主角",
            age=16,
            gender="男",
            personality="善良、勇敢、有点内向",
            background="普通高中生，成绩中等",
            appearance="中等身高，短发，戴眼镜",
            goals="成为更好的自己",
            relationships={"小红": "好朋友"},
            character_arc="从懦弱到勇敢",
            created_at=datetime.now()
        ),
        Character(
            task_id=task_id,
            name="小红",
            role="配角",
            age=16,
            gender="女",
            personality="开朗、热心、聪明",
            background="小明的同班同学",
            appearance="长发，活泼可爱",
            goals="帮助朋友",
            relationships={"小明": "好朋友"},
            character_arc="一直保持乐观",
            created_at=datetime.now()
        )
    ]
    return characters

def create_mock_scenes(task_id: str):
    """创建模拟场景数据"""
    scenes = [
        Scene(
            task_id=task_id,
            scene_name="教室",
            description="明亮的教室，阳光透过窗户洒进来",
            location="学校",
            time_of_day="上午",
            weather="晴天",
            atmosphere="轻松愉快",
            key_objects=["课桌", "黑板", "窗户"],
            lighting="自然光",
            created_at=datetime.now()
        ),
        Scene(
            task_id=task_id,
            scene_name="操场",
            description="宽阔的操场，学生们在活动",
            location="学校",
            time_of_day="下午",
            weather="晴天",
            atmosphere="活跃热闹",
            key_objects=["跑道", "篮球场", "树木"],
            lighting="阳光明媚",
            created_at=datetime.now()
        )
    ]
    return scenes

def create_mock_plots(task_id: str):
    """创建模拟剧情数据"""
    plots = [
        Plot(
            task_id=task_id,
            node_id="node_1",
            sequence=1,
            scene_id=1,  # 假设scene_id为1
            characters=["小明"],
            dialogue=[
                {"speaker": "小明", "text": "这个问题好难啊..."},
                {"speaker": "小明", "text": "我该怎么办呢？"}
            ],
            actions=["小明皱着眉头看着题目"],
            emotions={"小明": "困惑"},
            created_at=datetime.now()
        ),
        Plot(
            task_id=task_id,
            node_id="node_2",
            sequence=2,
            scene_id=2,
            characters=["小明", "小红"],
            dialogue=[
                {"speaker": "小明", "text": "小红，你能帮我看看这个问题吗？"},
                {"speaker": "小红", "text": "当然可以！让我们一起想想办法。"}
            ],
            actions=["小明走向小红", "小红微笑着点头"],
            emotions={"小明": "期待", "小红": "热心"},
            created_at=datetime.now()
        )
    ]
    return plots

def main():
    """主函数：创建完整的模拟数据"""
    print("🎭 开始创建模拟数据...")
    print()

    # 生成task_id
    task_id = f"mock_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    project_name = "模拟测试项目"

    print(f"📋 Task ID: {task_id}")
    print(f"📋 项目名称: {project_name}")
    print()

    try:
        with Session(engine) as session:
            # 1. 创建世界观
            print("1️⃣  创建世界观数据...")
            worldview = create_mock_worldview(task_id, project_name)
            session.add(worldview)
            session.commit()
            print(f"   ✅ 世界观已创建 (status={worldview.status})")

            # 2. 创建大纲
            print("2️⃣  创建大纲数据...")
            outline = create_mock_outline(task_id)
            session.add(outline)
            session.commit()
            print(f"   ✅ 大纲已创建 (status={outline.status})")

            # 3. 创建故事节点
            print("3️⃣  创建故事节点...")
            nodes = create_mock_story_nodes(task_id, depth=5)
            for node in nodes:
                session.add(node)
            session.commit()
            print(f"   ✅ 已创建 {len(nodes)} 个故事节点")

            # 4. 创建角色
            print("4️⃣  创建角色数据...")
            characters = create_mock_characters(task_id)
            for char in characters:
                session.add(char)
            session.commit()
            print(f"   ✅ 已创建 {len(characters)} 个角色")

            # 5. 创建场景
            print("5️⃣  创建场景数据...")
            scenes = create_mock_scenes(task_id)
            for scene in scenes:
                session.add(scene)
            session.commit()
            print(f"   ✅ 已创建 {len(scenes)} 个场景")

            # 6. 创建剧情（plots 已迁平台库，用独立 session）
            print("6️⃣  创建剧情数据...")
            plots = create_mock_plots(task_id)
            with Session(platform_engine) as plot_session:
                for plot in plots:
                    plot_session.add(plot)
                plot_session.commit()
            print(f"   ✅ 已创建 {len(plots)} 个剧情")

            print()
            print("=" * 60)
            print("🎉 模拟数据创建完成！")
            print("=" * 60)
            print()
            print("📊 数据统计:")
            print(f"   - Task ID: {task_id}")
            print(f"   - 世界观: 1 条")
            print(f"   - 大纲: 1 条")
            print(f"   - 故事节点: {len(nodes)} 条")
            print(f"   - 角色: {len(characters)} 条")
            print(f"   - 场景: {len(scenes)} 条")
            print(f"   - 剧情: {len(plots)} 条")
            print()
            print("🧪 测试命令:")
            print(f"   # 测试角色生成")
            print(f"   docker-compose exec char_designer python -c \"from tasks import generate_characters; generate_characters.delay('{task_id}')\"")
            print()
            print(f"   # 测试音频生成")
            print(f"   docker-compose exec audio_producer python -c \"from tasks import generate_audio; generate_audio.delay('{task_id}')\"")
            print()
            print(f"   # 测试场景生成")
            print(f"   docker-compose exec scene_painter python -c \"from tasks import generate_scenes; generate_scenes.delay('{task_id}')\"")
            print()
            print(f"   # 测试完整任务链")
            print(f"   docker-compose exec char_designer python -c \"")
            print(f"from celery import chain")
            print(f"from tasks import generate_characters")
            print(f"import sys")
            print(f"sys.path.insert(0, '/app/services')")
            print(f"from audio_producer.tasks import generate_audio")
            print(f"from scene_painter.tasks import generate_scenes")
            print(f"task_chain = chain(")
            print(f"    generate_characters.s('{task_id}'),")
            print(f"    generate_audio.s(),")
            print(f"    generate_scenes.s()")
            print(f")")
            print(f"result = task_chain.apply_async()")
            print(f"print(f'任务链已提交: {{result.id}}')")
            print(f"\"")
            print()
            print("📁 输出目录:")
            print(f"   storage/outputs/{task_id}/")

    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())
