#!/usr/bin/env python3
"""
手动游戏构建脚本 - 独立运行，不依赖Celery

使用方法:
    直接修改下面的配置，然后运行: python manual_build.py
"""

# ==================== 配置区域 ====================
# 修改这里的 task_id
TASK_ID = "ancient_stats_demo"
DOMAIN = "localhost"
FORCE = True  # 是否强制构建，跳过确认
ENABLE_OSS_UPLOAD = False  # 是否上传 OSS（False 时本地保留 assets，便于本地测试）
DELETE_LOCAL_ASSETS = True  # OSS上传后是否删除本地assets目录（仅在 ENABLE_OSS_UPLOAD=True 时生效）
# ================================================

import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 添加项目根目录到路径
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from services.game_builder.converter import FinalConverter


def manual_build_game(task_id: str, domain: str = "localhost", force: bool = False, delete_local_assets: bool = True,
                      enable_oss_upload: bool = False):
    """
    手动构建游戏包
    Args:
        task_id: 游戏项目ID
        domain: 域名配置（默认localhost）
        force: 强制构建，跳过确认（默认False）
        enable_oss_upload: 保留兼容参数，始终禁用远端上传
    """
    print("=" * 60)
    print("🎮 手动游戏构建工具")
    print("=" * 60)
    print(f"📝 Task ID: {task_id}")
    print(f"🌐 Domain: {domain}")
    print("📁 Storage: local-only")
    print()

    try:
        # 获取脚本所在目录
        script_dir = Path(__file__).resolve().parent

        # 定义路径
        # 1. 源目录：使用项目本地路径（写死）
        # 脚本在 novel-game-factory/services/game_builder/manual_build.py
        # storage 在 novel-game-factory/storage
        storage_path = script_dir.parent.parent / "storage"
        source_dir = storage_path / "outputs" / task_id

        # 2. 模板目录：相对于脚本目录
        template_dir = script_dir / "game_template" / "shijuexiaoshuo"

        # 3. 输出目录：使用项目本地路径
        output_dir = storage_path / "built_games" / task_id

        print("📂 路径配置:")
        print(f"   源目录: {source_dir}")
        print(f"   模板目录: {template_dir}")
        print(f"   输出目录: {output_dir}")
        print()

        # 验证源目录是否存在
        if not source_dir.exists():
            print(f"❌ 错误: 源目录不存在")
            print(f"   路径: {source_dir}")
            print()
            print("💡 提示:")
            print("   1. 检查task_id是否正确")
            print("   2. 确认该任务的资源已经生成完成")
            print("   3. 检查STORAGE_PATH环境变量配置")
            return False

        # 验证模板目录是否存在
        if not template_dir.exists():
            print(f"❌ 错误: 模板目录不存在")
            print(f"   路径: {template_dir}")
            return False

        # 检查源目录内容
        print("🔍 检查源目录内容...")
        required_dirs = ['images', 'audios']
        missing_dirs = []
        for dir_name in required_dirs:
            dir_path = source_dir / dir_name
            if dir_path.exists():
                print(f"   ✅ {dir_name}/ 存在")
            else:
                print(f"   ⚠️  {dir_name}/ 不存在")
                missing_dirs.append(dir_name)

        if missing_dirs:
            print()
            print(f"⚠️  警告: 缺少以下目录: {', '.join(missing_dirs)}")
            print("   构建可能会失败或不完整")
            if not force:
                response = input("   是否继续? (y/n): ")
                if response.lower() != 'y':
                    print("❌ 用户取消构建")
                    return False
            else:
                print("   ⚠️  强制模式：继续构建")

        print()
        print("🚀 开始构建游戏包...")
        print()

        # 创建转换器实例
        converter = FinalConverter(
            source_dir=str(source_dir),
            template_dir=str(template_dir),
            output_dir=str(output_dir),
            domain=domain,
            delete_local_assets=delete_local_assets,
            task_id=task_id,
            enable_oss_upload=enable_oss_upload
        )

        # 步骤 1: 转换所有节点生成游戏代码
        print("📝 步骤 1/2: 转换节点生成游戏代码...")
        converter.create_per_node_projects()
        print("   ✅ 节点转换完成")
        print()

        # 步骤 2: 生成 Nginx 配置
        print("📝 步骤 2/2: 生成 Nginx 配置...")
        converter.generate_nginx_config_only()
        print("   ✅ Nginx配置生成完成")
        print()

        print("=" * 60)
        print("🎉 游戏包构建成功！")
        print("=" * 60)
        print(f"📂 输出目录: {output_dir}")
        print()
        print("📋 构建产物:")
        if output_dir.exists():
            # 列出生成的节点目录
            node_dirs = [d for d in output_dir.iterdir() if d.is_dir() and not d.name.startswith('.')]
            print(f"   - 节点数量: {len(node_dirs)}")
            for node_dir in sorted(node_dirs)[:5]:  # 只显示前5个
                print(f"     • {node_dir.name}/")
            if len(node_dirs) > 5:
                print(f"     ... 还有 {len(node_dirs) - 5} 个节点")

            # 检查nginx配置
            nginx_conf = output_dir / "nginx.conf"
            if nginx_conf.exists():
                print(f"   - Nginx配置: nginx.conf ✅")
            else:
                print(f"   - Nginx配置: nginx.conf ❌")

        print()
        print("💡 下一步:")
        print("   1. 部署到服务器")
        print("   2. 配置Nginx使用生成的nginx.conf")
        print("   3. 访问游戏")
        print("=" * 60)

        return True

    except Exception as e:
        print()
        print("=" * 60)
        print("❌ 构建失败")
        print("=" * 60)
        print(f"错误信息: {e}")
        print()

        import traceback
        print("详细错误:")
        traceback.print_exc()
        print("=" * 60)

        return False


def main():
    """主函数"""
    # 使用脚本顶部配置的参数
    task_id = TASK_ID
    domain = DOMAIN
    force = FORCE
    delete_local_assets = DELETE_LOCAL_ASSETS
    enable_oss_upload = ENABLE_OSS_UPLOAD

    # 执行构建
    success = manual_build_game(task_id, domain, force, delete_local_assets, enable_oss_upload)

    # 返回退出码
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
