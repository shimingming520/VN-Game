#!/usr/bin/env python3
"""
手动触发 char_designer 开始的任务链

用于跳过 story_planner，直接从 char_designer 开始执行
"""
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from common.celery_app import celery_app
from celery import chain


def trigger_char_chain(task_id: str):
    """
    触发从 char_designer 开始的任务链

    Args:
        task_id: 游戏项目ID (例如: douluodalu_c55d8e88)
    """
    print("=" * 60)
    print("🚀 手动触发任务链（从 char_designer 开始）")
    print("=" * 60)
    print(f"📝 Task ID: {task_id}")
    print()

    try:
        # 创建任务链：char_designer -> audio_producer -> scene_painter -> game_builder
        # 注意：chain 会自动将前一个任务的返回值传递给下一个任务
        # 所以只有第一个任务需要显式传递参数
        task_chain = chain(
            celery_app.signature(
                'services.char_designer.tasks.generate_characters',
                args=[task_id],
                queue='char_queue'
            ),
            celery_app.signature(
                'services.audio_producer.tasks.generate_audio',
                queue='audio_queue'
            ),
            celery_app.signature(
                'services.scene_painter.tasks.generate_scenes',
                queue='scene_queue'
            ),
            celery_app.signature(
                'services.game_builder.tasks.build_game_package',
                queue='build_queue'
            )
        )

        # 提交任务链
        result = task_chain.apply_async()

        print("✅ 任务链已提交")
        print(f"   Chain ID: {result.id}")
        print()
        print("📋 执行顺序:")
        print("   1. char_designer (角色生成)")
        print("   2. audio_producer (音频生成)")
        print("   3. scene_painter (场景生成)")
        print("   4. game_builder (游戏构建)")
        print()
        print("=" * 60)
        print()
        print("💡 监控任务执行:")
        print("   docker-compose logs -f")
        print()
        print("或查看特定服务:")
        print("   docker logs -f novel-game-factory-char_designer-1")
        print("   docker logs -f novel-game-factory-audio_producer-1")
        print("   docker logs -f novel-game-factory-scene_painter-1")
        print("   docker logs -f novel-game-factory-game_builder-1")
        print()
        print("=" * 60)

        return result

    except Exception as e:
        print(f"❌ 任务链提交失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    # 使用用户提供的 task_id
    TASK_ID = "douluodalu_c55d8e88"

    trigger_char_chain(TASK_ID)
