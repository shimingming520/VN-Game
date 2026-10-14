#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
故事树后处理器 - 根据MD正文内容生成书名、简介和章节标题

该脚本会：
1. 从数据库中读取 story_tree_json 和 story_tree_md 表的数据
2. 使用大模型分析正文内容
3. 生成更贴切的书名、简介和章节标题
4. 更新 story_tree_json 表中的数据
"""

import json
import os
import sys
import yaml
import psycopg2
from copy import deepcopy
from dotenv import load_dotenv
from openai import OpenAI
from typing import Dict, Any, List
import re
from pathlib import Path

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import DATABASE_URL, OUTPUTS_PATH, PLATFORM_DATABASE_URL
try:
    from info_extractor.utils.story_tree_json_parse import (
        load_story_tree_for_player,
        story_tree_language_key,
    )
except ImportError:
    from ..utils.story_tree_json_parse import (  # type: ignore
        load_story_tree_for_player,
        story_tree_language_key,
    )

_PROMPT_DIR = Path(__file__).parent.parent / "prompts" / "scene_prompts"


def _load_prompt(filename: str) -> str:
    with open(_PROMPT_DIR / filename, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)['content'].strip()

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

# 解析平台库 URL 为 psycopg2 配置(story_tree_json/story_tree_md 已迁 agent_platform)
from urllib.parse import urlparse, unquote
parsed = urlparse(PLATFORM_DATABASE_URL)
DB_CONFIG = {
    'host': parsed.hostname,
    'port': parsed.port,
    'user': parsed.username,
    'password': unquote(parsed.password),
    'dbname': parsed.path[1:]  # 移除开头的 '/'
}

# 加载环境变量
load_dotenv()

MODEL_NAME = "gpt-5.6-sol"
API_KEY = (
    os.getenv("GPT_IMAGE_API_KEY")
    or os.getenv("OPENAI_API_KEY")
)
BASE_URL = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com").rstrip("/")
if not BASE_URL.endswith("/v1"):
    BASE_URL = f"{BASE_URL}/v1"


def _call_llm(prompt: str, max_tokens: int, temperature: float = 0.7) -> str:
    if not API_KEY:
        raise ValueError("未设置 GPT_IMAGE_API_KEY（或 OPENAI_API_KEY）")
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=300.0)
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if _usage_tracker and getattr(response, "usage", None):
        _usage_tracker.add_llm_usage(
            response.usage, MODEL_NAME, provider="openai_compatible"
        )
    return (response.choices[0].message.content or "").strip()


class StoryTreeUpdater:
    """故事树更新器"""

    def __init__(self):
        # 数据库配置
        self.db_config = DB_CONFIG

    def get_latest_taskid(self):
        """获取最新的task_id"""
        conn = psycopg2.connect(**self.db_config)
        cursor = conn.cursor()

        cursor.execute("SELECT DISTINCT task_id FROM story_tree_json ORDER BY task_id DESC LIMIT 1")
        result = cursor.fetchone()

        cursor.close()
        conn.close()

        if result:
            return result[0]
        else:
            raise ValueError("未找到任何task_id")

    def detect_language(self, text: str) -> str:
        """检测文本语言"""
        if not text:
            return 'zh'

        # 统计中文字符和英文字符的数量
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        english_chars = len(re.findall(r'[a-zA-Z]', text))

        # 如果中文字符占多数，则认为是中文，否则是英文
        if chinese_chars > english_chars:
            return 'zh'
        else:
            return 'en'

    def get_story_data(self, taskid: str):
        """获取故事数据"""
        conn = psycopg2.connect(**self.db_config)
        cursor = conn.cursor()

        # 获取story_tree_json数据
        cursor.execute("SELECT data FROM story_tree_json WHERE task_id = %s LIMIT 1", (taskid,))
        json_row = cursor.fetchone()

        # 获取story_tree_md数据
        cursor.execute("SELECT name, data FROM story_tree_md WHERE task_id = %s", (taskid,))
        md_rows = cursor.fetchall()

        requested_language = None
        try:
            cursor.execute(
                "SELECT output_language FROM novel_tasks WHERE task_id = %s LIMIT 1",
                (taskid,),
            )
            language_row = cursor.fetchone()
            if language_row and language_row[0]:
                requested_language = str(language_row[0]).strip()
        except Exception:
            # 旧环境/轻量测试库可能没有 novel_tasks；公共选择函数会回退到
            # INFO_EXTRACTOR_STORY_TREE_LANGUAGE 或 planner 的默认语言。
            requested_language = None

        cursor.close()
        conn.close()

        if not json_row:
            raise ValueError(f"未找到task_id为 {taskid} 的story_tree_json数据")

        story_tree = json_row[0]
        if isinstance(story_tree, (str, bytes, bytearray)):
            story_tree = json.loads(story_tree)
        if not isinstance(story_tree, dict):
            raise ValueError(
                f"task_id={taskid} 的 story_tree_json.data 不是有效 JSON object"
            )
        content_map = {}
        for node_name, content in md_rows:
            content_map[node_name] = content

        return story_tree, content_map, requested_language

    def generate_book_title(self, all_contents: List[str], lang: str = 'zh') -> str:
        """使用大模型生成书名"""
        combined_content = " ".join(all_contents[:5])  # 使用前5章内容

        if lang == 'zh':
            prompt = _load_prompt("update_story_tree_book_title_zh.yaml").replace("{combined_content}", combined_content)
        else:  # English
            prompt = _load_prompt("update_story_tree_book_title_en.yaml").replace("{combined_content}", combined_content)

        try:
            title = _call_llm(prompt, max_tokens=50)
            title = title.replace('"', '').replace("'", "").strip()
            return title

        except Exception as e:
            import traceback
            print(f"生成书名时出错: {e}")
            print(traceback.format_exc())
            return "未知之书" if lang == 'zh' else "Unknown Book"

    def generate_book_summary(self, all_contents: List[str], lang: str = 'zh') -> str:
        """使用大模型生成简介"""
        combined_content = " ".join(all_contents[:3])  # 使用前3章内容

        if lang == 'zh':
            prompt = _load_prompt("update_story_tree_book_summary_zh.yaml").replace("{combined_content}", combined_content)
        else:  # English
            prompt = _load_prompt("update_story_tree_book_summary_en.yaml").replace("{combined_content}", combined_content)

        try:
            summary = _call_llm(prompt, max_tokens=1000)
            # 硬性限制：中文80字，英文450字符
            try:
                if lang == 'zh':
                    if len(summary) > 80:
                        summary = summary[:80]
                else:
                    if len(summary) > 450:
                        summary = summary[:450]
            except Exception as e:
                print(f"截断简介时出错: {e}，使用原始简介")
            return summary

        except Exception as e:
            import traceback
            print(f"生成简介时出错: {e}")
            print(traceback.format_exc())
            return "一个精彩的故事等待展开..." if lang == 'zh' else "An exciting story awaits..."

    def generate_chapter_title(self, chapter_content: str, depth: int, node_id: str, lang: str = 'zh') -> str:
        """使用大模型生成章节标题"""
        chapter_content = chapter_content[:1000]
        if lang == 'zh':
            prompt = _load_prompt("update_story_tree_chapter_title_zh.yaml").replace("{chapter_content}", chapter_content)
        else:  # English
            prompt = _load_prompt("update_story_tree_chapter_title_en.yaml").replace("{chapter_content}", chapter_content)

        try:
            title = _call_llm(prompt, max_tokens=100)
            if not title:
                title = f"章节_{depth}_{node_id}" if lang == 'zh' else f"Chapter_{depth}_{node_id}"
            return title

        except Exception as e:
            import traceback
            print(f"生成章节标题时出错: {e}")
            print(traceback.format_exc())
            return f"章节_{depth}_{node_id}" if lang == 'zh' else f"Chapter_{depth}_{node_id}"

    def update_story_tree(self, taskid: str):
        """更新故事树"""
        print(f"开始处理 task_id: {taskid}")

        # 获取数据
        story_tree_payload, content_map, requested_language = self.get_story_data(taskid)

        # 新格式是 {locale: complete_tree, publication: metadata}；后续逻辑仍
        # 使用单语言树，但最终只回写被更新的 locale，不能把其它语言版本丢掉。
        combined_text_for_language = " ".join(
            content for content in content_map.values() if content
        )
        detected_lang = self.detect_language(combined_text_for_language)
        target_locale = requested_language or os.getenv(
            "INFO_EXTRACTOR_STORY_TREE_LANGUAGE",
            os.getenv("STORY_PLANNER_STORY_TREE_PLAYER_LANGUAGE", "en-US"),
        )
        locale_key = story_tree_language_key(story_tree_payload, target_locale)

        if locale_key is not None:
            # Planner-owned locale bundles already contain the complete
            # player-facing metadata for every language. Re-running this legacy
            # info post-processor would overwrite translated titles and would
            # also invalidate publication.fingerprint. Keep the bundle intact;
            # info consumers select a locale at their own read boundary.
            print(
                f"检测到 planner 多语言 story_tree（目标 locale={locale_key}），"
                "跳过旧版书名/章节标题重算，直接导出原始 bundle"
            )
            self._dump_to_file(taskid, story_tree_payload)
            return story_tree_payload

        if isinstance(story_tree_payload.get("nodes"), dict):
            # 历史单语言格式。
            locale_key = None
            story_tree = deepcopy(story_tree_payload)
        else:
            # 让公共选择函数负责最后的 en-US/首个有效版本回退；这里仅用于
            # 强制运行旧的元数据更新步骤时保持可用。
            story_tree = load_story_tree_for_player(
                story_tree_payload,
                target_locale,
            )
            locale_key = story_tree_language_key(story_tree_payload, target_locale)
        if not isinstance(story_tree.get("nodes"), dict):
            raise ValueError(f"task_id={taskid} 的 story_tree_json 没有可用的语言版本")

        # 生成文本必须和实际被更新的 locale 一致；否则中文正文 + en-US
        # 任务会把中文元数据写进英文版本。当前 updater 的 prompt 仅有中/英，
        # 其它 locale 保持原有版本内容，仍由 planner 负责其本地化。
        # Keep the historical flat-tree behavior: before locale bundles were
        # introduced, this post-processor followed the language of the MD
        # source rather than forcing the environment's player locale.
        generation_lang = detected_lang if detected_lang in {"zh", "en"} else "en"

        # 收集所有章节内容用于生成书名和简介
        all_contents = []
        for node_id, content in content_map.items():
            if content and len(content) > 10:  # 只收集有意义的内容
                all_contents.append(content)

        # 如果没有足够内容，使用原始故事树中的摘要
        if not all_contents:
            nodes = story_tree.get('nodes', {})
            for node_id, node_data in nodes.items():
                summary = node_data.get('summary', '')
                if summary and len(summary) > 10:
                    all_contents.append(summary)

        if not all_contents:
            print("警告：没有足够的内容来生成书名和简介")
            all_contents = ["故事内容"]

        # 检测语言（前面已基于正文确定目标 locale）
        print(
            f"检测到的内容语言: {'中文' if detected_lang == 'zh' else 'English'}；"
            f"元数据生成语言: {'中文' if generation_lang == 'zh' else 'English'}"
        )

        # 使用大模型生成书名和简介
        print("正在生成书名...")
        book_title = self.generate_book_title(all_contents, generation_lang)
        print(f"生成的书名: {book_title}")

        print("正在生成简介...")
        book_summary = self.generate_book_summary(all_contents, generation_lang)
        print(f"生成的简介: {book_summary[:100]}...")

        # 更新每个节点的标题
        print("正在更新章节标题...")
        nodes = story_tree.get('nodes', {})
        updated_nodes = {}

        for node_id, node_data in nodes.items():
            # 获取该节点的正文内容
            chapter_content = content_map.get(node_id, node_data.get('summary', ''))

            # 获取深度信息
            depth = node_data.get('depth', 1)

            # 使用大模型生成章节主题
            chapter_theme = self.generate_chapter_title(
                chapter_content, depth, node_id, generation_lang
            )

            # 构造新标题：章节主题 + 节点ID（例如："重生之痛 chapter_1"）
            new_title = f"{chapter_theme} {node_id}"

            # 更新节点数据
            updated_node = node_data.copy()
            updated_node['title'] = new_title
            updated_nodes[node_id] = updated_node

            print(f"  - {node_id}: {new_title}")

        # 以原始故事树为基础，只更新需要变动的字段，避免覆盖其他服务写入的数据
        updated_story_tree = story_tree.copy()
        updated_story_tree.update({
            'book_title': book_title,
            'book_summary': book_summary,
            'root_id': story_tree.get('root_id', ''),
            'nodes': updated_nodes,
        })
        # Only the historical flat format reaches this path.  Bundle payloads
        # return above so their publication metadata and all locale copies stay
        # unchanged.
        updated_story_tree['task_id'] = taskid
        persisted_story_tree = updated_story_tree

        # 更新数据库
        conn = psycopg2.connect(**self.db_config)
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE story_tree_json
            SET data = %s
            WHERE task_id = %s
        """, (json.dumps(persisted_story_tree, ensure_ascii=False), taskid))

        conn.commit()
        cursor.close()
        conn.close()

        print(f"成功更新 task_id: {taskid} 的故事树数据")
        print(f"书名: {book_title}")
        print(f"简介: {book_summary[:100]}...")
        print(f"更新了 {len(updated_nodes)} 个节点的标题")

        # 创建输出目录并保存更新后的故事树到JSON文件
        self._dump_to_file(taskid, persisted_story_tree)

        return persisted_story_tree

    def _dump_to_file(self, taskid: str, story_tree: dict):
        """将故事树写入 outputs/{task_id}/story_tree.json"""
        output_dir = Path(OUTPUTS_PATH) / taskid
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "story_tree.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(story_tree, f, ensure_ascii=False, indent=2)
        print(f"已将故事树保存到: {output_file}")

    def export_story_tree(self, taskid: str):
        """跳过 LLM 重算时使用：直接从数据库读取 story_tree_json 落盘成文件。"""
        conn = psycopg2.connect(**self.db_config)
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM story_tree_json WHERE task_id = %s LIMIT 1", (taskid,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row or not row[0]:
            raise ValueError(f"未找到task_id为 {taskid} 的story_tree_json数据，无法导出文件")

        story_tree = row[0]
        if isinstance(story_tree, str):
            story_tree = json.loads(story_tree)

        self._dump_to_file(taskid, story_tree)
        return story_tree


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description='更新故事树 - 生成书名、简介和章节标题')
    parser.add_argument('--task-id', type=str, help='指定 task_id')
    args = parser.parse_args()

    updater = StoryTreeUpdater()

    try:
        # 获取 task_id
        if args.task_id:
            taskid = args.task_id
            print(f"使用指定的 task_id: {taskid}")
        else:
            taskid = updater.get_latest_taskid()
            print(f"获取到最新的 task_id: {taskid}")

        if _usage_tracker and taskid:
            _usage_tracker.set_context(taskid, "info_extractor")

        # 更新故事树
        updater.update_story_tree(taskid)

        print("故事树更新完成！")

        if _usage_tracker:
            _usage_tracker.flush()

    except Exception as e:
        print(f"处理过程中发生错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
