"""联调用 stub Celery worker:用真实 task 名注册假实现,秒级返回。

只用于验证 adapter 链路(平台 Job → adapter → Celery → 回调),
不跑真实算法。用法:

    REDIS_HOST=localhost celery -A services.platform_adapter.stub_worker \
        worker -l info -Q story_queue,extract_queue,char_queue,scene_queue,audio_queue,sound_queue,build_queue -c 7
"""
import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.celery_app import celery_app  # noqa: E402

STUB_TASKS = [
    "services.story_planner.tasks.parse_novel_to_draft",
    "services.info_extractor.tasks.extract_info",
    "services.char_designer.tasks.generate_characters",
    "services.scene_painter.tasks.generate_scenes",
    "services.audio_producer.tasks.generate_audio",
    "services.sound_arranger.tasks.generate_sound",
    "services.game_builder.tasks.build_game_package",
]


def _make_stub(name):
    @celery_app.task(name=name)
    def stub(task_id):
        print(f"[stub] {name} running for {task_id}")
        time.sleep(1)
        if str(task_id).startswith("fail"):
            raise RuntimeError(f"stub 模拟失败: {task_id}")
        return {"stub": True, "task_id": task_id}
    return stub


for _name in STUB_TASKS:
    _make_stub(_name)
