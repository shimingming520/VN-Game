import os
import json
import re
import asyncio
from sqlalchemy import create_engine, text
from openai import AsyncOpenAI
from tqdm.asyncio import tqdm
from tenacity import retry, stop_after_attempt, wait_random_exponential
from logger_config import setup_logger
from pypinyin import lazy_pinyin
import sys

logger = setup_logger("Step1_Plots", "step1_plots.log")

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import DATABASE_URL, PLATFORM_DATABASE_URL
from common.db_pool import worker_pool_kwargs

try:
    from common.usage_tracker import tracker as _usage_tracker
    from common.usage_tracker import init_usage_table

    init_usage_table()
except Exception:
    _usage_tracker = None

DB_URL = PLATFORM_DATABASE_URL  # 跨模块共用表已迁平台库(agent_platform)
global_engine = create_engine(
    DB_URL,
    pool_pre_ping=True,
    **worker_pool_kwargs(default_pool_size=10, default_max_overflow=20),
)

API_KEY = os.getenv("DASHSCOPE_API_KEY")
BASE_URL = os.getenv(
    "DASHSCOPE_COMPATIBLE_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
MODEL_NAME = "qwen3-max"

MAX_CONCURRENT_REQUESTS = 5

ALLOWED_EMOTIONS = [
    "calm", "fearful", "angry", "sad",
    "surprised", "happy", "disgusted"
]

EMOTION_KEYWORDS = {
    "happy": [
        "微笑", "大笑", "欢笑", "欢喜", "开心", "高兴", "激动", "温柔", "得意", "轻快", "好耶", "打趣", "释然", "轻笑",
        "噗嗤",
        "smile", "laugh", "joy", "grin", "chuckle", "happy", "glad", "cheerful", "relieved", "excited"
    ],
    "sad": [
        "哭泣", "大哭", "落泪", "眼泪", "泪水", "抽泣", "悲伤", "哀求", "哽咽", "苦涩", "伤心", "难过", "绝望", "叹息",
        "垂下头", "心如刀绞", "痛苦", "红了眼眶",
        "cry", "tear", "sob", "weep", "sad", "grief", "despair", "sigh", "pain", "heartbroken", "mourn"
    ],
    "angry": [
        "愤怒", "发怒", "怒吼", "大吼", "气愤", "愤恨", "怒骂", "责骂", "怒瞪", "瞪眼", "喝斥", "大喝", "冷笑", "讥笑",
        "嘲笑", "质问", "咆哮", "滚", "闭嘴", "呵斥", "咬牙切齿",
        "angry", "furious", "rage", "shout", "yell", "glare", "roar", "growl", "scowl", "shut up", "sneer", "mock"
    ],
    "fearful": [
        "惊恐", "害怕", "苍白", "退缩", "哆嗦", "冷汗", "别过来", "惊慌", "发抖", "颤抖", "恐惧", "畏惧", "慌乱",
        "慌张",
        "fear", "scared", "terrified", "tremble", "shiver", "pale", "panic", "shudder", "horrified"
    ],
    "surprised": [
        "惊诧", "愣住", "错愕", "发呆", "傻眼", "意外", "不可置信", "瞪大眼", "倒吸", "怎么可能", "难道",
        "shock", "surprise", "stunned", "gasp", "unbelievable", "startle", "taken aback"
    ],
    "disgusted": [
        "厌恶", "鄙视", "嫌弃", "呸", "讨厌", "恶心", "皱眉", "冷漠", "白眼", "冷傲", "嘲弄", "令人作呕",
        "disgust", "hate", "revulsion", "frown", "scoff", "disdain", "roll eyes"
    ],
}

ROLE_MAPPING = {}


def fetch_source_context(target_task_id=None):
    """获取提取剧情所需的全部上下文"""

    with global_engine.connect() as conn:
        task_id = target_task_id
        if not task_id:
            logger.info("🔍 未指定 task_id，查找最新任务...")
            sql = text("SELECT task_id FROM story_tree_md ORDER BY id DESC LIMIT 1")
            res = conn.execute(sql).fetchone()
            if not res:
                logger.error("❌ story_tree_md 表为空")
                return None, None, None
            task_id = res[0]

        logger.info(f"🚀 锁定任务 Task ID: {task_id}")

        sql_char = text("SELECT data FROM characters WHERE task_id = :tid")
        row_char = conn.execute(sql_char, {"tid": task_id}).fetchone()

        char_data = {}
        if row_char:
            char_data = row_char[0]
            logger.info(f"✅ 成功加载角色库: 包含 {len(char_data)} 个角色")
        else:
            logger.warning("⚠️ 警告: 未找到对应的角色数据。")

        sql_md = text("SELECT name, data FROM story_tree_md WHERE task_id = :tid")
        rows_md = conn.execute(sql_md, {"tid": task_id}).fetchall()

        if not rows_md:
            logger.error(f"❌ 未找到 MD 内容 (TaskID: {task_id})")
            return None, None, None

        md_contents = {row[0]: row[1] for row in rows_md}
        logger.info(f"📦 加载剧情文本: {len(md_contents)} 个章节")

        return task_id, char_data, md_contents


def save_to_plots_table(task_id, plots_data):
    """存入 plots 表"""
    if not plots_data: return None

    try:
        global_engine.dispose()  # 强制释放旧连接，确保获取新连接
        with global_engine.begin() as conn:
            sql = text("""
                       INSERT INTO plots (task_id, data, create_time, update_time)
                       VALUES (:tid, :data, NOW(), NOW()) ON CONFLICT (task_id) DO
                       UPDATE SET
                           data = EXCLUDED.data,
                           update_time = NOW()
                           RETURNING id
                       """)
            result = conn.execute(sql, {
                "tid": task_id,
                "data": json.dumps(plots_data, ensure_ascii=False)
            })
            new_id = result.fetchone()[0]
            logger.info(f"💾 [Plots] 入库成功! DB_ID: {new_id}")
            return new_id
    except Exception as e:
        logger.error(f"❌ [Plots] 入库失败: {e}")
        try:
            fallback_dir = os.path.join(project_root, "storage", "outputs", str(task_id))
            os.makedirs(fallback_dir, exist_ok=True)
            fallback_path = os.path.join(fallback_dir, "plots.json")
            with open(fallback_path, "w", encoding="utf-8") as f:
                json.dump(plots_data, f, ensure_ascii=False, indent=2)
            logger.info(f"📁 [Plots] 已降级保存到本地: {fallback_path}")
        except Exception as fe:
            logger.error(f"❌ [Plots] 本地降级保存也失败: {fe}")
        return None


# ================= 新增：序幕处理与数据库更新 =================

def process_hook_section(text):
    """
    解析序幕部分，清除【】标签，强制转为旁白
    """
    lines = text.split('\n')
    hook_beats = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith('#'):
            continue

        # 核心清洗正则：匹配以【开头，以】结尾的标签，以及后面可能跟着的（动作描述）和冒号
        clean_line = re.sub(r'【.*?】[：:]?\s*', '', line)
        clean_line = clean_line.strip('""\'\'“”‘’')

        if not clean_line:
            continue

        hook_beats.append({
            "is_hook": True,
            "beat_type": "narration",
            "speaker_name": "旁白",
            "speaker_id": "PangBai",
            "content": clean_line,
            "emotion": "calm",
            "_loc": "prologue",
            "_time": "day",
            "_feat": "none"
        })
    return hook_beats


def update_story_tree_hook_id(task_id, hook_id):
    """
    将提取到的 hook_id 更新到 story_tree_json 表中（只 UPDATE）
    """
    try:
        query = text("""
                     UPDATE story_tree_json
                     SET data        = jsonb_set(COALESCE(data, '{}'::jsonb), '{hook_id}', CAST(:hook_id AS jsonb)),
                         update_time = now()
                     WHERE task_id = :task_id
                     """)
        with global_engine.begin() as conn:
            result = conn.execute(query, {"hook_id": str(hook_id), "task_id": task_id})
            if result.rowcount > 0:
                logger.info(f"✅ 成功将 hook_id ({hook_id}) 更新至 story_tree_json, task_id: {task_id}")
            else:
                logger.warning(f"⚠️ 未找到 task_id: {task_id}，hook_id 更新可能被忽略！")
    except Exception as e:
        logger.error(f"❌ 更新 hook_id 失败: {e}")


def build_local_context(node_id, char_db_json):
    """构建单节点的局部角色映射，并进行命名碰撞预警"""
    local_role_mapping = {}
    prompt_lines = []

    if not char_db_json:
        return local_role_mapping, "", ""

    # 1. 过滤出当前节点在场的角色 (基于 node_appearances)
    present_chars = {}
    for char_key, info in char_db_json.items():
        appearances = info.get("node_appearances", {})
        # 只要该节点在出场列表中，或者该角色根本没配置出场列表（异常数据兜底）
        if node_id in appearances or not appearances:
            present_chars[char_key] = info

    # 若当前节点查不到任何角色，则兜底加载全局角色
    if not present_chars:
        present_chars = char_db_json

    # 2. 构建映射并进行哈希碰撞检测
    alias_to_owner = {}
    conflicts = set()

    for key, info in present_chars.items():
        real_name = info.get("name") or key
        local_role_mapping[real_name] = real_name

        # 检查主名碰撞
        if real_name in alias_to_owner and alias_to_owner[real_name] != real_name:
            conflicts.add(real_name)
        alias_to_owner[real_name] = real_name

        valid_aliases = []
        aliases = info.get("other_names", [])
        if isinstance(aliases, list):
            for alias in aliases:
                if alias and isinstance(alias, str):
                    clean = alias.strip()
                    if clean and clean != real_name:
                        local_role_mapping[clean] = real_name
                        valid_aliases.append(clean)

                        # 检查别名碰撞
                        if clean in alias_to_owner and alias_to_owner[clean] != real_name:
                            conflicts.add(clean)
                        alias_to_owner[clean] = real_name

        if valid_aliases:
            prompt_lines.append(f"{real_name} (别名/代称: {', '.join(valid_aliases)})")
        else:
            prompt_lines.append(real_name)

    local_vip_prompt_str = " | ".join(prompt_lines)

    # 3. 动态生成冲突警告 Flag
    conflict_warning = ""
    if conflicts:
        conflict_str = ", ".join(conflicts)
        conflict_warning = f"""
⚠️【极高优先级防混淆警告】:
在本段剧情中，以下称呼存在严重指代歧义（多角色共用或主别名重叠）：[{conflict_str}]。
提取时，绝不可进行无脑的字面替换！你**必须**严密结合上下文语境、动作描写及人物立场，推理出该称呼在本句中具体属于哪个角色的真名。
"""
        logger.warning(f"⚠️ [节点 {node_id}] 检测到名称冲突风险: {conflict_str}，已注入消歧义 Prompt。")

    return local_role_mapping, local_vip_prompt_str, conflict_warning


def clean_markdown_content(content):
    """切除无关元数据"""
    stop_markers = [r'\n###\s*后续选项', r'\n###\s*Next Options', r'\n###\s*Options']
    for marker in stop_markers:
        match = re.search(marker, content, re.IGNORECASE)
        if match:
            return content[:match.start()]
    return content


def robust_json_extract(text):
    match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    if match: return match.group(1).strip()
    start = text.find('[')
    end = text.rfind(']')
    if start != -1 and end != -1: return text[start:end + 1]
    return text.strip()


def is_chinese_content(text):
    return bool(re.search(r'[\u4e00-\u9fa5]', text))


def to_pascal_case_speaker_id(text):
    """生成合规的 speaker_id (拼音规则与驼峰规则)"""
    if not text:
        return None

    if re.search(r'[\u4e00-\u9fa5]', text):
        # 遵循所有ID使用拼音的原则
        return "".join([p.capitalize() for p in lazy_pinyin(text)])

    # 英文处理：去除特殊符号并转为驼峰
    text = re.sub(r"['’]s\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"['’]", "", text)
    text = re.sub(r"[^a-zA-Z0-9]", " ", text)

    parts = text.split()
    return "".join(part.capitalize() for part in parts)


def normalize_speaker_info(raw_name, role_mapping):
    if not raw_name: return None, None
    real_name = role_mapping.get(raw_name, raw_name)  # 修改这里
    real_id = to_pascal_case_speaker_id(real_name)
    return real_name, real_id


def split_text_with_overlap(full_text, chunk_size=5000, overlap_size=600):
    """切分文本并附带重叠上下文 (安全语义切分：防对话截断)"""
    lines = full_text.split('\n')
    chunks_data = []

    current_chunk = []
    current_length = 0
    previous_context_buffer = []

    for i, line in enumerate(lines):
        line_len = len(line) + 1

        # 当达到字数软限制时，寻找“安全切分点”
        if current_length + line_len > chunk_size:
            clean_line = line.strip()
            prev_clean_line = current_chunk[-1].strip() if current_chunk else ""

            # 判断是否处于连续对话交锋区 (当前行或上一行以引号开头)
            is_dialogue_active = clean_line.startswith(('“', '"', '「')) or \
                                 prev_clean_line.startswith(('“', '"', '「'))

            # 只有在“非连续对话”或者“超出硬限制(多容忍1000字)”时，才允许切断
            if not is_dialogue_active or current_length > chunk_size + 1000:
                if "".join(current_chunk).strip():
                    context_str = "".join(previous_context_buffer)[-overlap_size:] if previous_context_buffer else ""
                    chunks_data.append({
                        "text": "\n".join(current_chunk),
                        "context": context_str
                    })
                    previous_context_buffer.extend(current_chunk)
                    previous_context_buffer = previous_context_buffer[-overlap_size:]

                current_chunk = []
                current_length = 0

        current_chunk.append(line)
        current_length += line_len

    if current_chunk and "".join(current_chunk).strip():
        context_str = "".join(previous_context_buffer)[-overlap_size:] if previous_context_buffer else ""
        chunks_data.append({
            "text": "\n".join(current_chunk),
            "context": context_str
        })

    return chunks_data


def refine_emotion_by_keywords(content, original_emotion, beat_type):
    """Python 兜底情绪逻辑：增强上下文匹配与防误杀（废除标点武断判定）"""

    # 1. 旁白默认冷静
    if beat_type == 'narration':
        return "calm"

    # 2. 如果大模型已经给出了明确且非 calm 的情感，无条件信任大模型
    if original_emotion not in ["calm", "neutral", ""]:
        return original_emotion

    text_lower = content.strip().lower()

    # 3. 统计命中词频
    emotion_scores = {emo: 0 for emo in EMOTION_KEYWORDS}
    for emotion, keywords in EMOTION_KEYWORDS.items():
        for kw in keywords:
            # 判断是否为纯英文单词
            if re.match(r'^[a-zA-Z\s]+$', kw):
                # 英文使用正则 \b 匹配边界
                if re.search(rf'\b{kw}[a-z]*\b', text_lower):
                    if re.search(rf'\b(not|didn\'t|wasn\'t|isn\'t|never|no)\s+{kw}[a-z]*\b', text_lower):
                        continue
                    emotion_scores[emotion] += 1
            else:
                # 中文保留直接匹配
                if kw in text_lower:
                    # 极其轻量的否定词排除
                    if f"不{kw}" in text_lower or f"没{kw}" in text_lower:
                        continue
                    emotion_scores[emotion] += 1

    # 找出得分最高的情感
    best_emotion = max(emotion_scores, key=emotion_scores.get)

    # 如果有明确命中的情感词，应用兜底
    if emotion_scores[best_emotion] > 0:
        return best_emotion

    # 4. 如果大模型说是 calm，且词库也没命中任何动作词，直接返回原状态
    # （彻底废除了原有的标点符号强行兜底逻辑）
    return original_emotion


def clean_universal_content(text):
    if not text: return ""
    text = text.strip()

    # 遇到提取到的控制指令，直接放行不清洗
    if "effects_" in text or "◆◆◆" in text or "选项" in text:
        return text

    patterns = [r'^["“«\']+', r'["”»\']+$']
    for p in patterns: text = re.sub(p, '', text)

    text = re.sub(r'[\*_]+', '', text)
    text = re.sub(r'^>\s*', '', text)
    text = re.sub(r'~~.*?~~', '', text)

    if re.match(r'^[-*_]{3,}$', text):
        return ""

    if text.endswith(','): text = text[:-1] + '.'
    return text.strip()


# ================= 新增：物理切分解析器 (处理分支标记) =================
def parse_markdown_with_branches(content):
    """将 Markdown 文本切分为纯剧情块和结构标记块"""
    blocks = []
    current_text = []
    in_side_story = False

    for line in content.split('\n'):
        line = line.strip()
        if not line: continue

        # 1. 识别标准番外标题 (这里是你已有的代码)
        if line.startswith('## 番外'):
            if current_text:
                blocks.append({"type": "text", "content": "\n".join(current_text)})
                current_text = []

            if in_side_story:
                blocks.append({"type": "side_story_end", "content": "番外结束，回到主线"})

            clean_side = line.replace('#', '').strip()
            blocks.append({"type": "side_story", "content": clean_side})
            in_side_story = True
            continue
            # 2. 识别普通标题 (主线剧情的开始)
        elif line.startswith('#'):
            if current_text:
                blocks.append({"type": "text", "content": "\n".join(current_text)})
                current_text = []

            # 💡 核心逻辑：如果在番外中遇到了普通标题，自动插入结束标记
            if in_side_story:
                blocks.append({"type": "side_story_end", "content": "番外结束，回到主线"})
                in_side_story = False  # 重置状态

            continue  # 普通标题依然跳过，不提正文

        # ==================== 👇 新增：过滤脏标题 👇 ====================
        elif re.match(r'^番外[一二三四五六七八九十百0-9]+', line) and len(line) < 20:
            continue
        # ==================== 👆 新增结束 👆 ====================

        # 2. 识别分支结构标记 (这里是你已有的代码)
        elif '软分支选择' in line:
            if current_text:
                blocks.append({"type": "text", "content": "\n".join(current_text)})
                current_text = []
            blocks.append({"type": "branch_start", "content": line})

        elif '软分支结束' in line:
            if current_text:
                blocks.append({"type": "text", "content": "\n".join(current_text)})
                current_text = []
            blocks.append({"type": "branch_end", "content": line})


        elif re.search(r'^.*?\[?选项\s*[A-Z]\]?', line):
            if current_text:
                blocks.append({"type": "text", "content": "\n".join(current_text)})
                current_text = []
            clean_opt = re.sub(r'^.*?选项\s*[A-Z]\]?[：:\s]*', '', line).strip()
            clean_opt = re.sub(r'^[^\w\u4e00-\u9fa5]+', '', clean_opt).strip()
            blocks.append({"type": "option", "content": clean_opt, "raw": line})

        elif line.startswith('<!--') and 'effects' in line:
            if current_text:
                blocks.append({"type": "text", "content": "\n".join(current_text)})

                current_text = []

            clean_effect = re.sub(r'<!--\s*(.*?)\s*-->', r'\1', line).strip()

            blocks.append({"type": "variable_update", "content": clean_effect})

        else:
            current_text.append(line)

    if in_side_story:
        if current_text:
            blocks.append({"type": "text", "content": "\n".join(current_text)})
            current_text = []
        blocks.append({"type": "side_story_end", "content": "番外结束，回到主线"})

    if current_text:
        blocks.append({"type": "text", "content": "\n".join(current_text)})

    return blocks


@retry(stop=stop_after_attempt(5), wait=wait_random_exponential(min=1, max=60))
async def extract_beats_async(client, chunk_data, semaphore, vip_prompt_str, conflict_warning):
    chunk_text = chunk_data['text']
    prev_context = chunk_data['context']

    vip_list_str = vip_prompt_str if vip_prompt_str else "None provided"

    is_cn = is_chinese_content(chunk_text)

    if is_cn:
        npc_guidelines = """
    - 如果确定该角色不在 VIP 名单中，请直接提取原文中的中文称呼或名字，**绝对不要翻译成英文！**
    - 例如：原文是“车夫”，就输出“车夫”；原文是“翠儿”，就输出“翠儿”；原文是“老和尚”，就输出“老和尚”。
    - 规则：避免极其冗长的描述性名称。
      - 错误示例："卖花的老奶奶"
      - 正确示例："老奶奶" 或 "卖花女"
    """
    else:
        npc_guidelines = """
    - If the character is definitely NOT in the VIP list, use a generic Role Name in PascalCase.
    - Examples: "Driver", "Waiter", "PoliceOfficer", "Stranger", "Crowd".
    - Rule: Avoid long descriptive names. 
      - Bad: "The old lady selling flowers"
      - Good: "FlowerSeller"
    """

    user_content_str = f"""
<previous_context>
{prev_context}
</previous_context>

<current_text>
{chunk_text}
</current_text>
"""

    if is_cn:
        system_prompt = f"""
# Role
视觉小说脚本专家 (Director Mode).

# Task
将 <current_text> 中的小说内容提取为视觉小说脚本格式（JSON）。
**注意：<previous_context> 仅供你“阅读”以理解人物关系（比如识别“她”是谁），严禁提取 Context 中的内容！**

{conflict_warning}

# 角色管理系统

## GROUP A: VIPs (主角名单)
**你必须将文中的描述性称呼映射为以下真名:**
[{vip_list_str}]

* **规则**: 如果角色的外貌/行为符合 VIP 特征，**必须使用真名**。
* *例子*: 如果名单里有 "Lestat"，文中 "那个穿天鹅绒外套的男人" -> `Lestat`。

## GROUP B: NPCs (路人规范)
如果确定不是 VIP，请使用标准化的职业/身份词 (PascalCase):
{npc_guidelines}

# CRITICAL RULES (核心铁律)

1. **角色定位 (角色对齐 - 极重要)**:
   - 严格执行上述 GROUP A 和 GROUP B 的规则。
   - 遇到 "他/她" 或 "那个男人"，必须结合上下文判断是 VIP 还是 NPC，填入准确名字。

2. **旁白瘦身 (Narration Reduction - 极重要)**:
   - **视觉排重**: 假设画面已经展示了环境和人物立绘。**删掉**对衣着、家具、天气、五官的静态描写。
   - **保留核心**: 仅保留 **心理活动**、**嗅觉/触觉**、**抽象氛围**、**关键动作**。
   - *Case 1*: 原文“她穿着一件白色的连衣裙，裙摆随风飘扬，站在树下。” -> 提取为 **(删除，因为立绘和背景已有)** 或 简写为 "风吹过树下。"
   - *Case 2*: 原文“空气中弥漫着消毒水的味道，让他想起三年前的那个夜晚。” -> 提取为 **"空气中弥漫着刺鼻的消毒水味，勾起了冰冷的回忆。"** (保留气味和联想)

3. **角色情绪 (Character Emotion - High Priority)**:
   - **对象**: 仅针对 **对话 (Dialogue)** 赋予明确情感，旁白 (Narration) 保持 'calm'。
   - **指令**: 你必须具备极强的“上下文穿透力”。不要只看台词本身，**必须结合这句台词前后的“动作描写”、“神态描写”（如“冷笑”、“哽咽”、“颤抖着说”）来反推台词的情感**。
   - **绝对红线**: 只要人物表现出明显情绪起伏，**严禁输出 'calm'**。
   - **映射逻辑与推断示例**:
     - 动作含“吼”、“冷笑”、“拍桌子” 或 台词含“滚！” -> `angry`
     - 动作含“哽咽”、“落泪” 或 台词含“求你了” -> `sad`
     - 动作含“愣住”、“猛地抬头” 或 台词含“怎么会？” -> `surprised`
     - 动作含“发抖”、“后退” 或 台词含“别过来” -> `fearful`
     - 动作含“微笑”、“松了口气” -> `happy`
     - 动作含“皱眉”、“移开视线” 或 台词含“真恶心” -> `disgusted`

4. **代词消解 (Pronoun Resolution)**:
   - 必须结合 <previous_context> 将文中出现的“她”、“他”、“那个男人”、“说话者”替换为**角色的真名**（如“楚瑶”、“厉寒霆”）。
   - **严禁输出代词**：`speaker_name` 字段绝对不允许出现 "她", "他", "男人", "女人", "未知", "说话者"。

5. **动态场景追踪 (Dynamic Scene Tracking - 必须严格执行)**:
   - **大局观 (AVG 静态背景逻辑)**：你正在提取的是视觉小说 (AVG) 静态背景图，背景是变动缓慢的美术资产。只要没发生“跨大地图转移”（如从室内走到荒野）或“显著昼夜交替”，场景信息必须与上一句完全一致！
   - **强制输出临时字段**：你必须为每一句话在 JSON 中额外输出 `location` (地点), `time` (时间), `feature` (特征) 字段。只能使用全小写英文和下划线。
     - `location` (大地图)：忽略微观移动。比如从床边走到院子，统统归入 `house_interior`。**（【严重警告】：严禁直接抄袭本提示词中的示例单词！你必须根据原文实际地点自己命名，如 `clinic`, `snow_mountain`, `dungeon` 等！）**
     - `time` (大光影)：不区分 dawn, early_morning 等微小变化，统一强制归类为 `morning`、`day` 或 `night`。
     - `feature` (大气候)：只能是长期物理特征（如 `foggy`, `heavy_snow`）。严禁使用微观感受。

6. **对话清洗**:
   - 去除对话标签（如“他冷笑道”、“厉寒霆问”），只保留引号内的台词。

# ONE-SHOT EXAMPLE (示范)

## Input:
<current_text>
水晶吊灯照亮了奢华的客厅。厉寒霆坐在黑色的轮椅上。
“你走吧。”他闭上眼。
楚瑶看着他，心如刀绞。
随后，楚瑶跑出了门外，站在大雨的街道上。
</current_text>

## Output JSON:
[
  {{ "beat_type": "narration", "speaker_name": "旁白", "content": "厉寒霆坐在黑色的轮椅上。", "emotion": "calm", "location": "luxurious_living_room", "time": "night", "feature": "chandelier" }},
  {{ "beat_type": "dialogue", "speaker_name": "厉寒霆", "content": "你走吧。", "emotion": "sad", "location": "luxurious_living_room", "time": "night", "feature": "chandelier" }},
  {{ "beat_type": "narration", "speaker_name": "旁白", "content": "楚瑶心如刀绞。", "emotion": "calm", "location": "luxurious_living_room", "time": "night", "feature": "chandelier" }},
  {{ "beat_type": "narration", "speaker_name": "旁白", "content": "楚瑶跑出了门外。", "emotion": "calm", "location": "street", "time": "night", "feature": "heavy_rain" }}
]
(注意：
 1. 删掉了“水晶吊灯”、“黑色轮椅”（因为那是画面）。
 2. 保留了“心如刀绞”（心理）。
 3. 前三句都在客厅，所以 location, time, feature 保持绝对一致，直到第四句跑出门外才改变 location。)

# Output Format
JSON Array `[...]`.
"""
    else:
        system_prompt = f"""
# Role
Visual Novel Script Expert(Director Mode).

# Task
Extract structured script data from <current_text> ONLY.
**Use <previous_context> as READ-ONLY reference to identify characters (e.g., resolving 'he'/'she'). DO NOT extract text from context.**

{conflict_warning}

# CAST SYSTEM (CRITICAL)

## GROUP A: THE VIPs (Strict Mapping)
**You must map any descriptions of these characters to their Real Names:**
[{vip_list_str}]

* **Rule**: If a character matches the description of a VIP, YOU MUST USE THE VIP NAME.
* *Example*: If "Daniel" is in the list, "The boy" -> `Daniel`.
* *Example*: If "Lestat" is in the list, "The man in velvet" -> `Lestat`.

## GROUP B: THE NPCs (Standardized Roles)
If a character is definitely NOT a VIP, use a **Standardized Role Name**:
{npc_guidelines}

# CRITICAL RULES

1.  **Cast Alignment (High Priority)**:
    - Strictly enforce GROUP A and GROUP B logic.
    - Resolve "He"/"She"/"The Man" to a specific VIP Name or NPC Role.
    - **Forbidden**: Do not output "The Man", "The Boy", "Stranger" if they are VIPs.

2. **Narration Reduction (CRITICAL)**:
   - **Visual Filtering**: Assume the Background (BG) and Sprites are visible. **REMOVE** descriptions of clothes, furniture, weather, and facial features if static.
   - **Keep**: Internal thoughts, Smells, Touch, Abstract Atmosphere, Critical Actions.
   - *Example*: "She stood there in a red dress." -> **DELETE** (Sprite shows it).
   - *Example*: "The smell of blood made him sick." -> **KEEP** (Can't see smell).

3. **Character Emotion (High Priority)**:
   - **Target**: Assign explicit emotions to **Dialogue** ONLY. Narration MUST remain `calm`.
   - **Directive**: You must demonstrate strong "Contextual Penetration." Do not look at the spoken line in isolation. **You MUST infer the dialogue's emotion by analyzing the surrounding "action descriptions" and "facial expressions" (e.g., "sneered", "sobbed", "said tremblingly").**
   - **Absolute Red Line**: If a character shows obvious emotional fluctuation, **you are FORBIDDEN to output 'calm'**.
   - **Mapping Logic & Inference Examples**:
     - Actions include "roared", "sneered", "slammed the table" OR dialogue includes "Get out!" -> `angry`
     - Actions include "sobbed", "shed tears" OR dialogue includes "Please..." -> `sad`
     - Actions include "stunned", "head snapped up" OR dialogue includes "How could it be?" -> `surprised`
     - Actions include "shivered", "stepped back" OR dialogue includes "Don't come closer!" -> `fearful`
     - Actions include "smiled", "sighed in relief" -> `happy`
     - Actions include "frowned", "looked away" OR dialogue includes "Disgusting" -> `disgusted`

4. **Pronoun Resolution (Identity Persistence)**:
   - You MUST resolve pronouns ("he", "she", "the man") to **Real Names** (e.g., "Louis", "Lestat") using the context.
   - **FORBIDDEN**: Do not output "She", "He", "Man", "Woman", "Unknown", "The Speaker". as `speaker_name`.

5. **Dynamic Scene Tracking (Macro Background Logic - CRITICAL)**:
   - **AVG Mindset**: You are extracting data for Static Backgrounds in a Visual Novel (AVG), NOT a dynamic movie storyboard!
   - **Extreme Inertia Principle**: Unless physical macro-map travel occurs (e.g., from an interior to a wilderness) or a significant lighting shift occurs (day turns to night), the scene information MUST REMAIN 100% IDENTICAL to the previous beat.
   - **Required Fields**: You MUST output `location`, `time`, and `feature` for EVERY beat. Use ONLY lowercase English words and underscores.
     - `location` (Macro Maps): Ignore micro-movements. Walking from a bed to a courtyard is still just `house_interior`. **(CRITICAL WARNING: DO NOT copy example words from this prompt! You must generate your own names based on the text, e.g., `clinic`, `snow_mountain`, `dungeon`!)**
     - `time` (Macro Lighting): Standardize as `morning`, `day`, or `night`.
     - `feature` (Macro Climate): Use lasting physical conditions (e.g., `foggy`, `heavy_snow`). FORBIDDEN: micro-feelings.

6. **Dialogue Cleaning**:
   - Remove speech tags (e.g., "he said", "cried Louis"). Keep only the spoken dialogue.

# ONE-SHOT EXAMPLE (Complete Demo)

## Input:
<current_text>
The full moon shone on the gothic castle.
"Who are you?" the boy asked, trembling.
The man smiled, revealing fangs. "I am death."
A driver outside shouted, "Move it!"
</current_text>

## Output JSON:
[
  {{ "beat_type": "dialogue", "speaker_name": "Daniel", "content": "Who are you?", "emotion": "fearful", "location": "gothic_castle", "time": "night", "feature": "full_moon" }},
  {{ "beat_type": "narration", "speaker_name": "Narrator", "content": "The man smiled, revealing fangs.", "emotion": "calm", "location": "gothic_castle", "time": "night", "feature": "full_moon" }},
  {{ "beat_type": "dialogue", "speaker_name": "Lestat", "content": "I am death.", "emotion": "happy", "location": "gothic_castle", "time": "night", "feature": "full_moon" }},
  {{ "beat_type": "dialogue", "speaker_name": "Driver", "content": "Move it!", "emotion": "angry", "location": "castle_exterior", "time": "night", "feature": "carriage" }}
]
(Note: 
 1. **SLIMMING (Detailed)**: 
    - **DELETED Background**: "moon", "castle", "curtains" (Visual Redundancy).
    - **DELETED Minor Action**: "smoothed his lace cuffs" (Sprite Detail).
    - **KEPT Action**: "revealing fangs" (Critical Plot Point).
 2. **MAPPING**: 'The boy' -> Daniel, 'The man' -> Lestat.
 3. **FORMAT**: No markdown.
 4. **NPC**: 'Driver' standardized.
 5. DELETED Background: "moon", "castle" (Visual Redundancy).
 6. KEPT Action: "revealing fangs" (Critical Plot Point).
 7. The first three beats share the exact same location, time, and feature. The location only changes when the driver outside speaks.)

# Output Format
JSON Array `[...]`.
"""

    async with semaphore:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_content_str}
            ],
            temperature=0.3
        )
        if _usage_tracker and response.usage:
            _usage_tracker.add_llm(response.usage.prompt_tokens, response.usage.completion_tokens, MODEL_NAME)
        return response.choices[0].message.content


def post_process_beats_final(beats_stream_data, role_mapping):
    """清洗并将数据扁平化返回，恢复 speaker_id 逻辑"""
    processed_list = []

    for beat in beats_stream_data:
        if beat is None:
            continue

        # 如果是系统生成的结构化节点，直接放行，避免覆盖
        if beat.get("speaker_id") == "System":
            processed_list.append(beat)
            continue

        raw_content = beat.get("content") or beat.get("text", "")
        clean_content = clean_universal_content(raw_content)

        if not clean_content: continue

        raw_speaker_name = beat.get("speaker_name")
        raw_speaker_id = beat.get("speaker_id")
        beat_type = beat.get("beat_type", "narration")

        is_narrator = False
        if beat_type == "narration": is_narrator = True
        if raw_speaker_name in ["Narrator", "旁白", "System", "Story"]: is_narrator = True

        if is_narrator:
            beat_type = "narration"
            final_speaker_id = "PangBai"
            if is_chinese_content(clean_content):
                final_speaker_name = "旁白"
            else:
                final_speaker_name = "Narrator"
        else:
            source_name = raw_speaker_name if raw_speaker_name else raw_speaker_id
            final_speaker_name, final_speaker_id = normalize_speaker_info(source_name, role_mapping)

            if not final_speaker_name:
                final_speaker_name = source_name or "Unknown"
                final_speaker_id = to_pascal_case_speaker_id(final_speaker_name)

            beat_type = "dialogue"

        raw_emotion = beat.get("emotion", "calm")
        if raw_emotion not in ALLOWED_EMOTIONS:
            raw_emotion = "calm"

        final_emotion = refine_emotion_by_keywords(clean_content, raw_emotion, beat_type)

        # 宽容提取逻辑：防止大模型大小写输出错误
        loc = beat.get("location") or beat.get("Location") or "unknown"
        time_val = beat.get("time") or beat.get("Time") or "unknown"
        feat = beat.get("feature") or beat.get("Feature") or "none"

        new_beat = {
            "beat_type": beat_type,
            "speaker_name": final_speaker_name,
            "speaker_id": final_speaker_id,
            "content": clean_content,
            "emotion": final_emotion,
            "_loc": str(loc).lower().strip(),
            "_time": str(time_val).lower().strip(),
            "_feat": str(feat).lower().strip()
        }
        processed_list.append(new_beat)

    return processed_list


async def process_node_content(client, node_id, raw_content, semaphore, char_data):
    """处理单个节点的内容 (融入块切分机制)"""

    local_role_mapping, local_vip_prompt_str, conflict_warning = build_local_context(node_id, char_data)

    # 第一步：用解析器物理切块
    blocks = parse_markdown_with_branches(raw_content)
    stream_data = []

    text_tasks = []
    text_block_indices = []

    # 搜集所有纯文本块，送去大模型
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            clean_content = clean_markdown_content(block["content"])
            if not clean_content.strip(): continue

            chunk_size = 2000 if is_chinese_content(clean_content) else 6000
            overlap_size = 300 if is_chinese_content(clean_content) else 900
            chunks_data = split_text_with_overlap(clean_content, chunk_size, overlap_size)

            for chunk in chunks_data:
                text_tasks.append(extract_beats_async(client, chunk, semaphore, local_vip_prompt_str, conflict_warning))
                text_block_indices.append(i)

    pbar = tqdm(total=len(text_tasks), desc=f"Processing {node_id}", leave=False)

    async def wrap_with_progress(task):
        try:
            return await task
        except Exception as e:
            logger.error(f"❌ 块处理异常 (Chunk Error): {e}")
            return None
        finally:
            pbar.update(1)

    wrapped_tasks = [wrap_with_progress(t) for t in text_tasks]
    raw_outputs = await asyncio.gather(*wrapped_tasks)
    pbar.close()

    # 第二步：将大模型输出和控制节点按原顺序缝合
    task_idx = 0
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            while task_idx < len(text_block_indices) and text_block_indices[task_idx] == i:
                ai_output = raw_outputs[task_idx]
                if ai_output:
                    clean_json = robust_json_extract(ai_output)
                    try:
                        chunk_beats = json.loads(clean_json)
                        if isinstance(chunk_beats, dict):
                            for v in chunk_beats.values():
                                if isinstance(v, list): chunk_beats = v; break

                        if isinstance(chunk_beats, list):
                            for b in chunk_beats: stream_data.append(b)
                    except json.JSONDecodeError as e:
                        logger.error(f"❌ JSON 解析失败! Node: {node_id}, Error: {e}")
                        logger.error(f"📦 原始截断数据:\n{clean_json}")
                task_idx += 1
        else:
            # 拼装控制结构节点
            if block["type"] in ["side_story", "side_story_end"]:
                sys_beat = {
                    "beat_type": "narration",
                    "content": block["content"],
                    "speaker_name": "旁白",
                    "speaker_id": "PangBai",
                    "emotion": "calm",
                    "location": "title_screen", "time": "day", "feature": "black"
                }
            else:
                sys_beat = {
                    "beat_type": block["type"], "content": block["content"],
                    "speaker_name": "System", "speaker_id": "System",
                    "emotion": "calm", "_loc": "unknown", "_time": "unknown", "_feat": "none"
                }
            if block["type"] == "option": sys_beat["raw_option"] = block["raw"]
            stream_data.append(sys_beat)

    if stream_data:
        return node_id, post_process_beats_final(stream_data, local_role_mapping)
    return node_id, []


def clean_orphan_beats(beats, threshold=2):
    """
    后处理兜底：消除大模型产生的微观移动幻觉（1~2句话的场景孤岛）。
    """
    if not beats: return beats

    def fmt(s):
        return re.sub(r'[^a-z0-9]', '_', str(s).lower()).strip('_')

    def get_core_str(b):
        loc = b.get("_loc", "unknown")
        time_val = b.get("_time", "unknown")
        return f"scene_{fmt(loc)}_{fmt(time_val)}"

    # 1. 按照物理核心地点进行分组打包
    groups = []
    current_group = []
    current_core = get_core_str(beats[0]) if "_loc" in beats[0] else "SYSTEM"

    for beat in beats:
        # 跳过系统结构节点，防止打乱分支逻辑
        if beat.get("is_hook") or beat.get("beat_type") in ["branch_start", "branch_end", "option", "variable_update"]:
            if current_group:
                groups.append({"core": current_core, "beats": current_group})
            groups.append({"core": "SYSTEM", "beats": [beat]})
            current_group = []
            current_core = None
            continue

        core = get_core_str(beat)
        if current_core is None: current_core = core

        if core == current_core:
            current_group.append(beat)
        else:
            if current_group:
                groups.append({"core": current_core, "beats": current_group})
            current_core = core
            current_group = [beat]

    if current_group:
        groups.append({"core": current_core, "beats": current_group})

    # 2. 遍历查找“孤岛”并进行同化合并
    i = 1
    while i < len(groups) - 1:
        group = groups[i]
        prev_group = groups[i - 1]
        next_group = groups[i + 1]

        # 触发条件：当前是剧情节点，且长度 <= 2（即只有1-2句话）
        if group["core"] != "SYSTEM" and len(group["beats"]) <= threshold:
            # 判断是否是幻觉孤岛：被夹在两个相同的宏观地点中间
            if prev_group["core"] == next_group["core"] and prev_group["core"] != "SYSTEM":
                # 强制同化：将这几句话的地点、时间、特征，强行修改为和上一个场景一样
                ref_beat = prev_group["beats"][0]
                for b in group["beats"]:
                    b["_loc"] = ref_beat.get("_loc", "unknown")
                    b["_time"] = ref_beat.get("_time", "unknown")
                    b["_feat"] = ref_beat.get("_feat", "none")

                # 将被同化的 beat 并入前一个组，消灭孤岛
                prev_group["beats"].extend(group["beats"])
                groups.pop(i)
                continue  # 因为弹出了元素，索引保持不变，继续检查当前位置的新元素
        i += 1

    # 3. 将洗净的数组重新拍平
    cleaned_beats = []
    for g in groups:
        cleaned_beats.extend(g["beats"])

    return cleaned_beats


# ================= 主入口 =================

async def main(target_task_id=None):
    """执行提取流程"""
    task_id, char_data, md_contents = fetch_source_context(target_task_id)
    if not task_id: return None

    if _usage_tracker:
        _usage_tracker.set_context(task_id, "audio_producer")

    client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    master_script = {}
    hook_beats_map = {}  # 缓存序幕剥离的剧情数据

    logger.info("--- [Plots] 开始提取 (DB Mode) ---")

    tasks = []
    for node_id, content in md_contents.items():
        if content.strip().startswith("PangBai"):
            logger.info(f"⏭️ Skipping {node_id} because content starts with 'PangBai'")
            continue

        # --- 核心拦截与剥离：独立处理序幕 ---
        if "chapter_1" in node_id:
            pattern = re.compile(r'═+[\r\n]+◆◆◆\s*序幕结束\s*·\s*正文开始\s*◆◆◆[\r\n]+═+')
            parts = pattern.split(content)
            if len(parts) == 2:
                hook_text = parts[0]
                content = parts[1]  # 将正文继续留给下面交给大模型提取
                hook_beats = process_hook_section(hook_text)
                if hook_beats:
                    hook_beats_map[node_id] = hook_beats
                    await asyncio.to_thread(update_story_tree_hook_id, task_id, len(hook_beats))
            else:
                logger.warning(f"⚠️ 在 {node_id} 中未找到合规的序幕分割线，将视为全正文处理。")

        tasks.append(process_node_content(client, node_id, content, semaphore, char_data))

    for completed_task in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Extracting"):
        node_id, beats = await completed_task
        if beats:
            master_script[node_id] = beats
            logger.info(f"   ✅ {node_id}: {len(beats)} beats (raw)")

    # 缝合序列：将剥离出来纯 Python 处理的 hook_beats 追加到对应节点的最前面
    for node_id, hook_b in hook_beats_map.items():
        if node_id in master_script:
            master_script[node_id] = hook_b + master_script[node_id]
        else:
            master_script[node_id] = hook_b

    if master_script:

        # ================= 全局序号统筹与切片融合 =================
        global_scene_counter = {}
        sorted_nodes = sorted(master_script.keys())

        current_base_str = ""
        current_core_scene_str = ""
        current_seq = 0
        current_chunk_size = 0
        last_emotion = "calm"
        last_speaker_id = ""

        SOFT_LIMIT = 13
        HARD_LIMIT = 20

        def fmt(s):
            return re.sub(r'[^a-z0-9]', '_', str(s).lower()).strip('_')

        for node_id in sorted_nodes:
            beats = master_script[node_id]
            beats = clean_orphan_beats(beats, threshold=2)
            final_ordered_beats = []

            node_beat_idx = 0
            in_branch = False
            current_option = None
            option_counters = {'A': 0, 'B': 0}
            pending_option_prefix = None

            for beat in beats:
                is_hook = beat.pop("is_hook", False)
                raw_b_type = beat.get("beat_type", "dialogue")
                content = str(beat.get("content", "")).strip()

                if is_hook:
                    node_beat_idx += 1
                    ordered_beat = {
                        "beat_id": str(node_beat_idx),
                        "scene_id": None,  # <--- 强制设置为 None (存入数据库后就是 null)
                        "beat_type": beat.get("beat_type"),
                        "speaker_name": beat.get("speaker_name"),
                        "speaker_id": beat.get("speaker_id"),
                        "content": content,
                        "emotion": beat.get("emotion")
                    }
                    final_ordered_beats.append(ordered_beat)
                    continue

                # 分支控制信号处理
                if raw_b_type == "branch_start":
                    in_branch = True
                    current_option = None
                    option_counters = {'A': 0, 'B': 0}
                    pending_option_prefix = None
                    continue

                if raw_b_type == "branch_end":
                    in_branch = False
                    current_option = None
                    pending_option_prefix = None
                    continue

                if raw_b_type == "option":
                    in_branch = True
                    raw_opt_text = beat.get("raw_option", content)
                    match = re.search(r'选项\s*([A-Z])', raw_opt_text)
                    if match:
                        current_option = match.group(1)
                    else:
                        current_option = 'B' if option_counters.get('A', 0) > 0 else 'A'
                    if current_option not in option_counters: option_counters[current_option] = 0

                    pending_option_prefix = content
                    continue

                if raw_b_type == "variable_update":
                    current_beat_id = f"{node_beat_idx}-C"
                    effective_b_type = "variable_update"
                    pending_option_prefix = None
                else:
                    effective_b_type = raw_b_type
                    if not in_branch:
                        node_beat_idx += 1
                        current_beat_id = str(node_beat_idx)
                    else:
                        if not current_option: current_option = 'A'
                        option_counters[current_option] += 1
                        current_beat_id = f"{node_beat_idx}-{current_option}{option_counters[current_option]}"

                if pending_option_prefix and effective_b_type in ["dialogue", "narration"]:
                    content = f"{pending_option_prefix}||{content}"
                    pending_option_prefix = None

                loc = beat.pop("_loc", "unknown")
                time_val = beat.pop("_time", "unknown")
                feat = beat.pop("_feat", "none")

                # 核心防抖判定依据：只看地点和时间
                core_scene_str = f"scene_{fmt(loc)}_{fmt(time_val)}"

                # 完整的场景名称，保留特征供生图使用
                base_str = f"scene_{fmt(loc)}_{fmt(time_val)}_{fmt(feat)}"
                base_str = re.sub(r'_+', '_', base_str)

                current_emotion = beat.get("emotion", "calm")
                is_break = False

                # 条件 A：物理场景转移防抖验证
                if core_scene_str != current_core_scene_str:
                    is_break = True
                # 条件 B：长对话切片逻辑
                else:
                    if effective_b_type in ["dialogue", "narration"]:
                        if current_chunk_size >= SOFT_LIMIT:
                            if effective_b_type == "narration":
                                is_break = True
                            elif current_emotion in ["fearful", "angry",
                                                     "surprised"] and current_emotion != last_emotion:
                                is_break = True

                        if current_chunk_size >= HARD_LIMIT:
                            if effective_b_type == "narration":
                                is_break = True
                            elif beat.get("speaker_id") != last_speaker_id:
                                is_break = True
                            elif current_chunk_size >= 25:
                                is_break = True

                if is_break:
                    # 【核心修复】：无论是什么原因触发的 break，都必须把当前的场景状态作为新阶段的基准
                    current_core_scene_str = core_scene_str
                    current_base_str = base_str

                    global_scene_counter[current_base_str] = global_scene_counter.get(current_base_str, 0) + 1
                    current_seq = global_scene_counter[current_base_str]
                    current_chunk_size = 0

                if effective_b_type in ["dialogue", "narration"]:
                    current_chunk_size += 1
                    if effective_b_type == "dialogue":
                        last_emotion = current_emotion
                    last_speaker_id = beat.get("speaker_id")

                if effective_b_type == "variable_update":
                    ordered_beat = {
                        "beat_id": current_beat_id,
                        "scene_id": None,
                        "beat_type": effective_b_type,
                        "speaker_name": None,
                        "speaker_id": None,
                        "content": content,
                        "emotion": None
                    }
                else:
                    ordered_beat = {
                        "beat_id": current_beat_id,
                        "scene_id": f"{current_base_str}_{current_seq:02d}",
                        "beat_type": effective_b_type,
                        "speaker_name": beat.get("speaker_name"),
                        "speaker_id": beat.get("speaker_id"),
                        "content": content,
                        "emotion": current_emotion
                    }
                final_ordered_beats.append(ordered_beat)

            master_script[node_id] = final_ordered_beats

        logger.info(f"✅ 提取完成，包含 {len(master_script)} 个节点的剧情。")
        await asyncio.to_thread(save_to_plots_table, task_id, master_script)
        if _usage_tracker:
            _usage_tracker.flush()
        return task_id
    else:
        logger.error("❌ 未提取到任何有效剧情。")
        if _usage_tracker:
            _usage_tracker.flush()
        return None


if __name__ == "__main__":
    tid = asyncio.run(main(target_task_id="test_e85cff11cs"))
    if tid:
        logger.info(f"Process finished for Task ID: {tid}")
