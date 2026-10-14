"""Celery application instance and queue definitions."""
import os
import sys

from celery import Celery
from celery.signals import worker_process_init
from common.config import REDIS_URL

# 十几个小时属于正常，48 小时才作为死循环/失控任务的硬保护。Redis broker
# visibility timeout 必须更长，避免正常长任务在执行中被当成丢失消息重投。
_VISIBILITY_TIMEOUT = int(os.environ.get("CELERY_VISIBILITY_TIMEOUT", "1209600"))
_MAIN_TASK_TIME_LIMIT = int(os.environ.get("MAIN_TASK_TIME_LIMIT", "172800"))
_MAIN_TASK_SOFT_TIME_LIMIT = int(
    os.environ.get(
        "MAIN_TASK_SOFT_TIME_LIMIT",
        str(max(_MAIN_TASK_TIME_LIMIT - 1200, 1)),
    )
)

_MAIN_TASK_NAMES = (
    "services.story_planner.tasks.parse_novel_to_draft",
    "services.info_extractor.tasks.extract_info",
    "services.char_designer.tasks.generate_characters",
    "services.scene_painter.tasks.generate_scenes",
    "services.audio_producer.tasks.generate_audio",
    "services.sound_arranger.tasks.generate_sound",
    "services.game_builder.tasks.build_game_package",
)

celery_app = Celery(
    "novel_game_factory",
    broker=REDIS_URL,
    backend=REDIS_URL
)

# Queue configuration
celery_app.conf.task_routes = {
    "services.story_planner.tasks.*": {"queue": "story_queue"},
    "services.info_extractor.tasks.*": {"queue": "extract_queue"},
    "services.sound_arranger.tasks.*": {"queue": "sound_queue"},
    "services.audio_producer.tasks.*": {"queue": "audio_queue"},
    "services.scene_painter.tasks.*": {"queue": "scene_queue"},
    "services.char_designer.tasks.*": {"queue": "char_queue"},
    "services.game_builder.tasks.*": {"queue": "build_queue"},
    # chord 完成轮询任务：默认落到无人消费的 default 队列会导致 chord callback
    # （game_builder）永不触发。路由到 build_queue，由 game_builder worker 顺带消费。
    "celery.chord_unlock": {"queue": "build_queue"},
}

# 十几个小时属于正常生成；48 小时才视为失控任务。每个服务都可以通过环境
# 变量继续调大，但不能完全无上限，否则死循环会无限占 Worker、持续烧 token。
celery_app.conf.task_annotations = {
    task_name: {
        "time_limit": _MAIN_TASK_TIME_LIMIT,
        "soft_time_limit": _MAIN_TASK_SOFT_TIME_LIMIT,
    }
    for task_name in _MAIN_TASK_NAMES
}

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # 三处必须保持一致（Celery Redis 文档要求）。任务正常跑十几个小时期间
    # 不会因为消息租约到期而重复投递。
    broker_transport_options={"visibility_timeout": _VISIBILITY_TIMEOUT},
    result_backend_transport_options={"visibility_timeout": _VISIBILITY_TIMEOUT},
    visibility_timeout=_VISIBILITY_TIMEOUT,
    # 与上一条配套：默认 prefetch=4 会让 worker 预取 4 条却只跑 1 条，
    # 剩下 3 条在 acks_late 下长期 unacked，visibility 计时照走，
    # 于是长任务会连带把排在它后面的消息一起拖到重投。长任务场景必须为 1。
    worker_prefetch_multiplier=1,
    # 生成任务耗时长且会烧模型额度。只有执行成功后再 ack，Pod 被驱逐、进程
    # OOM 或节点掉线时不提前 ack。子进程级崩溃由 Adapter/中台作为一次明确
    # 失败处理，避免 Celery 自己无限重投绕开平台 max_attempts；整 Pod 丢失则由
    # 阶段 lease + Adapter 同 attempt 恢复机制接管。
    task_acks_late=True,
    task_reject_on_worker_lost=False,
    task_track_started=True,
    # Adapter 只在任务结束时读取结果，保留一天足够排障，同时防止 70 个 Worker
    # 长期运行后 Redis backend 累积无界增长。
    result_expires=86400,
    # Celery 6 起启动阶段是否重试不再由 broker_connection_retry 隐式决定。
    broker_connection_retry_on_startup=True,
)


@worker_process_init.connect
def _dispose_inherited_sqlalchemy_pools(**_kwargs):
    """Celery prefork 子进程不能复用父进程已创建的 PostgreSQL 连接。"""
    try:
        from sqlalchemy.engine import Engine
    except Exception:
        return

    seen = set()
    for module in list(sys.modules.values()):
        namespace = getattr(module, "__dict__", None)
        if not namespace:
            continue
        for value in list(namespace.values()):
            if not isinstance(value, Engine) or id(value) in seen:
                continue
            seen.add(id(value))
            try:
                # close=False 是 SQLAlchemy 官方推荐的 fork 后处理方式：子进程
                # 丢弃继承池，但不主动操作父进程仍持有的连接。
                value.dispose(close=False)
            except TypeError:
                value.dispose()
