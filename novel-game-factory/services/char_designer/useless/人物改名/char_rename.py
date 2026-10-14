"""
角色改名器 (Character Renamer)

流程：
1. 从 characters 表读取指定 task_id 的角色数据（主名 + 别名）
2. 用大模型为每个名字生成对应的新名字
3. 将映射关系写入 mapping 表的 name_map 字段

name_map 格式：{"原名": "新名", ...}



使用方法
python services/char_designer/core/char_rename.py --task-id <task-id>

"""

import os
import sys
import json
import logging
import argparse
from pathlib import Path
from typing import Dict, List, Optional

# 项目根目录
_current_dir = Path(__file__).resolve().parent
_project_root = _current_dir.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from common.config import DATABASE_URL, PLATFORM_DATABASE_URL

# 支持直接运行和作为模块导入两种方式
_utils_dir = _current_dir.parent / "utils"
if str(_utils_dir) not in sys.path:
    sys.path.insert(0, str(_utils_dir))
from llm_client import LLMClient

try:
    from sqlalchemy import create_engine, text
except ImportError:
    raise ImportError("请安装 sqlalchemy: pip install sqlalchemy")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ─────────────────────────── DB helpers ────────────────────────────

def _get_engine():
    return create_engine(PLATFORM_DATABASE_URL)


def _load_characters(engine, task_id: str) -> Dict:
    """从 characters 表读取角色数据"""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM characters WHERE task_id = :tid"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        raise ValueError(f"characters 表中未找到 task_id={task_id}")
    data = row[0]
    if isinstance(data, str):
        data = json.loads(data)
    return data


def _save_name_map(engine, task_id: str, name_map: Dict[str, str]):
    """将 name_map 写入 mapping 表（upsert）"""
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO mapping (task_id, name_map, create_time, update_time)
            VALUES (:tid, cast(:nm as jsonb), NOW(), NOW())
            ON CONFLICT (task_id)
            DO UPDATE SET name_map = EXCLUDED.name_map, update_time = NOW()
        """), {"tid": task_id, "nm": json.dumps(name_map, ensure_ascii=False)})
    logger.info(f"name_map 已写入 mapping 表，共 {len(name_map)} 条映射")


# ─────────────────────────── name extraction ───────────────────────

def _collect_names(characters_data: Dict) -> Dict[str, List[str]]:
    """
    从 characters data 收集所有主名和别名。

    返回：{"主名": ["别名1", "别名2"], ...}
    """
    result: Dict[str, List[str]] = {}

    for char_key, char_val in characters_data.items():
        if not isinstance(char_val, dict):
            continue

        main_name: str = char_val.get("name") or char_key
        if not main_name:
            continue

        aliases: List[str] = []
        for field in ("other_names", "aliases", "nicknames", "alias"):
            raw = char_val.get(field)
            if isinstance(raw, list):
                aliases = [a for a in raw if isinstance(a, str) and a.strip()]
                break
            elif isinstance(raw, str) and raw.strip():
                aliases = [raw.strip()]
                break

        result[main_name] = aliases

    return result


# ─────────────────────────── LLM rename ────────────────────────────

_SYSTEM_PROMPT = """你是一位专业的中文小说角色改名专家。
你的任务是为小说中的角色生成新名字，同时保持原有的命名风格和人物关系。

改名规则：
1. **总则**  
   - 主名改名后，其所有别名/称谓必须与新主名保持一致的姓氏或核心字。  
   - 原来同姓的角色，改名后也应保持同姓（例如：原先"苏雨棠"和"苏萱"都姓苏，改名后可改为"叶芷柔"和"叶榕"，仍同姓）。
   - 所有名字（包括中文名和英文名）都必须更换姓氏和名，新姓氏必须与原姓氏不同。  
   - 中文名风格与原名相近（古风对古风、现代对现代）意象要相似；英文名同样需要更换为全新的英文姓名，风格相近。  
   - 别名不能与新主名完全相同，新别名要同时参考 旧主名、旧别名和新主名来生成
   - 原先人物名不同的，改名后也不同；如果原先同名不同姓，改名后也同名不同姓（例如：原先"苏雨棠""林雨萱"可分别改为"叶芷柔""陆芷欣"；但原先"苏雨棠""林雨棠"则改为"叶芷柔""陆芷柔"）。
   - 如果遇到 【固定搭配型称呼】和 【词缀型称呼】的矛盾，比如说 "老师" 那么优先按照【固定搭配型称呼】来判定
   
2. **完全人名**  
   - 定义：指完整的个人姓名，不带任何词缀（如"苏雨棠"）。  
   - 处理：整体替换为新的完整姓名，遵循总则中的风格一致、姓氏一致等要求。

3. **词缀型称呼**
   - 定义：由"人名核心"（可以是姓氏、名字或完整人名）加上固定的前缀或后缀构成。
   - 处理原则：识别并完整保留词缀，只替换人名核心部分，且替换后的人名核心必须与新主名的姓氏或核心字一致。
   - **多词缀组合**：称呼中同时含有多个词缀时，所有词缀均保留，只替换中间的人名核心。优先级高于单词缀规则，即若一个称呼可同时匹配单词缀和多词缀，按多词缀处理（不限于此，请举一反三）：
     - "小X子" → "小X"和"X子"两种词缀，保留"小"和"子"，替换X
     - "小X姐" → "小X"和"X姐"两种词缀，保留"小"和"姐"，替换X（如"小舞姐" → "小林姐"，不能丢掉"小"）
     - "小X哥" → "小X"和"X哥"两种词缀，保留"小"和"哥"，替换X
   - 常见格式示例（不限于此，请举一反三）：    
     - "小X" → 保留"小"，替换X（如"小舞" → 保留"小"，将"舞"换成其它字）
     - "X老师" → 保留"老师"，替换X
     - "老X" → 保留"老"，替换X
     - "X爷爷/奶奶/叔/伯" → 保留称谓后缀，替换X
     - "X老大/老二" → 保留"老大/老二"，替换X为新姓
     - "X主任/队长/会长" → 保留职务后缀，替换X为新姓
     - "X哥/X姐/X哥哥/X姐姐" → 保留"哥"/"姐"/"哥哥"/"姐姐"，替换X
     - "X鬼/X酒鬼" → 保留"鬼"/"酒鬼"，替换X
     - "酒鬼X" → 保留"酒鬼"前缀，替换X
     - "X老爸" → 保留"老爸"，替换X为新名
     - "X妹子/X妹纸" → 保留"妹子"/"妹纸"，替换X
     - 其他任何"词缀+人名"或"人名+词缀"的组合，均按此原则处理。
   

4. **完全词缀型称呼**
   - 定义：整个称呼全部由词缀/通用描述词构成，不含任何具体的姓或名。
   - 判断方法：拆开后每个部分都是通用词，没有一个部分是真实姓名。
   - 示例：
     - "酒鬼老爸" → "酒鬼"和"老爸"都是通用词，不是姓名 → 保持原样不变
     - "邋遢铁匠" → "邋遢"和"铁匠"都是通用词，不是姓名 → 保持原样不变
     - "小助理" → "小"和"助理"都是通用词，不是姓名 → 保持原样不变
     -
   - 处理：保持原样不变。
   - 注意：与词缀型称呼的区别在于，词缀型称呼中必须含有真实的姓或名作为核心；完全词缀型称呼则完全没有真实姓名成分。

5. **固定搭配型称呼**
   - 定义：纯粹的通用职称/称谓，与具体人名无关（如"大师"、"老师"、"村长"、"战魂大师"、"执事大人"等）。
   - 处理：保持原样不变。

输出格式：严格返回 JSON 对象，key 为原名，value 为新名。不要输出任何解释，只输出 JSON。"""


def _build_user_prompt(names_info: Dict[str, List[str]]) -> str:
    lines = ["请为以下角色的所有名字（主名和别名）生成新名字：", ""]

    for main_name, aliases in names_info.items():
        if aliases:
            lines.append(f"- 主名：{main_name}，别名：{', '.join(aliases)}")
        else:
            lines.append(f"- 主名：{main_name}，无别名")

    lines += [
        "",
        "要求：",
        "- 每个原名（包括别名）都必须有对应的新名",
        "- 同一角色的主名和别名改名后要保持姓氏/核心字一致",
        "- 纯通用称谓原样保留",
        '- 返回格式：{"原名": "新名", ...}',
    ]
    return "\n".join(lines)


def _rename_with_llm(
    names_info: Dict[str, List[str]],
    provider: str = "qwen",
    model: Optional[str] = None,
) -> Dict[str, str]:
    """调用 LLM 生成改名映射，返回 {"原名": "新名", ...}"""
    client = LLMClient(provider=provider, model=model, temperature=0.7, max_tokens=4000)

    user_prompt = _build_user_prompt(names_info)
    logger.info("正在调用 LLM 生成改名映射...")

    result = client.chat_completion_json(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
    )

    name_map: Dict[str, str] = {k: v for k, v in result.items() if isinstance(k, str) and isinstance(v, str)}

    # 缺失的名字用原名填充
    all_names = set(names_info.keys())
    for aliases in names_info.values():
        all_names.update(aliases)

    missing = all_names - set(name_map.keys())
    if missing:
        logger.warning(f"LLM 未返回以下名字的映射，使用原名填充：{missing}")
        for name in missing:
            name_map[name] = name

    logger.info(f"LLM 改名完成，共 {len(name_map)} 条映射")
    return name_map


# ─────────────────────────── main pipeline ─────────────────────────

def run(task_id: str, provider: str = "qwen", model: Optional[str] = None) -> Dict[str, str]:
    """
    完整流程：读取角色 -> LLM 改名 -> 写入 mapping 表

    Returns:
        name_map: {"原名": "新名", ...}
    """
    engine = _get_engine()

    # 1. 读取角色数据
    logger.info(f"读取 task_id={task_id} 的角色数据...")
    characters_data = _load_characters(engine, task_id)
    logger.info(f"共读取到 {len(characters_data)} 个角色")

    # 2. 收集所有名字
    names_info = _collect_names(characters_data)
    if not names_info:
        raise ValueError("未从 characters 表中提取到任何角色名")

    total_names = sum(1 + len(v) for v in names_info.values())
    logger.info(f"共 {len(names_info)} 个角色，{total_names} 个名字（含别名）")

    # 3. LLM 改名
    name_map = _rename_with_llm(names_info, provider=provider, model=model)

    # 4. 写入数据库
    _save_name_map(engine, task_id, name_map)

    # 打印结果
    print("\n改名映射：")
    for orig, new in name_map.items():
        changed = " ✓" if orig != new else ""
        print(f"  {orig} -> {new}{changed}")

    return name_map


# ─────────────────────────── CLI ───────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="角色改名器 - 用 LLM 为角色生成新名字并写入 mapping 表")
    parser.add_argument("--task-id", required=True, help="任务 ID")
    parser.add_argument("--provider", choices=["openai", "claude", "qwen"], default="qwen", help="LLM 提供商（默认：qwen）")
    parser.add_argument("--model", default=None, help="模型名称（默认根据提供商选择）")
    args = parser.parse_args()

    try:
        name_map = run(task_id=args.task_id, provider=args.provider, model=args.model)
        print(f"\n✅ 完成，共生成 {len(name_map)} 条改名映射")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ 执行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
