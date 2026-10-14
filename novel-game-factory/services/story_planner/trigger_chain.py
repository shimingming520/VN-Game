"""
直接触发后续任务链（跳过 story_planner）

拓扑:
  info_extractor -> [char_designer→scene_painter | audio_producer | sound_arranger] -> game_builder

并行分支全部成功才触发 game_builder，任一失败则不触发。

用法: python trigger_chain.py <game_project>
示例: python trigger_chain.py douluodalu_35d5ce51
"""
import sys
from celery import chain, chord, group
from common.celery_app import celery_app

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("❌ 错误: 缺少 game_project 参数")
        print("用法: python trigger_chain.py <game_project>")
        print("示例: python trigger_chain.py douluodalu_35d5ce51")
        sys.exit(1)

    game_project = sys.argv[1]

    print("="*60)
    print("🚀 触发后续任务链（并行拓扑）")
    print("="*60)
    print(f"🎮 游戏项目ID: {game_project}")
    print("="*60)

    try:
        task_chain = chain(
            # 第一步：信息提取
            celery_app.signature(
                'services.info_extractor.tasks.extract_info',
                args=[game_project],
                queue='extract_queue'
            ),
            # 第二步：并行生成，全部完成后触发 game_builder
            chord(
                group(
                    # 分支1：角色图片 → 场景图片（串行）
                    chain(
                        celery_app.signature(
                            'services.char_designer.tasks.generate_characters',
                            queue='char_queue'
                        ),
                        celery_app.signature(
                            'services.scene_painter.tasks.generate_scenes',
                            queue='scene_queue'
                        ),
                    ),
                    # 分支2：语音
                    celery_app.signature(
                        'services.audio_producer.tasks.generate_audio',
                        queue='audio_queue'
                    ),
                    # 分支3：音效/BGM
                    celery_app.signature(
                        'services.sound_arranger.tasks.generate_sound',
                        queue='sound_queue'
                    ),
                ),
                # 回调：所有分支完成后打包
                celery_app.signature(
                    'services.game_builder.tasks.build_game_package',
                    args=[game_project],
                    queue='build_queue',
                    immutable=True
                ),
            ),
        )

        result = task_chain.apply_async()
        print(f"✅ 已触发并行任务链")
        print(f"   Chain ID: {result.id}")
        print(f"   拓扑: info_extractor -> [char→scene | audio | sound] -> game_builder")
        print("="*60)
        print("💡 使用以下命令监控日志:")
        print("   docker-compose logs -f")
        print("="*60)

    except Exception as e:
        print(f"❌ 触发任务链失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
