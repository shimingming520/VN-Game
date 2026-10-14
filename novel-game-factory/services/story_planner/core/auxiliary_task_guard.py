"""Bounded deduplication guard for Story Planner B-class auxiliary tasks."""

from __future__ import annotations

from contextlib import contextmanager
import logging
import uuid


_RELEASE_IF_OWNER_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
logger = logging.getLogger(__name__)


def _default_redis_client():
    import redis

    from common.config import REDIS_URL

    return redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


@contextmanager
def auxiliary_task_guard(
    task_id: str,
    task_kind: str,
    *,
    ttl_seconds: int,
    redis_client=None,
):
    """Allow one owner for a stable ``task_id + task_kind`` business unit."""
    from common.config import GROUP_NAME

    client = redis_client or _default_redis_client()
    safe_kind = str(task_kind).strip().lower().replace(" ", "_")
    key = f"story_planner:aux:{GROUP_NAME}:{safe_kind}:{task_id}"
    owner = uuid.uuid4().hex
    acquired = bool(
        client.set(
            key,
            owner,
            nx=True,
            ex=max(1, int(ttl_seconds)),
        )
    )
    try:
        yield acquired
    finally:
        if acquired:
            try:
                client.eval(_RELEASE_IF_OWNER_SCRIPT, 1, key, owner)
            except Exception as exc:
                logger.warning(
                    "辅助任务租约释放失败，将等待 TTL 自动回收: key=%s error=%s",
                    key,
                    exc,
                )
