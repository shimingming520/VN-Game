#!/usr/bin/env python
"""本地 Celery 测试 - 不需要 Redis"""
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

# 配置 Celery 使用内存模式（不需要 Redis）
from common import celery_app

# 设置为 eager 模式：任务立即同步执行，不需要 worker
celery_app.celery_app.conf.update(
    task_always_eager=True,  # 任务立即执行
    task_eager_propagates=True,  # 异常会传播
)

from services.game_builder.tasks import build_game_package


if __name__ == "__main__":
    print("使用 Celery eager 模式测试（无需 Redis）...")
    print("=" * 60)

    # 调用 Celery 任务（会立即执行）
    result = build_game_package.delay()

    print("\n" + "=" * 60)
    print("任务执行结果:")
    print(result.get())  # 获取结果
