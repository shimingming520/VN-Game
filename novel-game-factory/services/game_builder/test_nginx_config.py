#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试统一 nginx 配置文件生成逻辑
"""
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from services.game_builder.converter import FinalConverter

def test_nginx_config_generation():
    """测试 nginx 配置文件生成"""
    print("=" * 60)
    print("测试统一 Nginx 配置文件生成")
    print("=" * 60)

    # 使用现有的游戏目录进行测试
    game_dir = Path("novel-game-factory/storage/built_games/lsb")

    if not game_dir.exists():
        print(f"错误：游戏目录不存在: {game_dir}")
        return

    print(f"\n使用游戏目录: {game_dir}")

    # 创建转换器实例
    converter = FinalConverter(
        source_dir=str(game_dir),
        template_dir="novel-game-factory/services/game_builder/game_template/shijuexiaoshuo",
        output_dir=str(game_dir)
    )

    # 生成 nginx 配置
    print("\n第一次生成配置...")
    converter.generate_nginx_config()

    # 读取并显示生成的配置
    unified_config_path = game_dir.parent / "nginx.conf"
    if unified_config_path.exists():
        print(f"\n✅ 统一配置文件已生成: {unified_config_path}")
        print("\n配置文件内容预览（前50行）:")
        print("-" * 60)
        with open(unified_config_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for i, line in enumerate(lines[:50], 1):
                print(f"{i:3d}: {line}", end='')
        print("-" * 60)

        # 测试重复生成（应该删除旧配置并添加新配置）
        print("\n\n第二次生成配置（测试更新逻辑）...")
        converter.generate_nginx_config()

        print("\n✅ 配置更新成功！")
        print(f"\n统一配置文件位置: {unified_config_path}")
        print(f"游戏文件目录: {game_dir}")

        # 统计配置中的游戏数量
        with open(unified_config_path, 'r', encoding='utf-8') as f:
            content = f.read()
            game_count = content.count("# GAME_START:")
            print(f"\n当前配置中包含 {game_count} 个游戏")
    else:
        print(f"\n❌ 配置文件未生成: {unified_config_path}")

if __name__ == "__main__":
    test_nginx_config_generation()
