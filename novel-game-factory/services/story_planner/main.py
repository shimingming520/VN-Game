"""
一键运行脚本 (Project Runner)
串联：世界观分析 -> 自动计算深度 -> 故事生成
"""

# 🛡️ 优先级最高：在导入任何模块前清理代理
import os
for proxy_var in ["http_proxy", "https_proxy", "all_proxy",
                  "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "no_proxy", "NO_PROXY"]:
    os.environ.pop(proxy_var, None)

print("✅ 已清理所有代理设置（优先级执行）")

import sys
import math
import argparse
import time
import datetime
from pathlib import Path
from typing import Optional

# --- 1. 环境配置 (参考 main.py) ---
try:
    from dotenv import load_dotenv
    load_dotenv()  # 加载 .env 文件
except ImportError:
    print("⚠️  警告: 未安装 python-dotenv，无法加载 .env 文件")

# 🛡️ 二次清理（防止.env文件重新设置代理）
for proxy_var in ["http_proxy", "https_proxy", "all_proxy",
                  "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]:
    os.environ.pop(proxy_var, None)

# 确保能找到 table 包（添加项目根目录到路径）
# 当前文件: /app/services/story_planner/main.py
# 需要添加: /app (项目根) 和 /app/services (服务根)
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /app
services_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # /app/services

if project_root not in sys.path:
    sys.path.insert(0, project_root)
if services_root not in sys.path:
    sys.path.insert(0, services_root)

# 导入配置
from common.config import STORY_DEPTH, STORY_WIDTH

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

# 导入核心模块（使用本地模块）
from story_planner.core import WorldAnalyst, StoryGenerator, read_novel_text

# 数据库相关导入（用于监控）
try:
    from sqlmodel import Session, select
    from story_planner.table.models import engine, Worldview, Outline, WorldviewStatus, OutlineStatus
except ImportError as e:
    print(f"⚠️  警告: 无法导入数据库模块，监控功能可能不可用: {e}")
    engine = None  # 设置为None，避免后续使用时出现NameError

# ==================== 监控功能 ====================

def wait_for_worldview_confirmation(task_id: str, poll_interval: int = 15, timeout: Optional[int] = None) -> bool:
    """
    等待世界观状态变为CONFIRMED

    Args:
        task_id: 世界观task_id
        poll_interval: 轮询间隔（秒）
        timeout: 超时时间（秒），None表示无超时

    Returns:
        bool: 成功确认返回True，超时或错误返回False
    """
    if engine is None:
        print("❌ 错误: 数据库引擎未初始化，无法执行监控功能")
        print("   请检查数据库模块导入是否成功")
        return False

    print(f"\n🔍 开始监控世界观状态变化: task_id={task_id}")
    print(f"   等待世界观状态变为CONFIRMED (1)...")

    start_time = time.time()
    poll_count = 0

    while True:
        poll_count += 1
        current_time = time.time()

        # 检查超时
        if timeout and (current_time - start_time) > timeout:
            print(f"⏰ 超时: 在{timeout}秒内未检测到世界观状态变化")
            return False

        try:
            with Session(engine) as session:
                worldview = session.exec(
                    select(Worldview).where(Worldview.task_id == task_id)
                ).first()

                if not worldview:
                    print(f"❌ 错误: 找不到世界观 task_id={task_id}")
                    return False

                status = worldview.status
                status_desc = WorldviewStatus.get_description(status) if hasattr(WorldviewStatus, 'get_description') else str(status)

                if poll_count == 1 or poll_count % 10 == 0:  # 每10次轮询输出一次状态
                    print(f"   轮询 #{poll_count}: 世界观状态={status} ({status_desc})")

                if status == WorldviewStatus.CONFIRMED:
                    print(f"✅ 世界观状态已变为CONFIRMED!")
                    return True

                # 如果世界观被归档，则失败
                if status == WorldviewStatus.ARCHIVED:
                    print(f"❌ 世界观已被归档，无法继续")
                    return False

        except Exception as e:
            print(f"⚠️  轮询世界观状态时出错: {e}")

        # 等待下一次轮询
        time.sleep(poll_interval)


def wait_for_outline_confirmation(task_id: str, poll_interval: int = 15, timeout: Optional[int] = None) -> bool:
    """
    等待大纲状态变为CONFIRMED

    Args:
        task_id: 大纲task_id（与世界观的task_id相同）
        poll_interval: 轮询间隔（秒）
        timeout: 超时时间（秒），None表示无超时

    Returns:
        bool: 成功确认返回True，超时或错误返回False
    """
    if engine is None:
        print("❌ 错误: 数据库引擎未初始化，无法执行监控功能")
        print("   请检查数据库模块导入是否成功")
        return False

    print(f"\n🔍 开始监控大纲状态变化: task_id={task_id}")
    print(f"   等待大纲状态变为CONFIRMED (1)...")

    start_time = time.time()
    poll_count = 0

    while True:
        poll_count += 1
        current_time = time.time()

        # 检查超时
        if timeout and (current_time - start_time) > timeout:
            print(f"⏰ 超时: 在{timeout}秒内未检测到大纲状态变化")
            return False

        try:
            with Session(engine) as session:
                outline = session.exec(
                    select(Outline).where(Outline.task_id == task_id)
                ).first()

                if not outline:
                    # 大纲可能尚未生成，继续等待
                    if poll_count == 1 or poll_count % 10 == 0:
                        print(f"   轮询 #{poll_count}: 大纲尚未生成，继续等待...")
                else:
                    status = outline.status
                    status_desc = OutlineStatus.get_description(status) if hasattr(OutlineStatus, 'get_description') else str(status)

                    if poll_count == 1 or poll_count % 10 == 0:
                        print(f"   轮询 #{poll_count}: 大纲状态={status} ({status_desc})")

                    if status == OutlineStatus.CONFIRMED:
                        print(f"✅ 大纲状态已变为CONFIRMED!")
                        return True

                    # 如果大纲被归档，则失败
                    if status == OutlineStatus.ARCHIVED:
                        print(f"❌ 大纲已被归档，无法继续")
                        return False

        except Exception as e:
            print(f"⚠️  轮询大纲状态时出错: {e}")

        # 等待下一次轮询
        time.sleep(poll_interval)


def generate_outline_for_worldview(task_id: str, world_bible, depth: int) -> bool:
    """
    为世界观生成大纲

    Args:
        task_id: 世界观task_id
        world_bible: WorldBible对象
        depth: 故事深度

    Returns:
        bool: 成功返回True，失败返回False
    """
    print(f"\n📝 开始生成大纲...")
    print(f"   世界观task_id: {task_id}")

    try:
        from story_planner.table.outline_fetcher import fetch_outline

        outline_task_id, outline_chapters = fetch_outline(
            worldview_task_id=task_id,
            world_bible=world_bible,
            max_depth=depth
        )

        print(f"✅ 大纲生成成功!")
        print(f"   大纲task_id: {outline_task_id}")
        print(f"   章节数: {len(outline_chapters)}")

        # 显示大纲预览
        print(f"\n📋 大纲预览:")
        chapter_keys = sorted(outline_chapters.keys(),
                             key=lambda x: (int(x.split('-')[0]) if '-' in x else int(x), x))
        for key in chapter_keys[:3]:  # 显示前3章
            content = outline_chapters[key]
            preview = content[:100] + "..." if len(content) > 100 else content
            print(f"   {key}: {preview}")
        if len(outline_chapters) > 3:
            print(f"   ... 还有 {len(outline_chapters) - 3} 章")

        return True

    except ImportError as e:
        print(f"❌ 无法导入outline_fetcher模块: {e}")
        return False
    except Exception as e:
        print(f"❌ 大纲生成失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def generate_story_tree_for_outline(task_id: str, world_bible, depth: int, provider: str) -> bool:
    """
    为大纲生成故事树

    Args:
        task_id: 大纲task_id（与世界观的task_id相同）
        world_bible: WorldBible对象
        depth: 故事深度
        provider: LLM提供商

    Returns:
        bool: 成功返回True，失败返回False
    """
    print(f"\n📖 开始生成故事树...")

    try:
        tree, session_dir = run_step_2_generate(
            world_bible,
            width=STORY_WIDTH,
            depth=depth,
            provider=provider,
            task_id=task_id
        )

        print("\n" + "="*50)
        print("🎉 故事树生成成功！")
        print(f"📂 成果目录:")
        print(f"   - 剧情结构: {session_dir}/story_tree.json")
        print(f"   - 故事文本: {session_dir}/generated_stories/")
        print(f"   - 大纲task_id: {task_id} (已保存到数据库)")
        print("="*50)

        return True

    except Exception as e:
        print(f"❌ 故事树生成失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def monitor_and_generate(task_id: str, world_bible, depth: int, provider: str,
                         poll_interval: int = 15, timeout: Optional[int] = None) -> bool:
    """
    监控世界观和大纲状态，并自动生成后续内容

    Args:
        task_id: 世界观task_id
        world_bible: WorldBible对象
        depth: 故事深度
        provider: LLM提供商
        poll_interval: 轮询间隔（秒）
        timeout: 超时时间（秒）

    Returns:
        bool: 整个流程成功返回True，失败返回False
    """
    print("\n" + "="*60)
    print("🚀 进入监控模式")
    print(f"   任务ID: {task_id}")
    print(f"   轮询间隔: {poll_interval} 秒")
    if timeout:
        print(f"   超时时间: {timeout} 秒")
    print("="*60)

    # 1. 等待世界观状态变为CONFIRMED
    if not wait_for_worldview_confirmation(task_id, poll_interval, timeout):
        print("❌ 世界观确认失败，退出监控")
        return False

    # 2. 生成大纲
    if not generate_outline_for_worldview(task_id, world_bible, depth):
        print("❌ 大纲生成失败，退出监控")
        return False

    # 3. 等待大纲状态变为CONFIRMED
    if not wait_for_outline_confirmation(task_id, poll_interval, timeout):
        print("❌ 大纲确认失败，退出监控")
        return False

    # 4. 生成故事树
    if not generate_story_tree_for_outline(task_id, world_bible, depth, provider):
        print("❌ 故事树生成失败，退出监控")
        return False

    print("\n" + "="*60)
    print("🎉🎉🎉 监控模式完成！所有步骤已成功执行 🎉🎉🎉")
    print("="*60)
    return True


def calculate_depth(novel_file: str, chars_per_chapter: int = 10000) -> int:
    """
    固定深度为8层 (新版策略)。

    8层结构:
    - 第1-2章: 发散节点
    - 第3章: 收束节点
    - 第4-5章: 发散节点
    - 第6章: 收束节点
    - 第7章: 分流到3种结局类型(团圆/悲剧/开放)
    - 第8章: 最终结局
    """
    try:
        content = read_novel_text(novel_file, convert_to_utf8=True).text
        total_chars = len(content)
    except Exception:
        print("⚠️  无法读取小说文件字数")
        total_chars = 0

    # 固定深度为8
    depth = 8

    print(f"📊 深度配置 (v3.6 优化版):")
    print(f"   - 原始小说字数: {total_chars}")
    print(f"   - 固定深度: {depth} 章 (约14万字)")
    print(f"   - 收束策略: 第3,6章收束 + 第7章分流3种结局 + 第8章结局")
    print(f"   - 结局类型: 团圆/悲剧/开放")

    return depth

def run_step_1_analyze(novel_file, project_name, provider):
    """第一步：分析世界观"""
    print("\n" + "="*50)
    print("🚀 步骤 1/2: 提取小说世界观...")
    print("="*50)

    analyst = WorldAnalyst(provider=provider)
    world_bible = analyst.analyze_novel_file(
        filepath=novel_file,
        project_name=project_name,
        output_dir="world_db"
    )
    return world_bible

def run_step_2_generate(world_bible, width, depth, provider, task_id):
    """第二步：生成多分支故事树
    Args:
        world_bible: 世界圣经
        width: 分支宽度
        depth: 故事深度
        provider: LLM提供商
        task_id: 世界观/大纲的task_id（必需，用于关联数据库中的大纲和创建目录）
    """
    print("\n" + "="*50)
    print("🚀 步骤 2/2: 生成多分支剧情树...")
    print(f"   - 设定宽度: {width} (固定)")
    print(f"   - 设定深度: {depth} (动态计算)")
    print("="*50)

    import random

    # 检查 task_id 是否存在
    if not task_id:
        raise ValueError("❌ 错误: task_id 不能为空。所有故事树必须关联一个有效的世界观 task_id")

    if _usage_tracker:
        _usage_tracker.set_context(task_id, "story_planner")

    # 使用 task_id 作为存档目录
    base_dir = Path("storage/story_tree")
    session_dir = base_dir / str(task_id)
    print(f"📂 本次运行存档目录: {session_dir} (使用 task_id)")

    session_dir.mkdir(parents=True, exist_ok=True)

    generator = StoryGenerator(
        world_bible=world_bible,
        provider=provider,
        story_dir=str(session_dir),  # 使用task_id目录作为story_dir
        max_depth=depth,  # 传递最大深度
        task_id=task_id  # 传递task_id，用于关联数据库中的大纲
    )

    # 初始化或加载（在新目录下，总是初始化新树）
    tree_file = generator.story_dir / "story_tree.json"
    if tree_file.exists():
        print("📂 加载现有剧情树...")
        tree = generator.load_existing_tree()
    else:
        print("🌱 初始化新剧情树...")
        tree = generator.initialize_story()

    root_id = tree.root_id

    # 开始循环生成
    for line_idx in range(width):
        print(f"\n📖 正在编写第 {line_idx + 1}/{width} 条世界线...")
        current_node_id = root_id
        current_node = tree.get_node(current_node_id)

        for step_idx in range(depth):
            print(f"  ⬇️  进度 {step_idx + 1}/{depth} (当前节点: {current_node_id})")

            if not current_node.children:
                # 如果是最后一步，可能没有children了
                if step_idx < depth - 1:
                    print("   ⚠️ 警告：中途断档，无后续选项")
                break

            # 智能选择策略：优先选择未生成的子节点
            unvisited_choices = []
            for i, child_choice in enumerate(current_node.children):
                if child_choice.child_id not in tree.nodes:
                    unvisited_choices.append(i)

            if unvisited_choices:
                # 优先选择未访问的选项
                if len(unvisited_choices) > 1:
                    # 如果有多个未生成选项，根据世界线索引选择不同选项
                    choice_index = unvisited_choices[line_idx % len(unvisited_choices)]
                else:
                    choice_index = unvisited_choices[0]
                print(f"    🆕 发现 {len(unvisited_choices)} 个未生成选项，选择索引 {choice_index}")
            else:
                # 所有选项都已存在，随机选择
                choice_index = random.randint(0, len(current_node.children) - 1)
                print(f"    🔄 所有 {len(current_node.children)} 个选项已生成，随机选择索引 {choice_index}")

            choice = current_node.children[choice_index]

            # 检查是否已存在 (菱形收束的关键：如果收束点已生成，直接复用)
            if choice.child_id in tree.nodes:
                print(f"    🔗 剧情收束/复用: 跳转至现有节点 {choice.child_id}")
                current_node_id = choice.child_id
                current_node = tree.get_node(current_node_id)
            else:
                print(f"    ✍️  新剧情创作中...")
                try:
                    new_node = generator.continue_story(
                        current_node_id=current_node_id,
                        choice_index=choice_index
                    )
                    current_node_id = new_node.id
                    current_node = new_node
                    # 避免 API 速率限制
                    time.sleep(1)
                except Exception as e:
                    print(f"    ❌ 生成中断: {e}")
                    import traceback
                    traceback.print_exc()
                    break

    print("✅ 剧情树生成完毕。")
    # 剪枝无效链接
    tree.prune_invalid_links(max_depth=depth)
    # 保存剪枝后的剧情树到本地
    tree.save(str(session_dir / "story_tree.json"))
    print(f"📂 存档位置: {session_dir}")

    # 保存 JSON 到数据库
    try:
        from story_planner.table.story_tree_to_db import save_story_json_to_db
        import json

        # 读取刚保存的 JSON 文件
        json_path = session_dir / "story_tree.json"
        with open(json_path, 'r', encoding='utf-8') as f:
            json_data = json.load(f)

        record = save_story_json_to_db(
            task_id=task_id,
            json_data=json_data,
            overwrite=True
        )
        if record:
            print(f"✅ 故事树JSON已保存到数据库: task_id={task_id}")
            # 自动删除 task_id 文件夹下的文件
            try:
                import shutil
                if session_dir.exists():
                    shutil.rmtree(session_dir)
                    print(f"🗑️  已自动删除本地文件目录: {session_dir}")
                else:
                    print(f"⚠️  本地目录不存在，无需删除: {session_dir}")
            except Exception as e:
                print(f"⚠️  删除本地文件目录失败: {e}")
                import traceback
                traceback.print_exc()
        else:
            print(f"⚠️  故事树JSON保存到数据库失败")
    except Exception as e:
        print(f"⚠️  保存故事树JSON到数据库失败: {e}")
        import traceback
        traceback.print_exc()

    if _usage_tracker:
        _usage_tracker.flush()

    return tree, session_dir


def main():
    parser = argparse.ArgumentParser(description="视觉小说一键生成器")
    parser.add_argument("novel_file", help="小说原始文本路径")
    parser.add_argument("--project", default="MyProject", help="项目名称")
    parser.add_argument("--depth", type=int, default=STORY_DEPTH, help=f"故事深度 (默认{STORY_DEPTH}层,可通过.env配置)")
    parser.add_argument("--provider", default="openai", help="模型提供商")
    parser.add_argument("--worldview-source", default="analyze", choices=["analyze", "latest"],
                       help="世界观数据来源: analyze-从小说分析, latest-使用数据库中最新的世界观")
    parser.add_argument("--skip-story-generation", action="store_true",
                       help="跳过故事生成步骤，只生成大纲")
    parser.add_argument("--include-archived", action="store_true",
                       help="包含已归档的世界观记录（仅当--worldview-source=latest时有效）")
    # 运行模式参数（默认启用监控模式）
    parser.add_argument("--immediate", action="store_true",
                       help="立即模式：不等待状态确认，立即生成所有内容（传统模式）")
    parser.add_argument("--poll-interval", type=int, default=15,
                       help="监控轮询间隔（秒），默认15秒（仅在监控模式有效）")
    parser.add_argument("--timeout", type=int, default=None,
                       help="监控超时时间（秒），默认无超时（仅在监控模式有效）")

    args = parser.parse_args()

    # 根据世界观来源获取世界观数据
    if args.worldview_source == "analyze":
        # 验证文件存在
        if not os.path.exists(args.novel_file):
            print(f"❌ 错误: 找不到文件 {args.novel_file}")
            sys.exit(1)

        print(f"🔍 世界观来源: 从小说文件分析")
        # 1. 分析小说文件
        wb = run_step_1_analyze(args.novel_file, args.project, args.provider)
        task_id = wb.task_id  # 从世界观对象获取task_id

        if not task_id:
            print("❌ 错误: 世界观分析失败，未生成 task_id")
            sys.exit(1)

        print(f"✅ 世界观分析完成:")
        print(f"   task_id: {task_id}")
        print(f"   项目名称: {wb.project_name}")
        print(f"   角色数量: {len(wb.characters)}")
        print(f"   场景数量: {len(wb.scenes)}")

    else:  # args.worldview_source == "latest"
        print(f"🔍 世界观来源: 使用数据库中最新的世界观")

        # 导入必要的模块
        try:
            from story_planner.table.world_bible_to_db import get_latest_worldview_data_and_task_id
            from story_planner.core import WorldBible
        except ImportError as e:
            print(f"❌ 导入模块失败: {e}")
            print(f"   请确保项目结构正确，table模块可访问")
            sys.exit(1)

        # 获取最新世界观数据
        result = get_latest_worldview_data_and_task_id(
            include_archived=args.include_archived,
            status=None  # 不筛选状态，使用默认（排除已归档，除非指定include_archived）
        )

        if not result:
            print(f"❌ 未找到世界观记录")
            if not args.include_archived:
                print(f"   提示: 尝试添加 --include-archived 参数以包含已归档的记录")
            sys.exit(1)

        data = result["data"]
        task_id = result["task_id"]

        print(f"✅ 找到最新世界观:")
        print(f"   task_id: {task_id}")
        print(f"   项目名称: {data.get('project_name', '未知')}")
        print(f"   艺术风格: {data.get('art_style', '未知')}")
        print(f"   角色数量: {len(data.get('characters', {}))}")
        print(f"   场景数量: {len(data.get('scenes', {}))}")

        # 将字典数据转换为WorldBible对象
        wb = WorldBible(**data)

    # 2. 计算深度 (固定为8层)
    depth = args.depth  # 默认8,用户可强制覆盖但不推荐
    print(f"\n⚙️  使用深度: {depth} 章")

    # ==================== 判断运行模式 ====================
    if not args.immediate:
        print(f"\n🔔 进入监控模式（默认）")
        print(f"   轮询间隔: {args.poll_interval} 秒")
        if args.timeout:
            print(f"   超时时间: {args.timeout} 秒")
        else:
            print(f"   超时时间: 无限制")

        # 调用监控生成函数
        success = monitor_and_generate(
            task_id=task_id,
            world_bible=wb,
            depth=depth,
            provider=args.provider,
            poll_interval=args.poll_interval,
            timeout=args.timeout
        )

        if not success:
            print("❌ 监控模式执行失败")
            sys.exit(1)
        else:
            print("✅ 监控模式执行成功")
            sys.exit(0)

    # ==================== 立即模式（传统流程） ====================

    # 3. 生成/获取大纲（task_id 已在上面步骤确保存在）
    outline_task_id = task_id  # 大纲使用世界观的 task_id
    outline_chapters = None

    try:
        from table.outline_fetcher import fetch_outline
        print(f"\n📝 生成/获取大纲...")
        print(f"   世界观task_id: {task_id}")

        outline_task_id, outline_chapters = fetch_outline(
            worldview_task_id=task_id,
            world_bible=wb,
            max_depth=depth
        )

        print(f"✅ 大纲处理完成!")
        print(f"   大纲task_id: {outline_task_id}")
        print(f"   章节数: {len(outline_chapters)}")

        # 显示大纲预览
        print(f"\n📋 大纲预览:")
        chapter_keys = sorted(outline_chapters.keys(),
                             key=lambda x: (int(x.split('-')[0]) if '-' in x else int(x), x))
        for key in chapter_keys[:3]:  # 显示前3章
            content = outline_chapters[key]
            preview = content[:100] + "..." if len(content) > 100 else content
            print(f"   {key}: {preview}")
        if len(outline_chapters) > 3:
            print(f"   ... 还有 {len(outline_chapters) - 3} 章")

    except ImportError as e:
        print(f"⚠️  无法导入outline_fetcher模块: {e}")
        print(f"   跳过大纲生成步骤，将在故事生成时自动创建大纲")
    except Exception as e:
        print(f"⚠️  大纲生成失败: {e}")
        import traceback
        traceback.print_exc()
        print(f"   跳过大纲生成步骤，将在故事生成时自动创建大纲")

    # 4. 生成故事（除非跳过）
    tree = None
    session_dir = None

    if not args.skip_story_generation:
        print(f"\n📖 生成故事树...")
        tree, session_dir = run_step_2_generate(
            wb,
            width=STORY_WIDTH,
            depth=depth,
            provider=args.provider,
            task_id=outline_task_id  # 使用大纲的task_id（等同于世界观的task_id）
        )

        print("\n" + "="*50)
        print("🎉🎉🎉 全流程执行成功！ 🎉🎉🎉")
        print(f"📂 成果目录:")
        print(f"   - 剧情结构: {session_dir}/story_tree.json")
        print(f"   - 故事文本: {session_dir}/generated_stories/")
        if outline_task_id:
            print(f"   - 大纲task_id: {outline_task_id} (已保存到数据库)")
        print("="*50)

        # 触发后续任务链：char -> audio -> scene
        try:
            from celery import chain
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from char_designer.tasks import generate_characters
            from audio_producer.tasks import generate_audio
            from scene_painter.tasks import generate_scenes

            print("\n🚀 触发后续任务链...")
            print(f"   任务链: 角色生成 -> 音频生成 -> 场景生成")
            print(f"   Task ID: {task_id}")

            # 创建任务链（串行执行）
            task_chain = chain(
                generate_characters.s(task_id),
                generate_audio.s(),
                generate_scenes.s()
            )
            result = task_chain.apply_async()

            print(f"✅ 任务链已提交到消息队列")
            print(f"   Chain ID: {result.id}")
            print(f"   后续任务将自动执行，请查看各服务日志")
        except Exception as e:
            print(f"⚠️  任务链触发失败: {e}")
            print(f"   可以手动运行各服务的 main.py 文件")
            import traceback
            traceback.print_exc()
    else:
        print("\n" + "="*50)
        print("✅ 大纲生成完成！")
        print(f"   世界观task_id: {task_id or '未知'}")
        if outline_task_id:
            print(f"   大纲task_id: {outline_task_id} (已保存到数据库)")
            print(f"   章节数: {len(outline_chapters)}")
        print("="*50)

    # ===== 第五步：生成导演脚本（可选，默认注释） =====
    # 取消下面的注释以启用自动生成导演脚本
    # print("\n" + "="*50)
    # print("🎬 步骤 5/5: 生成导演脚本...")
    # print("="*50)
    #
    # from src.high_quality_extractor import HighQualityNovelExtractor
    #
    # # 创建输出目录
    # output_dir = Path("outputjson")
    # output_dir.mkdir(exist_ok=True)
    #
    # # 创建时间戳子文件夹
    # output_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # output_session_dir = output_dir / output_timestamp
    # output_session_dir.mkdir(exist_ok=True)
    #
    # # 初始化提取器
    # extractor = HighQualityNovelExtractor(
    #     provider=args.provider,
    #     temperature=0.6,
    #     max_tokens=6000,
    # )
    #
    # # 直接从 story_tree.json 处理
    # try:
    #     results = extractor.process_story_tree_direct(
    #         story_tree_path=str(session_dir / "story_tree.json"),
    #         story_tree_dir=str(session_dir),
    #         output_json_path=str(output_session_dir / "final_script_assets.json"),
    #         max_workers=3,  # 并发数
    #     )
    #
    #     total_beats = sum(len(beats) for beats in results.values())
    #     print(f"✅ 导演脚本生成完成！")
    #     print(f"   - 处理节点数: {len(results)}")
    #     print(f"   - 总剧情节拍数: {total_beats}")
    #     print(f"   - 输出文件: {output_session_dir}/final_script_assets.json")
    # except Exception as e:
    #     print(f"❌ 导演脚本生成失败: {e}")
    #     import traceback
    #     traceback.print_exc()

if __name__ == "__main__":
    main()
