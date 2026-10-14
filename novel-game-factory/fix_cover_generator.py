#!/usr/bin/env python3
"""
修复 cover_generator_from_db.py 的 task_id 参数问题
"""
import sys

# 读取文件
with open('/app/services/scene_painter/cover_generator_from_db.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 替换第65行：注释掉模块级别的初始化
old_line = "OUTPUT_DIR, TEMP_DIR, POSITIONS_FILE = get_output_paths()"
new_line = "# OUTPUT_DIR, TEMP_DIR, POSITIONS_FILE = get_output_paths()  # 移到 main 函数中初始化"

if old_line in content:
    content = content.replace(old_line, new_line)
    print("✅ 已注释掉模块级别的 get_output_paths() 调用")
else:
    print("⚠️ 未找到需要替换的行")
    sys.exit(1)

# 修改 CONFIG 使用 None 作为默认值
old_config = '''CONFIG = {
    # 输出路径配置
    "COVER_OUTPUT_DIR": OUTPUT_DIR,  # 固定输出
    "TEMP_OUTPUT_DIR": TEMP_DIR,    # 固定临时
    "POSITIONS_OUTPUT_FILE": POSITIONS_FILE,  # 固定位置信息'''

new_config = '''CONFIG = {
    # 输出路径配置（将在 main 函数中设置）
    "COVER_OUTPUT_DIR": None,  # 将在 main 中初始化
    "TEMP_OUTPUT_DIR": None,    # 将在 main 中初始化
    "POSITIONS_OUTPUT_FILE": None,  # 将在 main 中初始化'''

if old_config in content:
    content = content.replace(old_config, new_config)
    print("✅ 已修改 CONFIG 默认值")
else:
    print("⚠️ 未找到 CONFIG 定义")

# 修改 main 函数，在开始时初始化路径
old_main_start = '''def main():
    import sys
    taskid = None
    if len(sys.argv) > 1:
        taskid = sys.argv[1]

    try:
        # 创建生成器
        generator = CoverGeneratorFromDB(CONFIG)'''

new_main_start = '''def main():
    import sys
    taskid = None
    if len(sys.argv) > 1:
        taskid = sys.argv[1]

    try:
        # 初始化输出路径
        OUTPUT_DIR, TEMP_DIR, POSITIONS_FILE = get_output_paths(taskid)
        CONFIG["COVER_OUTPUT_DIR"] = OUTPUT_DIR
        CONFIG["TEMP_OUTPUT_DIR"] = TEMP_DIR
        CONFIG["POSITIONS_OUTPUT_FILE"] = POSITIONS_FILE

        # 创建生成器
        generator = CoverGeneratorFromDB(CONFIG)'''

if old_main_start in content:
    content = content.replace(old_main_start, new_main_start)
    print("✅ 已在 main 函数中添加路径初始化")
else:
    print("⚠️ 未找到 main 函数开始部分")

# 写回文件
with open('/app/services/scene_painter/cover_generator_from_db.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("✅ 文件修复完成")
