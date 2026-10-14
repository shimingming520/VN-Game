#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
封面数据提取器 - 从数据库获取章节数据，提取视觉元素并构建生图提示词

负责：
- 从 story_tree_json / story_tree_md 表读取章节数据
- 计算节点位置（depth/order）
- 使用 gpt-5.6-sol 分析章节内容，提取关键视觉元素
- 根据视觉元素构建章节封面和游戏总封面的图像提示词
- 将以上结果全部写入 backgrounds 表：
    data['cover_metadata']       : 各章节视觉元素
    data['generated_prompts']    : 各章节图像提示词
    data['novel_cover_metadata'] : 游戏总封面提示词
"""

import os
import json
import time
import re
import sys
import yaml
import psycopg2
from openai import OpenAI
from pathlib import Path
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from urllib.parse import urlparse, unquote

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import DATABASE_URL, OUTPUTS_PATH, PLATFORM_DATABASE_URL
from common.visual_style import (
    ASSET_SCENE,
    STYLE_MODE_LLM,
    get_asset_style,
    load_visual_style_config,
)

try:
    from info_extractor.utils.story_tree_json_parse import load_story_tree_for_player
except ImportError:
    from utils.story_tree_json_parse import load_story_tree_for_player  # type: ignore


PROMPT_TEMPLATE = {
    "prefix": "Create a novel cover background featuring a single, unified scene with no human characters and no Chinese text elements: ",
    "text_overlay": "Include the text 'Chapter {depth}-{order}' in clear English letters, integrated naturally into the scene (top center, subtle white bold typography, cinematic title style, not UI, not subtitle); Do NOT render any Chinese titles, scene titles, or text from the scene description",
    "quality": "clear environmental storytelling, coherent composition, controlled colors, consistent lighting, landscape emphasis",
    "negative_prompt": "people, person, character, face, body, hands, limbs, head, eyes, nose, mouth, portrait, figure, man, woman, child, individual, crowd, group, facial features, human presence, human figures, humans, blurry, low quality, distorted, artifact, watermark, signature, simple, plain, boring, repetitive patterns"
}


def _build_chapter_prompt(
    node_id: str,
    position_info: dict,
    global_style: str,
    visual_elements: Optional[Dict[str, Any]] = None,
):
    """根据视觉元素构建单个章节的图像提示词，返回 (prompt, negative_prompt)"""
    # 章节封面不再叠加 "Chapter X-Y" 字样
    # chapter_label = f"Chapter {position_info['depth']}-{position_info['order']}"

    if visual_elements:
        scene_setting = visual_elements.get('scene_setting', '')
        atmosphere = visual_elements.get('atmosphere', '')
        lighting = visual_elements.get('lighting', 'story-appropriate lighting')

        key_objects = [obj for obj in visual_elements.get('key_objects', []) if obj and
                       'wheelchair' not in obj.lower() and 'burning teacup' not in obj.lower()]
        key_actions = [a for a in visual_elements.get('key_actions', []) if a and
                       'person' not in a.lower() and 'man' not in a.lower() and 'woman' not in a.lower()]
        colors = [c for c in visual_elements.get('colors', []) if c and 'color' not in c.lower()]
        visual_symbols = [s for s in visual_elements.get('visual_symbols', []) if s]

        parts = [
            global_style,
            f"{atmosphere}, establishing shot of {scene_setting}",
        ]
        if key_objects:
            parts.append(f"containing {', '.join(key_objects)}, all objects placed logically in environment")
        if key_actions:
            parts.append(f"displaying {', '.join(key_actions)}")
        if visual_symbols:
            parts.append(f"with symbolic elements {', '.join(visual_symbols)}")
        if colors:
            parts.append(f"in {', '.join(colors)} tones")
        parts.append(f"with {lighting}")
        parts.append("logical placement of objects, physically coherent relationships, unified composition, cohesive environmental setting")
        parts.append("clear environmental storytelling, consistent visual treatment")
        # 章节封面不再叠加 "Chapter X-Y" 字样
        # parts.append(f"Including the text '{chapter_label}' in clear English letters, integrated naturally into the scene (top center, subtle white bold typography, cinematic title style)")
    else:
        parts = [
            global_style,
            PROMPT_TEMPLATE['prefix'],
            PROMPT_TEMPLATE['quality'],
            # 章节封面不再叠加 "Chapter X-Y" 字样
            # PROMPT_TEMPLATE['text_overlay'].format(
            #     depth=position_info['depth'], order=position_info['order']
            # ),
        ]

    prompt = ", ".join(p for p in parts if p.strip())
    return prompt, PROMPT_TEMPLATE['negative_prompt']


def _build_novel_cover_prompt(backgrounds_data: Dict[str, Any], global_style: str):
    """构建游戏总封面提示词，返回 (prompt, negative_prompt)"""
    first_ve = next(
        (d['visual_elements'] for d in backgrounds_data.values() if d.get('visual_elements')),
        None
    )
    if first_ve:
        scene_setting = first_ve.get('scene_setting', 'the novel primary setting')
        atmosphere = first_ve.get('atmosphere', 'story-appropriate atmosphere')
        colors = ', '.join(first_ve.get('colors', []))
        parts = [
            global_style,
            f"{atmosphere}, comprehensive cover representing the entire novel's theme and essence.",
            f"The main setting: {scene_setting}",
        ]
        if colors:
            parts.append(f"using {colors} color palette")
        parts.extend([
            "coherent composition, atmospheric lighting, clear environmental storytelling, consistent visual treatment",
            "Do not include specific chapter titles, numbers or textual elements in the image"
        ])
        prompt = ", ".join(p for p in parts if p.strip())
    else:
        prompt = ", ".join(
            part for part in (
                global_style,
                PROMPT_TEMPLATE['prefix'] +
                "A comprehensive cover representing the entire novel's theme and essence. " +
                PROMPT_TEMPLATE['quality'],
                "Do not include specific chapter titles, numbers or textual elements in the image",
            )
            if part.strip()
        )
    return prompt, PROMPT_TEMPLATE['negative_prompt']


def _load_prompt(filename: str) -> str:
    prompt_dir = Path(__file__).parent.parent / "prompts" / "scene_prompts"
    filepath = prompt_dir / filename
    with open(filepath, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    return data['content'].strip()

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

# 解析平台库 URL 为 psycopg2 配置(跨模块共用表已迁 agent_platform)
parsed = urlparse(PLATFORM_DATABASE_URL)
DATABASE_CONFIG = {
    'host': parsed.hostname,
    'port': parsed.port,
    'user': parsed.username,
    'password': unquote(parsed.password),
    'dbname': parsed.path[1:]
}

MODEL_NAME = "gpt-5.6-sol"
BASE_URL = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com").rstrip("/")
if not BASE_URL.endswith("/v1"):
    BASE_URL = f"{BASE_URL}/v1"


def get_output_paths(task_id=None):
    """返回基于task_id的输出路径"""
    if not task_id:
        raise ValueError("task_id is required for output paths")
    output_dir = os.path.join(OUTPUTS_PATH, task_id, "images", "backgrounds")
    temp_dir = os.path.join(OUTPUTS_PATH, task_id, "images", "backgrounds", "temp")
    positions_file = os.path.join(OUTPUTS_PATH, task_id, "images", "backgrounds", "node_positions.json")
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)
    return output_dir, temp_dir, positions_file


# ============================================================================
# 内容分析器
# ============================================================================

class ContentAnalyzer:
    """内容分析器 - 使用 GPT-5.6 分析章节内容并提取关键视觉元素"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = OpenAI(api_key=api_key, base_url=BASE_URL, timeout=300.0)

    def analyze_content(self, chapter_content: str, node_id: str = "") -> Dict[str, Any]:
        """分析章节内容，提取用于图像生成的视觉元素"""
        system_prompt = _load_prompt("cover_extractor_sys_en.yaml")
        prompt = _load_prompt("cover_extractor_user_en.yaml").replace("{{ chapter_content }}", chapter_content[:2000])

        try:
            response = self.client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {
                        'role': 'system',
                        'content': system_prompt
                    },
                    {
                        'role': 'user',
                        'content': prompt
                    }
                ],
                max_tokens=1500,
                temperature=0.6
            )

            content = response.choices[0].message.content or ""
            if _usage_tracker and getattr(response, 'usage', None):
                _usage_tracker.add_llm_usage(
                    response.usage, MODEL_NAME, provider="openai_compatible"
                )

            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                try:
                    visual_elements = json.loads(json_match.group())
                    for key in ['scene_setting', 'atmosphere']:
                        if key not in visual_elements:
                            visual_elements[key] = ""
                    print(f"✓ 内容分析完成，节点 {node_id}")
                    return visual_elements
                except json.JSONDecodeError as e:
                    print(f"⚠ 内容分析返回的JSON格式错误，使用默认值，节点 {node_id}: {e}")
                    return self._get_default_visual_elements(node_id)
            else:
                print(f"⚠ 内容分析未返回有效JSON，使用默认值，节点 {node_id}")
                return self._get_default_visual_elements(node_id)

        except Exception as e:
            print(f"⚠ 内容分析过程中发生错误 {node_id}: {e}")
            return self._get_default_visual_elements(node_id)

    def _get_default_visual_elements(self, node_id: str = "") -> Dict[str, Any]:
        return {
            "scene_setting": f"A setting related to chapter {node_id}" if node_id else "The novel's primary setting",
            "key_objects": [],
            "atmosphere": "Story-appropriate atmosphere",
            "colors": [],
            "key_actions": [],
            "visual_symbols": [],
            "lighting": "Story-appropriate lighting"
        }


# ============================================================================
# 封面数据提取器
# ============================================================================

class CoverExtractorFromDB:
    """
    从数据库提取封面所需数据，并用 LLM 分析章节内容生成视觉元素文本。
    结果写入 backgrounds 表，供 CoverGeneratorFromDB 使用。
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.content_analyzer = ContentAnalyzer(api_key)
        self.current_task_id = None

    def extract(self, taskid=None) -> Dict[str, Any]:
        """
        从数据库提取章节数据，分析视觉元素，写入 backgrounds 表。

        Returns:
            backgrounds_data: {node_id: {scene_id, node_content, scene_description,
                                          position, visual_elements}}
        """
        try:
            conn = psycopg2.connect(**DATABASE_CONFIG)
            cursor = conn.cursor()

            # 确定 task_id
            if taskid is not None:
                selected_taskid = taskid
            else:
                cursor.execute("SELECT DISTINCT task_id FROM story_tree_md ORDER BY task_id DESC LIMIT 1")
                result = cursor.fetchone()
                if result:
                    selected_taskid = result[0]
                else:
                    cursor.execute("SELECT DISTINCT task_id FROM story_tree_json ORDER BY task_id DESC LIMIT 1")
                    result = cursor.fetchone()
                    if result:
                        selected_taskid = result[0]
                    else:
                        print("错误: 未找到任何task_id")
                        cursor.close()
                        conn.close()
                        return {}

            print(f"使用task_id: {selected_taskid}")
            self.current_task_id = selected_taskid

            # 获取 story_tree_json（章节结构）
            cursor.execute("SELECT data FROM story_tree_json WHERE task_id = %s LIMIT 1", (selected_taskid,))
            json_row = cursor.fetchone()

            # 任务输出语言决定使用哪个完整 locale tree；独立运行时再回退到
            # INFO_EXTRACTOR_STORY_TREE_LANGUAGE / planner 的环境配置。
            requested_language = None
            try:
                cursor.execute(
                    "SELECT output_language FROM novel_tasks "
                    "WHERE task_id = %s LIMIT 1",
                    (selected_taskid,),
                )
                language_row = cursor.fetchone()
                if language_row and language_row[0]:
                    requested_language = str(language_row[0]).strip()
            except Exception as e:
                # psycopg2 marks the transaction as failed after an optional
                # compatibility query error; clear that state before reading
                # story_tree_md below.
                conn.rollback()
                print(f"⚠️ 无法读取任务输出语言，将使用环境/默认语言: {e}")

            # 获取 story_tree_md（章节内容）
            try:
                cursor.execute("SELECT name, data FROM story_tree_md WHERE task_id = %s", (selected_taskid,))
                md_rows = cursor.fetchall()
            except Exception as e:
                print(f"错误: 无法从story_tree_md表获取数据: {e}")
                cursor.close()
                conn.close()
                return {}

            backgrounds_data = {}

            if json_row:
                story_tree = load_story_tree_for_player(
                    json_row[0], language=requested_language
                )
                if not isinstance(story_tree.get("nodes"), dict):
                    raise ValueError(
                        f"task_id={selected_taskid} 的 story_tree_json 没有可用的语言版本"
                    )
                print(
                    "story_tree language selected: "
                    f"requested={requested_language or 'environment/default'}, "
                    f"actual={story_tree.get('player_language', 'unknown')}"
                )
                content_only_llm_route = (
                    load_visual_style_config(selected_taskid).get("mode")
                    == STYLE_MODE_LLM
                )
                if content_only_llm_route:
                    global_style = ""
                    print("LLM 自动 Anchor 路线：封面仅构造内容提示词")
                else:
                    global_style = get_asset_style(selected_taskid, ASSET_SCENE)
                    print(f"使用 scene_global_style: {global_style}")
                nodes = story_tree.get('nodes', {})
                root_id = story_tree.get('root_id', 'chapter_1')

                # 构建 node_id -> 内容 映射
                content_map = {row[0]: row[1] for row in md_rows if len(row) >= 2}

                # BFS 计算每个节点的 depth / order
                queue = [(root_id, 1)]
                node_positions = {}
                depth_counters = {}

                while queue:
                    node_id, depth = queue.pop(0)
                    if depth not in depth_counters:
                        depth_counters[depth] = 1
                    order = depth_counters[depth]
                    depth_counters[depth] += 1
                    node_positions[node_id] = {'depth': depth, 'order': order}

                    node_data = nodes.get(node_id, {})
                    for child in node_data.get('children', []):
                        queue.append((child['child_id'], depth + 1))

                # 写入 backgrounds 表（结构信息）
                background_data_for_db = {
                    'original_story_tree': story_tree,
                    'node_positions': node_positions,
                    'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'task_id': selected_taskid
                }
                cursor.execute(
                    """INSERT INTO backgrounds (data, task_id, create_time)
                       VALUES (%s, %s, NOW())
                       ON CONFLICT (task_id)
                       DO UPDATE SET data = EXCLUDED.data, create_time = EXCLUDED.create_time""",
                    (json.dumps(background_data_for_db, ensure_ascii=False), selected_taskid)
                )
                conn.commit()

                # 为每个节点提取视觉元素
                total = len(node_positions)
                for idx, (node_id, position) in enumerate(node_positions.items(), 1):
                    content = content_map.get(node_id, "")
                    if not content or not content.strip():
                        print(f"警告: 节点 {node_id} 没有找到内容，跳过处理")
                        continue

                    print(f"\n[{idx}/{total}] 分析节点 {node_id} ...")
                    visual_elements = self.content_analyzer.analyze_content(content, node_id)

                    backgrounds_data[node_id] = {
                        'scene_id': len(backgrounds_data) + 1,
                        'node_content': content,
                        'scene_description': content,
                        'position': position,
                        'visual_elements': visual_elements
                    }

                # 将 visual_elements 写回 backgrounds 表
                cover_metadata = {
                    nid: {'visual_elements': d['visual_elements']}
                    for nid, d in backgrounds_data.items()
                }
                cursor.execute(
                    "UPDATE backgrounds SET data = jsonb_set(data, '{cover_metadata}', %s::jsonb) WHERE task_id = %s",
                    (json.dumps(cover_metadata, ensure_ascii=False), selected_taskid)
                )
                conn.commit()
                print(f"✓ 已将 {len(cover_metadata)} 个节点的视觉元素写入数据库")

                # 构建所有章节封面提示词
                chapter_prompts = {}
                for node_id, bg in backgrounds_data.items():
                    prompt, negative_prompt = _build_chapter_prompt(
                        node_id,
                        bg['position'],
                        global_style,
                        bg.get('visual_elements'),
                    )
                    chapter_prompts[node_id] = {'prompt': prompt, 'negative_prompt': negative_prompt}
                    print(f"✓ 已构建章节 {node_id} 提示词")

                # 构建游戏总封面提示词
                novel_prompt, novel_negative = _build_novel_cover_prompt(
                    backgrounds_data,
                    global_style,
                )
                print("✓ 已构建游戏总封面提示词")

                # 一次性保存所有提示词
                cursor.execute(
                    "UPDATE backgrounds SET data = jsonb_set(data, '{generated_prompts}', %s::jsonb) WHERE task_id = %s",
                    (json.dumps(chapter_prompts, ensure_ascii=False), selected_taskid)
                )
                cursor.execute(
                    "UPDATE backgrounds SET data = jsonb_set(data, '{novel_cover_metadata}', %s::jsonb) WHERE task_id = %s",
                    (json.dumps({'prompt': novel_prompt, 'negative_prompt': novel_negative}, ensure_ascii=False), selected_taskid)
                )
                conn.commit()
                print(f"✓ 已将 {len(chapter_prompts)} 个章节提示词和游戏总封面提示词保存入库")

            cursor.close()
            conn.close()

            print(f"提取完成，共 {len(backgrounds_data)} 个节点")
            return backgrounds_data

        except Exception as e:
            print(f"提取数据时发生错误: {e}")
            import traceback
            traceback.print_exc()
            return {}


def main():
    import sys
    import argparse
    from dotenv import load_dotenv

    load_dotenv()
    api_key = (
        os.getenv("GPT_IMAGE_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    if not api_key:
        print("错误: 未设置 GPT_IMAGE_API_KEY（或 OPENAI_API_KEY）")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="封面数据提取器")
    parser.add_argument("--task-id", type=str, default=None, help="指定task_id")
    args = parser.parse_args()
    taskid = args.task_id

    extractor = CoverExtractorFromDB(api_key)
    if _usage_tracker and taskid:
        _usage_tracker.set_context(taskid, "info_extractor")

    backgrounds_data = extractor.extract(taskid)
    print(f"\n提取结果: {len(backgrounds_data)} 个节点")

    if _usage_tracker:
        _usage_tracker.flush()


if __name__ == "__main__":
    main()
