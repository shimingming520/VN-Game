#!/usr/bin/env python
"""测试脚本 - 游戏构建器"""
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from services.game_builder.tasks import build_game_package


def test_build_game_package():
    """测试游戏包构建功能"""
    print("=" * 60)
    print("开始测试游戏构建...")
    print("=" * 60)

    result = build_game_package()

    print("\n" + "=" * 60)
    print("测试结果:")
    print("=" * 60)
    print(f"状态: {result['status']}")
    print(f"消息: {result['message']}")
    if result['status'] == 'success':
        print(f"构建路径: {result['build_path']}")

    return result


if __name__ == "__main__":
    result = test_build_game_package()

    # 返回适当的退出码
    sys.exit(0 if result['status'] == 'success' else 1)
