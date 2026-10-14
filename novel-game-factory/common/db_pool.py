"""Shared SQLAlchemy worker pool sizing from deployment environment."""

import os


def _read_int(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def worker_pool_kwargs(
    default_pool_size: int,
    default_max_overflow: int,
) -> dict[str, int]:
    """Return bounded SQLAlchemy QueuePool settings for a worker process."""
    return {
        "pool_size": _read_int(
            "WORKER_DB_POOL_SIZE",
            default_pool_size,
            minimum=1,
        ),
        "max_overflow": _read_int(
            "WORKER_DB_MAX_OVERFLOW",
            default_max_overflow,
            minimum=0,
        ),
    }
