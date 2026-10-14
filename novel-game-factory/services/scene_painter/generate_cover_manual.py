#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手动指定提示词，生成游戏封面或章节封面（1690x960）
用法：直接修改下方的 PROMPT / NEGATIVE_PROMPT / OUTPUT_FILE，然后运行：

    python novel-game-factory/services/scene_painter/generate_cover_manual.py
"""

import os
import sys

_SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_SERVICE_DIR))
sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_PROJECT_ROOT, '.env'))

from services.scene_painter.core.cover_generator import ImageGenerator, CONFIG

# ============================================================
# 在这里填写你的提示词和输出路径
# ============================================================

# 章节编号：DEPTH-ORDER，例如第1章第1节 -> Chapter 1-1
# 游戏总封面不需要章节编号，设为 None 即可
DEPTH = 1   # 章节深度
ORDER = 1   # 章节序号

PROMPT = (
    "masterpiece, best quality, highres, cinematic lighting, detailed background, snow-covered ancient Chinese pavilion at twilight, pale winter sun casting long shadows across bloodstained snow, shattered wooden pillars and broken stone steps half-buried in white drifts, a cracked white jade pendant with faint carved character '辞' resting on crimson-stained ice, torn coarse fabric tangled on splintered railings, snapped arrows and discarded blades scattered beneath melting frost, distant bare trees silhouetted against heavy grey sky, swirling snowflakes caught in cold wind, desolate and mournful atmosphere, no humans, no characters, wide shot with deep depth of field "
)

NEGATIVE_PROMPT = CONFIG["PROMPT_TEMPLATE"]["negative_prompt"]

OUTPUT_FILE = "cover_output.png"  # 输出路径，可改为绝对路径

WIDTH = 1690
HEIGHT = 960

# ============================================================

def main():
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        print("错误: 未找到 DASHSCOPE_API_KEY，请检查 .env 文件")
        sys.exit(1)

    generator = ImageGenerator(
        api_endpoint=CONFIG["API_ENDPOINT"],
        api_key=api_key,
        timeout=CONFIG["API_TIMEOUT"],
        model=CONFIG["MODEL_NAME"]
    )

    # 章节封面不再叠加 "Chapter X-Y" 字样
    # if DEPTH is not None and ORDER is not None:
    #     chapter_label = f"Chapter {DEPTH}-{ORDER}"
    #     full_prompt = (
    #         PROMPT + f", Including the text '{chapter_label}' in clear English letters, "
    #         "integrated naturally into the scene (top center, subtle white bold typography, "
    #         "cinematic title style)"
    #     )
    # else:
    #     full_prompt = PROMPT
    full_prompt = PROMPT

    print(f"提示词: {full_prompt}")
    print(f"尺寸: {WIDTH} x {HEIGHT}")
    print(f"输出文件: {OUTPUT_FILE}")
    print("开始生成...")

    image_data = generator.retry_on_failure(
        prompt=full_prompt,
        negative_prompt=NEGATIVE_PROMPT,
        width=WIDTH,
        height=HEIGHT,
        max_retries=CONFIG["API_MAX_RETRIES"]
    )

    image_data.save_to_file(OUTPUT_FILE)
    print(f"\n完成！图片已保存到: {os.path.abspath(OUTPUT_FILE)}")


if __name__ == "__main__":
    main()
