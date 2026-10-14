import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
services_dir = os.path.join(project_root, "services")
utils_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "utils")
for p in (project_root, services_dir, os.path.normpath(utils_dir)):
    if p not in sys.path:
        sys.path.insert(0, p)

import json
from sqlalchemy import create_engine, text
from openai import OpenAI
from logger_config import setup_logger

from common.config import OUTPUTS_PATH, PLATFORM_DATABASE_URL
from common.llm_fallback import ModelFallback
from common.model_json import parse_model_json
from common.name_mapping_utils import get_change_name_section, get_original_conversion
from info_extractor.character.name_mappings import to_pascal_case_id

try:
    from common.usage_tracker import tracker as _usage_tracker
except ImportError:
    _usage_tracker = None

logger = setup_logger("Step_NameMap", "step_namemap.log")

DB_URL = PLATFORM_DATABASE_URL  # 跨模块共用表已迁平台库(agent_platform)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = OUTPUTS_PATH

MODEL_NAME = "qwen3.7-max"
# 主模型偶发不稳定时自动改用备选模型重发同一请求（同中转站、同调用参数）。
_model_fallback = ModelFallback(MODEL_NAME, name="NameMap", logger=logger)

def resolve_llm_credentials():
    """与 name_mappings.py 一致：走中转站的 OpenAI 兼容接口。

    三个变量名不同但值都是同一个中转站 key，DEEPSEEK_API_KEY 只是历史遗留的
    变量名（不是 DeepSeek 官方 key），作为最后一层兜底保留。
    """
    base_url = os.getenv("GPT_IMAGE_BASE_URL", "http://sefutech-api.kube-test.svc:3000").rstrip("/")
    api_key = (
        os.getenv("GPT_IMAGE_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
    )
    return f"{base_url}/v1", api_key

# 与 name_mappings.py 一致：超时按名称数量估算，只重试 1 次。
# 单次请求按名称数量估算，最大允许 1800 秒（30 分钟）；超时仍由调用层有限重试。
LLM_MAX_RETRIES = 1
LLM_TIMEOUT_BASE_SECONDS = 60.0
LLM_TIMEOUT_PER_NAME_SECONDS = 8.0
LLM_TIMEOUT_MAX_SECONDS = 1200.0


def llm_timeout_for(name_count):
    """按待处理名称数量估算单次请求超时上限。"""
    estimated = (
        LLM_TIMEOUT_BASE_SECONDS + LLM_TIMEOUT_PER_NAME_SECONDS * max(name_count, 0)
    )
    return min(estimated, LLM_TIMEOUT_MAX_SECONDS)
# 系统/旁白等非角色判定词，不写入映射（比较时统一转小写，故 Narration/NARRATION 均可命中）
SKIP_TOKENS = {'pangbai', '旁白', 'narration'}

# 旁白类 beat_type，直接排除
SKIP_BEAT_TYPES = {'narration', 'nvl_narration', 'system_panel', 'cg', 'variable_update'}


def is_chinese(string):
    for ch in str(string):
        if '一' <= ch <= '鿿':
            return True
    return False


def translate_names_batch(names, api_key):
    """批量翻译中文名；失败由调用方统一按完整 speaker ID/原名降级。"""
    base_url, resolved_key = resolve_llm_credentials()
    client = OpenAI(
        api_key=api_key or resolved_key,
        base_url=base_url,
        timeout=llm_timeout_for(len(names)),
        max_retries=LLM_MAX_RETRIES,
    )
    prompt = (
        "翻译以下角色名或称谓（中文），有些是人名，有些是描述性称谓，如：教数学的老师、小明的爸爸。\n"
        "【重要】英文人名保持中文顺序：姓在前、名在后。\n"
        "例如：李玉→Li Yu，萧无尘→Xiao Wuchen，顾辞→Gu Ci，沈清舒→Shen Qingshu。\n"
        "带‘·’的西式/奇幻姓名按其自身结构翻译；描述性称谓直接意译。\n"
        "输出纯 JSON，格式：{\"en\": {\"中文\": \"English\"}, \"ja\": {\"中文\": \"日本語\"}}\n"
        "不要输出任何其他内容。\n名称列表：" + json.dumps(names, ensure_ascii=False)
    )
    resp, used_model = _model_fallback.chat(
        client,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    if _usage_tracker and getattr(resp, "usage", None):
        _usage_tracker.add_llm_usage(
            resp.usage, used_model, provider="openai_compatible"
        )
    return parse_model_json(
        resp.choices[0].message.content, context="npc_translate", logger=logger
    )


def fetch_plot_data(target_task_id=None):
    """根据 Task ID 获取剧情数据，优先从数据库读取，失败则降级到本地文件"""
    engine = create_engine(DB_URL, pool_pre_ping=True)
    task_id = target_task_id

    try:
        with engine.connect() as conn:
            if not task_id:
                logger.info("🔍 未指定 Task ID，正在查找最新生成的剧情...")
                sql_latest = text("SELECT task_id FROM plots ORDER BY id DESC LIMIT 1")
                result = conn.execute(sql_latest).fetchone()
                if not result:
                    logger.error("❌ plots 表为空，无法提取。")
                    return None, None
                task_id = result[0]
                logger.warning(f"⚠️ 自动锁定最新任务: {task_id}")
            else:
                logger.info(f"🚀 [NameMap] 锁定指定任务: {task_id}")

            sql_data = text("SELECT data FROM plots WHERE task_id = :tid")
            result_data = conn.execute(sql_data, {"tid": task_id}).fetchone()
            if result_data:
                return task_id, result_data[0]

            logger.warning(f"⚠️ 未在 plots 表中找到 task_id='{task_id}' 的数据，尝试读取本地文件...")
    except Exception as e:
        logger.warning(f"⚠️ 数据库读取失败: {e}，尝试读取本地文件...")

    if task_id:
        fallback_path = os.path.join(OUTPUT_DIR, str(task_id), "plots.json")
        if os.path.exists(fallback_path):
            with open(fallback_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info(f"📁 [NameMap] 从本地文件加载 plots: {fallback_path}")
            return task_id, data

    logger.error(f"❌ 数据库和本地文件均未找到 task_id='{task_id}' 的 plots 数据")
    return None, None


def fetch_name_mappings(task_id):
    """获取 name_mappings：优先 name_mappings 表，降级到本地 {task_id}/name_mappings.json"""
    engine = create_engine(DB_URL, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            sql = text("SELECT data FROM name_mappings WHERE task_id = :tid")
            row = conn.execute(sql, {"tid": task_id}).fetchone()
            if row and row[0]:
                logger.info(f"🚀 [NameMap] 从 name_mappings 表加载: {task_id}")
                data = row[0]
                if isinstance(data, str):
                    data = json.loads(data)
                return data, ("db", None)
            logger.warning(f"⚠️ name_mappings 表中未找到 task_id='{task_id}'，尝试本地文件...")
    except Exception as e:
        logger.warning(f"⚠️ name_mappings 表读取失败: {e}，尝试本地文件...")

    local_path = os.path.join(OUTPUT_DIR, str(task_id), "name_mappings.json")
    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"📁 [NameMap] 从本地文件加载: {local_path}")
        return data, ("local", local_path)

    logger.warning(f"⚠️ 数据库和本地文件均未找到 task_id='{task_id}' 的 name_mappings，将新建空映射。")
    return None, (None, local_path)


def fetch_character_aliases(task_id):
    """返回 {别名: 角色库主名}，供 NPC 判重时把漏配的别名归并回主名。

    剧情提取若没能确认某角色的主名（如主名被误判剔除），full_name 会退成
    别名（"林雷" 而非 "林雷·巴鲁克"）。只按 full_name 判重的话，这个别名会
    被当成新 NPC 独立建档且不改名，与真主名的改名结果分裂成两条记录。
    读不到角色库时返回空表，判重退回原有的 full_name 口径。
    """
    char_data = None
    try:
        engine = create_engine(DB_URL, pool_pre_ping=True)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM characters WHERE task_id = :tid"),
                {"tid": task_id},
            ).fetchone()
        if row and row[0]:
            char_data = row[0]
            if isinstance(char_data, str):
                char_data = json.loads(char_data)
    except Exception as e:
        logger.warning(f"⚠️ characters 表读取失败，NPC 判重退回 full_name 口径: {e}")

    if char_data is None:
        local_path = os.path.join(OUTPUT_DIR, str(task_id), "characters.json")
        if os.path.exists(local_path):
            try:
                with open(local_path, "r", encoding="utf-8") as f:
                    char_data = json.load(f)
                logger.info(f"📁 [NameMap] 从本地文件加载角色库: {local_path}")
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"⚠️ 本地角色库读取失败，NPC 判重退回 full_name 口径: {e}")

    if not isinstance(char_data, dict):
        return {}

    main_names = set()
    for char_key, info in char_data.items():
        name = info.get("name") if isinstance(info, dict) else None
        main_name = str(name or char_key).strip()
        if main_name:
            main_names.add(main_name)

    alias_to_main = {}
    for char_key, info in char_data.items():
        if not isinstance(info, dict):
            continue
        main_name = str(info.get("name") or char_key).strip()
        if not main_name:
            continue
        aliases = info.get("other_names")
        if not isinstance(aliases, list):
            continue
        for alias in aliases:
            if not isinstance(alias, str):
                continue
            alias = alias.strip()
            if not alias or alias == main_name:
                continue
            # 一个别名被多个角色共用时指向不明，不能归并。
            if alias in alias_to_main and alias_to_main[alias] != main_name:
                alias_to_main[alias] = None
                continue
            alias_to_main.setdefault(alias, main_name)

    # 别名本身是另一个角色的主名（上游把"妇人"既建成独立角色、又写进"田氏"的
    # other_names）时，主名的权威性更高：它有自己的角色档案和改名结果，归并会把
    # 一个独立角色的记录并进别人名下。这类条目一律剔除。
    usurped = sorted(a for a in alias_to_main if a in main_names)
    ambiguous = sorted(a for a, m in alias_to_main.items() if m is None)
    resolved = {
        a: m for a, m in alias_to_main.items() if m and a not in main_names
    }
    if usurped:
        logger.warning(
            f"⚠️ {len(usurped)} 个名字既是独立主名、又被列为他人别名，"
            f"按主名处理不做归并（上游 other_names 数据有误）: {usurped[:8]}"
        )
    if ambiguous:
        logger.warning(
            f"⚠️ {len(ambiguous)} 个别名被多个角色共用，指代不明不做归并: {ambiguous[:8]}"
        )
    logger.info(f"✅ 已加载角色库别名索引: {len(resolved)} 条")
    return resolved


def collect_characters(plot_data):
    """从 plots 数据中收集 (full_name, speaker_id) 角色对，去重，跳过旁白。"""
    if isinstance(plot_data, str):
        plot_data = json.loads(plot_data)

    chars = {}  # full_name -> speaker_id (保留首次出现)
    for node_id, node_beats in plot_data.items():
        timeline = node_beats
        if isinstance(node_beats, dict) and 'script_timeline' in node_beats:
            timeline = node_beats['script_timeline']
        if not isinstance(timeline, list):
            continue

        for beat in timeline:
            beat_type = str(beat.get('beat_type') or '').strip().lower()
            speaker_id = beat.get('speaker_id')
            full_name = beat.get('full_name')

            fn_l = str(full_name).strip().lower() if full_name else ''

            if beat_type in SKIP_BEAT_TYPES:
                # narration 类 beat 一般是旁白，没有具体角色，予以跳过；
                # 但当 full_name 明确指向某个非旁白角色（如"系统"）时，仍需保留。
                if not (beat_type == 'narration' and full_name and fn_l not in SKIP_TOKENS):
                    continue

            if not speaker_id or not full_name:
                continue

            sid_l = str(speaker_id).strip().lower()
            if sid_l in SKIP_TOKENS or fn_l in SKIP_TOKENS:
                continue

            full_name = str(full_name).strip()
            speaker_id = str(speaker_id).strip()
            if full_name not in chars:
                chars[full_name] = speaker_id

    return chars


def save_name_mappings_to_db(task_id, mappings):
    """将 name_mappings 写回数据库（按 task_id upsert，jsonb 字段）。"""
    engine = create_engine(DB_URL, pool_pre_ping=True)
    payload = json.dumps(mappings, ensure_ascii=False)
    sql = text("""
        INSERT INTO name_mappings (task_id, data, create_time, update_time)
        VALUES (:tid, CAST(:data AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (task_id)
        DO UPDATE SET data = EXCLUDED.data, update_time = CURRENT_TIMESTAMP
    """)
    try:
        with engine.begin() as conn:
            conn.execute(sql, {"tid": task_id, "data": payload})
        logger.info(f"🗄️  [NameMap] 已写回数据库 name_mappings: {task_id}")
        return True
    except Exception as e:
        logger.error(f"❌ 写回数据库失败: {e}")
        return False


def update_name_mappings(target_task_id=None):
    task_id, plot_data = fetch_plot_data(target_task_id)
    if not plot_data:
        return

    mappings, (source, local_path) = fetch_name_mappings(task_id)
    if mappings is None:
        mappings = {}
    # 统一为新结构；读取时兼容旧扁平格式，写回时只保留
    # original_name/change_name 两个命名空间。
    has_new_structure = isinstance(mappings.get("change_name"), dict)
    change_source = get_change_name_section(mappings)
    original_conversion = get_original_conversion(mappings) if has_new_structure else {}
    new_name = dict(change_source.get("new-name") or {})
    conversion = dict(change_source.get("conversion") or {})
    zh_cn = dict(change_source.get("zh-CN") or {})
    en_us = dict(change_source.get("en-US") or {})
    ja_jp = dict(change_source.get("ja-JP") or {})

    if not original_conversion:
        used_original_ids = set()
        for original_name in new_name:
            base_id = to_pascal_case_id(original_name)
            original_id = base_id
            suffix = 2
            while original_id in used_original_ids:
                original_id = f"{base_id}_{suffix}"
                suffix += 1
            used_original_ids.add(original_id)
            original_conversion[original_name] = original_id

    mappings = {
        "original_name": {"conversion": original_conversion},
        "change_name": {
            "new-name": new_name,
            "conversion": conversion,
            "zh-CN": zh_cn,
            "en-US": en_us,
            "ja-JP": ja_jp,
        },
    }

    chars = collect_characters(plot_data)
    logger.info(f"🎭 共发现 {len(chars)} 个有效角色")

    alias_to_main = fetch_character_aliases(task_id)

    # 收集新增的 NPC（conversion 中不存在的）
    new_npcs = {}  # full_name -> speaker_id
    for full_name, speaker_id in chars.items():
        # 剧情里的 full_name 若只是角色库某主名的别名，按主名判重，
        # 否则同一个人会分裂成"主名（已改名）+ 别名（不改名）"两条记录。
        canonical_name = alias_to_main.get(full_name)
        lookup_name = canonical_name or full_name
        mapped_name = new_name.get(lookup_name, lookup_name)
        existing_id = conversion.get(mapped_name) or conversion.get(lookup_name)
        if lookup_name in original_conversion or existing_id:
            alias_note = f"（别名，归并到主名 {canonical_name}）" if canonical_name else ""
            logger.info(
                f"⏭️  已存在，跳过: {full_name}{alias_note} -> {mapped_name} -> "
                f"{existing_id or original_conversion.get(lookup_name)}"
            )
            continue
        if canonical_name:
            # 主名在角色库里却不在映射表中，属上游漏配：按主名补录，别名不另立档。
            logger.warning(
                f"⚠️ {full_name} 是角色库主名 {canonical_name} 的别名，"
                f"但主名不在映射表中，按主名补录"
            )
            new_npcs.setdefault(
                canonical_name, to_pascal_case_id(canonical_name) or speaker_id
            )
            continue
        new_npcs[full_name] = speaker_id

    # NPC 不改名：new-name 直接原名对应原名。
    # 剧情里临时出现的 NPC 多是单字名、泛称或描述性称呼，机器改名容易改坏语义
    # （"椿"、"家长"、"提问者"），且改名后还要处理撞名与链式替换。原名对应原名后
    # 第 16 步不会为这些 NPC 生成替换规则，正文中的称呼原样保留。
    _, api_key = resolve_llm_credentials()
    for full_name in new_npcs:
        new_name[full_name] = full_name
    if new_npcs:
        logger.info(f"🙂 新增 NPC 不改名，原名对应原名: {len(new_npcs)} 个")

    # 为 NPC 批量翻译出英文 + 日文（名字未改，仍需要三语显示名）
    translations = {"en": {}, "ja": {}}
    chinese_npcs = [new_name[fn] for fn in new_npcs if is_chinese(new_name[fn])]
    if chinese_npcs:
        if api_key:
            logger.info(f"🌐 调用 {MODEL_NAME} 翻译 {len(chinese_npcs)} 个 NPC 名称...")
            last_error = None
            for attempt in range(1, 4):
                try:
                    translations = translate_names_batch(chinese_npcs, api_key)
                    break
                except Exception as e:
                    last_error = e
                    logger.warning(f"⚠️ NPC 翻译第 {attempt}/3 次失败: {e}")
            else:
                logger.error(f"❌ NPC 翻译最终失败，将按统一规则降级: {last_error}")
        else:
            logger.warning("⚠️ 未配置大模型 API key，en-US 保留完整 speaker ID，ja-JP 退回原名")
    en_map = translations.get("en", {})
    ja_map = translations.get("ja", {})

    added = 0
    used_ids = set(conversion.values())
    used_original_ids = set(original_conversion.values())
    for full_name, speaker_id in new_npcs.items():
        original_base_id = to_pascal_case_id(full_name) or speaker_id
        original_id = original_base_id
        original_suffix = 2
        while original_id in used_original_ids:
            original_id = f"{original_base_id}_{original_suffix}"
            original_suffix += 1
        used_original_ids.add(original_id)
        original_conversion[full_name] = original_id

        # NPC 不改名，change_name 侧沿用原名 ID，保证第 16 步不会生成替换规则。
        anonymized_name = new_name[full_name]
        anonymized_id = original_id
        used_ids.add(anonymized_id)
        conversion[anonymized_name] = anonymized_id
        added += 1
        logger.info(f"➕ 新增 NPC 映射（不改名）: {full_name} -> {anonymized_id}")

        # 三语反查表（id -> 名），en-US / zh-CN / ja-JP 都补齐
        if is_chinese(anonymized_name):
            zh_cn.setdefault(anonymized_id, anonymized_name)
            # 与 ranked_name_mappings 统一：英文失败时保留完整 PascalCase ID，不使用首字母缩写。
            en_us.setdefault(anonymized_id, en_map.get(anonymized_name) or anonymized_id)
            ja_jp.setdefault(anonymized_id, ja_map.get(anonymized_name) or anonymized_name)
        else:
            en_us.setdefault(anonymized_id, anonymized_name)
            zh_cn.setdefault(anonymized_id, anonymized_name)
            ja_jp.setdefault(anonymized_id, anonymized_name)
        logger.info(
            f"   ↳ zh-CN: {zh_cn[anonymized_id]} | en-US: {en_us[anonymized_id]} | ja-JP: {ja_jp[anonymized_id]}"
        )

    # 1) 先写回数据库（upsert）
    save_name_mappings_to_db(task_id, mappings)

    # 2) 再覆盖本地 .../{task_id}/name_mappings.json
    local_path = os.path.join(OUTPUT_DIR, str(task_id), "name_mappings.json")
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    with open(local_path, "w", encoding="utf-8") as f:
        json.dump(mappings, f, ensure_ascii=False, indent=2)

    # 本步骤只补全 name_mappings，不改动 plots。换名统一由第 16 步
    # name_anonymizer 对全部产物一次性执行，避免第 7~15 步读到
    # 已换名的 plots 而 characters 表仍是原名，造成两边对不上。

    logger.info("-" * 30)
    logger.info(f"✅ [NameMap] 更新完成！新增 {added} 个角色映射。")
    logger.info(f"📂 本地存储路径: {os.path.abspath(local_path)}")
    return task_id


if __name__ == "__main__":
    TARGET_TASK_ID = ""
    update_name_mappings(TARGET_TASK_ID)
