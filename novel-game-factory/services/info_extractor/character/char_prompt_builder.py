"""
人物提示词生成器 (Story Tree Prompt Builder)
从数据库 characters 表读取人物数据，构造文生图提示词，
并将结果存入 character_prompts 表。

数据流：characters 表 -> 提示词构造 -> character_prompts 表

用法（在 novel-game-factory 目录下执行）：

# 自动使用最新任务
python -m novel-game-factory.services.info_extractor.character.char_prompt_builder

# 指定 task_id
python -m novel-game-factory.services.info_extractor.character.char_prompt_builder --task_id <task_id>

# 强制重建（即使 character_prompts 已存在也重新构造）
python -m novel-game-factory.services.info_extractor.character.char_prompt_builder --force --task_id

说明：不加 --force 时，若 character_prompts 已有该任务数据则跳过并返回已有结果。
本脚本只重建 character_prompts 表，不会接着跑后续 Step；要打通整条流水线请用 main.py。

"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, text

# 将项目根目录添加到 sys.path
import sys
from pathlib import Path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from common.config import PLATFORM_DATABASE_URL
from common.visual_style import (
    ASSET_CHARACTER,
    STYLE_MODE_LLM,
    get_asset_style,
    load_visual_style_config,
)
from common.character_personality_contract import (
    build_character_prompt_record,
    normalize_personality,
)
from .extract_raw import chinese_to_camel_case

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 提示词关键词替换表
KEYWORD_REPLACEMENTS = {
    "蝎子辫": "scorpion braid (no scorpions)",
    "蝎尾辫": "scorpion tail braid (no scorpions)",
    "沾有血迹": "略有战损痕迹",
    "沾有血污": "略有战损痕迹",
    "沾满黑血的": "战损的",
    "沾满血迹的": "战损的",
    # Blood-related vocabulary cleanup (clean from database descriptions)
    "血迹": "",
    "血污": "",
    "blood": "",
    "bloodstain": "",
    "blood stain": "",
    # Mud and stain-related vocabulary cleanup (clean from database descriptions)
    "泥污": "",
    "污垢": "",
    "mud": "",
    "dirt": "",
    "stain": "",
    "mud stain": "",
    "dirt stain": "",
}

NEGATIVE_PROMPT = (
    "multiple characters, extra limbs, deformed, ugly, low quality, blurry, watermark, signature, text, "
    "nameplate, name tag, name badge, name plate, name label, character name text, "
    "chest badge, chest tag, ID badge, ID card, name card on chest, badge on chest, "
    "nsfw, violence, gore, bad anatomy, extra fingers, missing limbs, disfigured, "
    "out of frame, cropped, "
    "half body, bust shot, close-up, headshot, portrait crop, "
    "knees up, waist up, thighs up, cut off legs, missing feet, feet out of frame"
)

# 人形-兽化等级对应的外观约束（英文），取自《人物人形与兽化程度分类规范》匹配提示词英语版本列。
# 用于按 human_animal_form 注入形态提示，避免鹦鹉/老鼠/龙虾等被错误画成人类。
HUMAN_ANIMAL_FORM_PROMPTS = {
    "pure_human": "Human.",
    "humanoid": "The character has a humanoid form and is human-like.",
    "humanoid_local_animal_feature": (
        "The character has a humanoid form and is human-like, with a human face; "
        "the overall appearance is human."
    ),
    "human_face_animal_body": (
        "The character has a human face; the body may contain obvious non-human structural features."
    ),
    "animal_form_humanoid_activity": (
        "The character is primarily non-human and animal-like in appearance; it has an animal face "
        "but not a complete animal head, and can perform humanoid activities. "
        "Do not generate an ordinary animal."
    ),
    "animal_head_humanoid_body": (
        "The character has a complete animal head, but the body still has a humanoid structure. "
        "Do not generate a human face or an ordinary animal body."
    ),
    "pure_animal_form": (
        "Not human and does not have a humanoid form. It has no anthropomorphic facial features "
        "and no anthropomorphic body."
    ),
    "other": (
        "Other non-standard form. Do not force the character into a human, humanoid, or any specific "
        "animal category; preserve the character's original unusual body structure."
    ),
}

# 主体明显不是人类的等级：不能再硬写 "only one human"，也不宜以 "a human" 作主体名词。
NON_HUMAN_FORMS = {
    "animal_form_humanoid_activity",
    "animal_head_humanoid_body",
    "pure_animal_form",
    "other",
}
# 原始 NEGATIVE_PROMPT（不约束半身/裁切）：
# NEGATIVE_PROMPT = (
#     "photorealistic, 3D render, real person, photograph, multiple characters, "
#     "extra limbs, deformed, ugly, low quality, blurry, watermark, signature, text, "
#     "nsfw, violence, gore, bad anatomy, extra fingers, missing limbs, disfigured, "
#     "out of frame, cropped"
# )


# ==================== 数据类 ====================

@dataclass
class CharacterInfo:
    name: str
    speaker_id: str
    gender: str = "unknown"
    age: int = 18
    apparent_age: int = 0  # 表面/外观年龄；>0 时优先用于生图年龄呈现
    age_category: str = ""
    height: str = ""
    race: str = "human"
    human_animal_form: str = ""  # 人形-兽化等级；空表示按人类保守处理
    occupation: str = ""
    combination: str = ""  # active combination string, e.g. "TangSan-1-2-3"
    appearance: Dict[str, Any] = field(default_factory=dict)
    clothing: Dict[str, Any] = field(default_factory=dict)
    expression: Dict[str, Any] = field(default_factory=dict)
    personality: Dict[str, Any] = field(default_factory=lambda: {
        "personality_id": None,
        "main_traits": [],
        "secondary_traits": [],
        "virtues": [],
        "flaws": [],
    })


# ==================== 工具函数 ====================

def _to_camel_case(name: str) -> str:
    return chinese_to_camel_case(name)


def _parse_age(age_val: Any) -> int:
    age_mapping = {
        "infant": 3, "child": 9, "adolescent": 15,
        "young adult": 23, "middle-aged": 40, "elderly": 60
    }
    if isinstance(age_val, int):
        return age_val
    age_str = str(age_val).strip()
    if age_str.isdigit():
        return int(age_str)
    if '-' in age_str:
        try:
            return int(age_str.split('-')[0])
        except ValueError:
            pass
    for k, v in age_mapping.items():
        if k in age_str:
            return v
    return 18


def _infer_gender(all_body_variants: List[Dict]) -> str:
    for v in all_body_variants:
        g = v.get('gender', '')
        if g in ('male', '男'):
            return 'male'
        if g in ('female', '女'):
            return 'female'
    return 'male'


def apply_keyword_replacements(prompt: str) -> str:
    for old, new in KEYWORD_REPLACEMENTS.items():
        prompt = prompt.replace(old, new)
    prompt = re.sub(r'，\s*，+', '，', prompt)
    prompt = re.sub(r'，\s*\.', '.', prompt)
    prompt = re.sub(r'\s+\.', '.', prompt)
    prompt = re.sub(r'沾有(?=[，。,.]|$)', '', prompt)
    prompt = re.sub(r'\s{2,}', ' ', prompt)
    return prompt.strip()


# ==================== 数据转换 ====================

def _convert_to_character_info(
    name: str,
    personality: Dict,
    body: Dict,
    clothing: Dict,
    all_body_variants: List[Dict]
) -> Optional[CharacterInfo]:
    try:
        gender = body.get('gender', 'unknown')
        if gender == '女':
            gender = 'female'
        elif gender == '男':
            gender = 'male'
        if gender in ('other', 'unknown'):
            gender = _infer_gender(all_body_variants)

        age_category = body.get('age_category', 'young adult')
        age_raw = body.get('age')
        if not age_raw or str(age_raw).strip() == '':
            age_map = {"infant": 3, "child": 9, "adolescent": 15,
                       "young adult": 23, "middle-aged": 40, "elderly": 60}
            age = age_map.get(age_category, 18)
        else:
            age = _parse_age(age_raw)

        # 表面年龄：实际百岁但外表十八时画十八。留空则视同实际年龄。
        apparent_raw = body.get('apparent_age')
        if apparent_raw is None or str(apparent_raw).strip() == '':
            apparent_age = 0
        else:
            apparent_age = _parse_age(apparent_raw)

        raw_app = body.get('appearance', {})
        head = raw_app.get('head', {})
        body_info = raw_app.get('body', {})
        appearance = {
            "hair_color": head.get('hair_color', ''),
            "eye_color": head.get('eye_color', ''),
            "skin_color": head.get('skin_color', ''),
            "skin_texture": head.get('skin_color', ''),  # 与原版保持一致
            "hair_style": head.get('hair_style', ''),
            "facial_features": [head['facial_description']] if head.get('facial_description') else [],
            "body_type": body_info.get('body_type', ''),
            "body_details": body_info.get('body_description', ''),
        }

        expression = {"eyes": body.get('expression', {}).get('eyes', '')}

        clothing_info = {
            "main_outfit": clothing.get('outfit_discription', ''),
            "texture": clothing.get('outfit_discription', ''),  # 备用字段，与原版一致
            "accessories": clothing.get('accessories', []),
            "weapons": clothing.get('weapons', []),
            "armor": clothing.get('armor', []),
            "occupation": clothing.get('occupation', ''),
        }

        personality_info = normalize_personality(personality)

        return CharacterInfo(
            name=name,
            speaker_id=_to_camel_case(name),
            gender=gender,
            age=age,
            apparent_age=apparent_age,
            age_category=age_category,
            height=str(body.get('height', '')).strip(),
            race=body.get('race', 'human'),
            human_animal_form=str(body.get('human_animal_form', '')).strip(),
            occupation=clothing_info['occupation'],
            appearance=appearance,
            clothing=clothing_info,
            expression=expression,
            personality=personality_info,
        )
    except Exception as e:
        logger.error(f"转换角色数据失败 {name}: {e}")
        return None


def parse_characters_from_db_json(json_data: Dict) -> List[CharacterInfo]:
    characters = []
    for char_key, char_data in json_data.items():
        char_name = char_data.get('name', char_key)
        p_vars = char_data.get('personality_variants', [])
        b_vars = char_data.get('whole_body_variants', [])
        c_vars = char_data.get('clothing_variants', [])

        if not (b_vars and c_vars):
            logger.warning(f"跳过角色 {char_name}: 缺少变体数据")
            continue

        # 选择最接近 18 岁的 young adult/adolescent 变体
        selected_body = b_vars[0]
        best_diff = float('inf')
        for variant in b_vars:
            age_cat = variant.get('age_category', '')
            age_val = _parse_age(variant.get('age', 0))
            diff = abs(age_val - 18)
            if age_cat in ("adolescent", "young adult") and diff < best_diff:
                best_diff = diff
                selected_body = variant
            elif diff < best_diff:
                best_diff = diff
                selected_body = variant

        char_obj = _convert_to_character_info(
            char_name,
            p_vars[0] if p_vars else {},
            selected_body,
            c_vars[0],
            b_vars
        )
        if char_obj:
            characters.append(char_obj)
    return characters


def _parse_combination(combination: str) -> Tuple[str, int, int, int]:
    """解析 combination 字符串，返回 (name, personality_id, whole_body_id, clothing_id)。"""
    parts = combination.rsplit("-", 3)
    if len(parts) != 4:
        raise ValueError(f"无效的 combination 格式: {combination}")
    name, p_id, b_id, c_id = parts
    return name, int(p_id), int(b_id), int(c_id)


def parse_characters_by_active_combinations(json_data: Dict) -> List[CharacterInfo]:
    """
    为每个 is_active=True 的 combination 生成一个 CharacterInfo。
    若角色无激活组合，回退到原始逻辑（最接近18岁的 body + 第一个 clothing）。
    """
    characters = []
    for char_key, char_data in json_data.items():
        if isinstance(char_data, list):
            char_data = char_data[0] if char_data else {}
        if not isinstance(char_data, dict):
            continue

        char_name = char_data.get('name', char_key)
        p_vars = char_data.get('personality_variants', [])
        b_vars = char_data.get('whole_body_variants', [])
        c_vars = char_data.get('clothing_variants', [])
        combinations = char_data.get('valid_status_combinations', [])

        if not (b_vars and c_vars):
            logger.warning(f"跳过角色 {char_name}: 缺少变体数据")
            continue

        body_map = {v["whole_body_id"]: v for v in b_vars if isinstance(v, dict) and "whole_body_id" in v}
        clothing_map = {v["clothing_id"]: v for v in c_vars if isinstance(v, dict) and "clothing_id" in v}
        active_combos = [c for c in combinations if isinstance(c, dict) and c.get("is_active")]

        if not active_combos:
            logger.debug(f"跳过角色 {char_name}: 无激活组合（未在剧情中说话）")
            continue

        personality_map = {
            v["personality_id"]: v
            for v in p_vars
            if isinstance(v, dict) and "personality_id" in v
        }

        for combo in active_combos:
            combination_str = combo.get("combination", "")
            if not combination_str:
                continue
            try:
                _, p_id, b_id, c_id = _parse_combination(combination_str)
            except ValueError:
                logger.warning(f"跳过无效 combination: {combination_str}")
                continue

            body = body_map.get(b_id, b_vars[0])
            clothing = clothing_map.get(c_id, c_vars[0])
            # 组合 ID 第一位是 personality_id，须据此取对应性格，
            # 而非固定用 p_vars[0]（否则第一位非 1 的组合性格全错）。
            personality = personality_map.get(p_id)
            if personality is None:
                logger.warning(
                    f"组合 {combination_str} 引用了不存在的 personality_id={p_id}；"
                    "不回退到其它性格，按空性格 Contract 处理"
                )
                personality = {"personality_id": p_id}
            char_obj = _convert_to_character_info(
                char_name, personality, body, clothing, b_vars
            )
            if char_obj:
                char_obj.combination = combination_str
                characters.append(char_obj)

    return characters


def _build_fallback_char(
    char_name: str,
    p_vars: List[Dict],
    b_vars: List[Dict],
    c_vars: List[Dict],
) -> Optional[CharacterInfo]:
    """原始逻辑：选最接近18岁的 body + 第一个 clothing。"""
    selected_body = b_vars[0]
    best_diff = float('inf')
    for variant in b_vars:
        age_cat = variant.get('age_category', '')
        age_val = _parse_age(variant.get('age', 0))
        diff = abs(age_val - 18)
        if age_cat in ("adolescent", "young adult") and diff < best_diff:
            best_diff = diff
            selected_body = variant
        elif diff < best_diff:
            best_diff = diff
            selected_body = variant
    return _convert_to_character_info(
        char_name, p_vars[0] if p_vars else {}, selected_body, c_vars[0], b_vars
    )


# ==================== 提示词构造 ====================


def generate_prompt(character: CharacterInfo, global_style: str) -> str:
    parts = {}

    # 1. 背景 + 人物核心描述
    gender_display = {"male": "male", "female": "female"}.get(character.gender, "character")
    # 年龄呈现：三个字段哪个有就拼哪个，都描述“外表”优先。
    # - apparent_age（外表年龄数字）→ "appears to be about X years old"
    # - age_category（外表年龄段）    → "appears to be a(n) XXX"
    # 实际 age 只在前两者都为空时才兜底写出，避免“百岁看似18”时又写 "100 years old"
    # 把脸画老（真实年龄的古老感应由 race/human_animal_form 表达）。
    age_phrases = []
    if character.apparent_age > 0:
        age_phrases.append(f"appears to be about {character.apparent_age} years old")
    if character.age_category:
        article = "an" if character.age_category[:1].lower() in "aeiou" else "a"
        age_phrases.append(f"appears to be {article} {character.age_category}")
    if not age_phrases and character.age > 0:
        age_phrases.append(f"actually {character.age} years old")
    age_display = ", ".join(age_phrases)
    form = character.human_animal_form
    is_non_human = form in NON_HUMAN_FORMS
    # 非人形态不用 "a {race}"（race 常为 human），主体名词改中性，形态交由约束句表达。
    subject_noun = "creature" if is_non_human else character.race
    # 逐段过滤空值再拼，避免 age_display / occupation 缺失时留下 ", ," 空段。
    subject_segments = [seg for seg in (gender_display, age_display, character.occupation) if seg]
    role_desc = (
        f"The background is completely pure white, pure white background, "
        f"and the main subject of the image is a {subject_noun}, "
        + ", ".join(subject_segments)
    )
    if character.personality.get('main_traits'):
        traits = character.personality['main_traits'][:2]
        role_desc += f", with temperament of {', '.join(traits)}"
    # 形态约束句：按人形-兽化等级注入，指导模型画对形态。
    form_prompt = HUMAN_ANIMAL_FORM_PROMPTS.get(form, "")
    if form_prompt:
        role_desc += f", state calm. {form_prompt}"
    else:
        role_desc += ", state calm."
    # 仅在人形（含人脸主导）等级才强制单人；非人形态强制单人会把动物画成人。
    if not is_non_human:
        role_desc += " Ensure there is only one character in the image."
    parts["background_and_character"] = role_desc

    # 2. 艺术风格
    normalized_style = global_style.strip().rstrip(".")
    if normalized_style:
        parts["global_style"] = normalized_style + "."

    # 3. 构图
    # 原始 framing（已被新版替换）：
    # parts["framing"] = "The composition shows the entire full body of the character, from head to toe, including feet"
    parts["framing"] = (
        "Full body shot, the entire character is visible from head to toe, "
        "including the feet, with empty space below the feet, "
        "do not crop the legs or feet."
    )

    # 4. 外观
    app = character.appearance
    if app:
        desc = []
        if app.get('hair_color'):
            desc.append(f"{app['hair_color']} hair")
        if app.get('eye_color'):
            eye = app['eye_color'].replace("紫气东来", "morning light").replace("神秘淡紫色光晕", "natural luster")
            desc.append(f"{eye} eyes")
        if app.get('facial_features'):
            desc.append(f"facial features of {', '.join(app['facial_features'])}")
        if app.get('body_type'):
            bt = app['body_type'].replace("爆发力", "vitality").replace("敏捷性", "agility")
            desc.append(f"{bt} body type")
        if app.get('skin_texture'):
            desc.append(f"{app['skin_texture']} skin")
        if desc:
            parts["appearance"] = "The character has " + ", ".join(desc) + "."

    # 5. 服装
    cloth = character.clothing
    if cloth:
        desc = []
        if cloth.get('main_outfit'):
            desc.append(f"wearing {cloth['main_outfit']}")
        elif cloth.get('texture'):
            texture_desc = cloth['texture']
            if "黑袍" in texture_desc or "制式" in texture_desc:
                desc.append(f"designed with {texture_desc} clothing")
            else:
                desc.append(f"wearing {texture_desc}")
        if cloth.get('accessories'):
            safe_acc = [a for a in cloth['accessories'] if "暗器" not in a and "武器" not in a and "战斗" not in a]
            if safe_acc:
                desc.append(f"with {', '.join(safe_acc)}")
        if desc:
            parts["clothing"] = "The clothing is clean and tidy, " + " ".join(desc) + "."

    # 6. 眼神（来自 expression）
    if character.expression.get('eyes'):
        eyes = character.expression['eyes'].replace("紫气东来", "morning light").replace("神秘淡紫色光晕", "natural luster")
        parts["eyes"] = f"The character has {eyes} eyes."

    # 7-10. 固定描述
    parts["visual_quality"] = "Consistent visual treatment, clear silhouette, coherent lighting and materials."
    parts["pose"] = (
        "The character is facing or half-facing the camera, standing, "
        "looking straight at the camera with the face oriented directly toward the front."
    )
    parts["focus"] = "Focus on showcasing the character design."
    parts["no_nameplate"] = (
        "Do not add any nameplate, name tag, name badge, name plate, name label, "
        "character name text, chest badge, chest tag, ID badge, or ID card "
        "on the character or the background."
    )
    parts["safety"] = (
        "Polished full-body character asset, safe content, suitable for all ages."
    )

    ordered = [
        parts.get("global_style"),
        parts.get("background_and_character"),
        parts.get("framing"),
        parts.get("appearance"),
        parts.get("clothing"),
        parts.get("eyes"),
        parts.get("visual_quality"),
        parts.get("pose"),
        parts.get("focus"),
        parts.get("no_nameplate"),
        parts.get("safety"),
    ]
    full_prompt = " ".join(p for p in ordered if p)
    return apply_keyword_replacements(full_prompt)


def _build_prompt_record(character: CharacterInfo, positive_prompt: str) -> Dict[str, Any]:
    """Build the JSONB record consumed by character and expression generation."""
    personality = normalize_personality(character.personality)
    if character.combination:
        try:
            _, expected_personality_id, _, _ = _parse_combination(character.combination)
            actual_personality_id = personality.get("personality_id")
            if actual_personality_id not in (None, expected_personality_id):
                logger.warning(
                    "组合性格 ID 与 personality 记录不一致，按 combination 为准: "
                    f"combination={character.combination} personality_id={actual_personality_id}"
                )
            personality["personality_id"] = expected_personality_id
        except ValueError:
            logger.warning(f"无法从 combination 校验 personality_id: {character.combination}")
    return build_character_prompt_record(
        name=character.name,
        speaker_id=character.speaker_id,
        positive_prompt=positive_prompt,
        negative_prompt=NEGATIVE_PROMPT,
        height=character.height,
        personality=personality,
        combination=character.combination,
    )


# ==================== 数据库操作 ====================

def fetch_characters_from_db(task_id: str = None) -> Tuple[Optional[str], Dict]:
    """从 characters 表读取人物数据。"""
    engine = create_engine(PLATFORM_DATABASE_URL)
    with engine.connect() as conn:
        if not task_id:
            row = conn.execute(text("SELECT task_id FROM characters ORDER BY id DESC LIMIT 1")).fetchone()
            if not row:
                raise RuntimeError("数据库中未找到角色数据")
            task_id = row[0]
            logger.info(f"自动选取最新任务: {task_id}")

        char_row = conn.execute(
            text("SELECT data FROM characters WHERE task_id = :tid"), {"tid": task_id}
        ).fetchone()
        if not char_row:
            raise RuntimeError(f"未找到 task_id={task_id} 的角色数据")
        char_data = char_row[0]

    return task_id, char_data


def collect_emotions_by_combination(engine, task_id: str) -> Dict[str, List[str]]:
    """
    从 plots 表统计每个 combination 出现过的 emotion 种类。
    返回 {combination: [emotion, ...]}，emotion 列表已去重并排序。
    """
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM plots WHERE task_id = :tid"), {"tid": task_id}
        ).fetchone()
    if not row:
        logger.warning(f"plots 表中未找到 task_id={task_id}")
        return {}

    data = row[0]
    if isinstance(data, str):
        import json as _json
        data = _json.loads(data)

    combo_emotions: Dict[str, set] = {}
    for node_beats in data.values():
        if not isinstance(node_beats, list):
            continue
        for beat in node_beats:
            combo = beat.get("speaker_combination", "")
            emotion = beat.get("emotion", "")
            if combo and emotion:
                combo_emotions.setdefault(combo, set()).add(emotion)

    return {k: sorted(v) for k, v in combo_emotions.items()}


def save_prompts_to_db(task_id: str, prompts: List[Dict]) -> None:
    """将提示词列表存入 character_prompts 表（upsert）"""
    engine = create_engine(PLATFORM_DATABASE_URL)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import json
    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO character_prompts (task_id, data, create_time, update_time)
                VALUES (:task_id, cast(:data as jsonb), :create_time, :update_time)
                ON CONFLICT (task_id) DO UPDATE
                    SET data = EXCLUDED.data,
                        update_time = EXCLUDED.update_time
            """),
            {
                "task_id": task_id,
                "data": json.dumps(prompts, ensure_ascii=False),
                "create_time": now,
                "update_time": now,
            }
        )
        conn.commit()
    logger.info(f"已保存 {len(prompts)} 个人物提示词 -> character_prompts (task_id={task_id})")


def _load_existing_prompts(task_id: str) -> List[Dict]:
    """读取 character_prompts 表中已有的提示词列表。"""
    engine = create_engine(PLATFORM_DATABASE_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM character_prompts WHERE task_id = :tid"),
            {"tid": task_id},
        ).fetchone()
    if not row or not row[0]:
        return []
    data = row[0]
    if isinstance(data, str):
        import json as _json
        data = _json.loads(data)
    return data


# ==================== 主入口 ====================

def _character_prompts_exists(task_id: str) -> bool:
    """检查 character_prompts 表中是否已有该任务的数据。"""
    engine = create_engine(PLATFORM_DATABASE_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM character_prompts WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id},
        ).fetchone()
    return row is not None


def _active_combinations(char_data: Dict) -> set:
    """characters 表里所有 is_active=True 的 combination 集合。"""
    result = set()
    if not isinstance(char_data, dict):
        return result
    for cd in char_data.values():
        if isinstance(cd, list):
            cd = cd[0] if cd else {}
        if not isinstance(cd, dict):
            continue
        for combo in cd.get("valid_status_combinations") or []:
            if isinstance(combo, dict) and combo.get("is_active"):
                combination = str(combo.get("combination") or "").strip()
                if combination:
                    result.add(combination)
    return result


def _plot_combinations(task_id: str) -> set:
    """plots 表里所有 beat 用到的 speaker_combination 集合。"""
    engine = create_engine(PLATFORM_DATABASE_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM plots WHERE task_id = :tid"), {"tid": task_id}
        ).fetchone()
    if not row or not row[0]:
        return set()
    data = row[0]
    if isinstance(data, str):
        import json as _json
        data = _json.loads(data)
    result = set()
    if not isinstance(data, dict):
        return result
    for beats in data.values():
        if not isinstance(beats, list):
            continue
        for beat in beats:
            if isinstance(beat, dict):
                combination = str(beat.get("speaker_combination") or "").strip()
                if combination:
                    result.add(combination)
    return result


def _prompt_combinations(prompts: List[Dict]) -> set:
    """character_prompts 里出现过的 combination 集合。"""
    result = set()
    for item in prompts or []:
        if isinstance(item, dict):
            combination = str(
                item.get("combination") or item.get("speaker_id") or ""
            ).strip()
            if combination:
                result.add(combination)
    return result


def check_prompt_coverage(
    prompts: List[Dict], char_data: Dict, task_id: str
) -> Dict[str, List[str]]:
    """校验 character_prompts 是否覆盖当前 active + 剧情组合。

    与下游 char_designer 预检 (visual_input_preflight) 同一判据，只做集合比对：
    - missing_active：characters 中 is_active=True 却在提示词表缺失的组合（须重建）
    - missing_plot：plots.speaker_combination 在提示词表缺失的组合（须重建）
    - extra：提示词表里有、但已不在 active 的旧组合（错位残留）
    """
    prompt_combos = _prompt_combinations(prompts)
    active_combos = _active_combinations(char_data)
    plot_combos = _plot_combinations(task_id)
    return {
        "missing_active": sorted(active_combos - prompt_combos),
        "missing_plot": sorted(plot_combos - prompt_combos),
        "extra": sorted(prompt_combos - active_combos),
    }


# 覆盖不全时重建并回填的最大次数（含首次），超过仍缺则判上游数据有缺口。
_MAX_REBUILD_ATTEMPTS = 3


def _build_prompt_entries(
    char_data: Dict, global_style: str, only: Optional[set] = None
) -> List[Dict]:
    """按 active 组合构造人物提示词记录列表（纯模板拼装，不调 LLM，不存库）。

    only 非空时只构造 combination 落在该集合内的记录（增量补缺用）；
    为 None 时构造全部 active 组合。
    """
    characters = parse_characters_by_active_combinations(char_data)
    prompts = []
    for char in characters:
        if only is not None and char.combination not in only:
            continue
        height = str(char.height).strip()
        # 身高缺失或非法（如"系统""旁白"等非实体角色）不再终止流程，
        # 统一规范为 "0"，交由下游 expand_canvas 跳过顶部像素追加。
        if re.fullmatch(r"\d+", height) and int(height) > 0:
            char.height = str(int(height))
        else:
            logger.warning(
                f"角色 {char.name} combination={char.combination or '(fallback)'} "
                f"身高缺失或无效，按 0 处理（不追加顶部像素）: height={char.height!r}"
            )
            char.height = "0"
        positive = generate_prompt(char, global_style)
        entry = _build_prompt_record(char, positive)
        prompts.append(entry)
        logger.info(f"  [{char.speaker_id}] combination={char.combination or '(fallback)'} 提示词已生成")
    return prompts


def _fill_emotions(task_id: str, prompts: List[Dict]) -> None:
    """为每条提示词补 emotions（就地修改）。"""
    engine = create_engine(PLATFORM_DATABASE_URL)
    combo_emotions = collect_emotions_by_combination(engine, task_id)
    for entry in prompts:
        combo = entry.get("combination", "")
        entry["emotions"] = combo_emotions.get(combo, [])
        if combo:
            logger.info(f"  [{entry['speaker_id']}] {combo} emotions={entry['emotions']}")


def _construct_and_save_prompts(
    task_id: str, char_data: Dict, global_style: str
) -> List[Dict]:
    """全量构造人物提示词、补 emotions 并存库，返回提示词列表。"""
    prompts = _build_prompt_entries(char_data, global_style)
    _fill_emotions(task_id, prompts)
    save_prompts_to_db(task_id, prompts)
    return prompts


def build_and_save_prompts(task_id: str = None, force: bool = False) -> List[Dict]:
    """
    主流程：从数据库读取人物 -> 构造提示词 -> 存入 character_prompts 表

    Args:
        task_id: 指定任务ID，None 则自动选取最新任务
        force: 为 True 时强制重新构造；为 False 且已存在数据时跳过并返回已有结果

    Returns:
        生成的提示词列表，格式与 prompts.json 一致
    """
    task_id, char_data = fetch_characters_from_db(task_id)
    content_only_llm_route = (
        load_visual_style_config(task_id).get("mode") == STYLE_MODE_LLM
    )
    if content_only_llm_route:
        global_style = ""
        logger.info("LLM 自动 Anchor 路线：character_prompts 只构造人物内容提示词")
    else:
        global_style = get_asset_style(task_id, ASSET_CHARACTER)
        logger.info(f"character_global_style: {global_style}")

    # 复用门禁改为“覆盖检查”而非“行是否存在”：
    # 状态组合更新（characters/plots 变成新 combination）后若仍复用旧提示词，
    # 会导致 char_designer 预检报 active/plot 组合缺提示词。因此已存在时先校验
    # 覆盖度，缺 active/plot 组合则进入重建循环，避免把旧版本快照带到下游。
    if not force and _character_prompts_exists(task_id):
        existing = _load_existing_prompts(task_id)
        coverage = check_prompt_coverage(existing, char_data, task_id)
        if coverage["missing_active"] or coverage["missing_plot"]:
            logger.warning(
                f"♻️ character_prompts 覆盖不全，将重建 (task_id={task_id}): "
                f"missing_active={coverage['missing_active']} "
                f"missing_plot={coverage['missing_plot']}"
            )
        else:
            logger.info(f"✅ character_prompts 已覆盖当前 active+剧情组合 (task_id={task_id})，跳过构造")
            if coverage["extra"]:
                logger.info(f"  （提示词含 {len(coverage['extra'])} 个非 active 旧组合，保留不影响下游: {coverage['extra']}）")
            return existing

    # 增量补缺循环，最多 _MAX_REBUILD_ATTEMPTS 轮：
    # 保留已有正确条目（含同事手工修过的），每轮只构造 missing 的组合并合并，
    # 而非整表重建；每轮结束存库。覆盖全即放行。次数用尽仍缺不阻断本步，
    # 只告警放行，缺口留给 char_designer 预检暴露。
    # 首轮初始化：无已有数据(force 或 表空)则先全量构造一次作为基线。
    if force or not _character_prompts_exists(task_id):
        prompts = _construct_and_save_prompts(task_id, char_data, global_style)
    else:
        prompts = existing  # 上面覆盖不全分支加载的已有条目

    coverage = None
    for attempt in range(1, _MAX_REBUILD_ATTEMPTS + 1):
        _, char_data = fetch_characters_from_db(task_id)
        coverage = check_prompt_coverage(prompts, char_data, task_id)
        missing = set(coverage["missing_active"]) | set(coverage["missing_plot"])
        if not missing:
            if attempt > 1:
                logger.info(f"✅ 第 {attempt}/{_MAX_REBUILD_ATTEMPTS} 轮补缺后覆盖完整，放行")
            if coverage["extra"]:
                logger.info(f"  （提示词含 {len(coverage['extra'])} 个非 active 旧组合: {coverage['extra']}）")
            return prompts

        logger.warning(
            f"♻️ 第 {attempt}/{_MAX_REBUILD_ATTEMPTS} 轮：补缺 {len(missing)} 个组合 "
            f"(task_id={task_id}): {sorted(missing)}"
        )
        # 只构造缺失组合，合并进已有 prompts（按 combination 去重覆盖），不动其它条目。
        new_entries = _build_prompt_entries(char_data, global_style, only=missing)
        _fill_emotions(task_id, new_entries)
        merged = {e.get("combination", ""): e for e in prompts}
        for e in new_entries:
            merged[e.get("combination", "")] = e
        prompts = list(merged.values())
        save_prompts_to_db(task_id, prompts)

    # 用尽轮数仍缺：多为上游数据缺口（如 active 组合对应角色缺变体数据被跳过）。
    # 不抛错阻断本步，只告警放行，把缺口留给 char_designer 预检暴露。
    logger.warning(
        f"⚠️ character_prompts 补缺 {_MAX_REBUILD_ATTEMPTS} 轮后仍未覆盖全部组合 (task_id={task_id})，"
        f"放行交下游预检: missing_active={coverage['missing_active']} "
        f"missing_plot={coverage['missing_plot']}；建议检查上游 characters/plots 数据"
    )
    return prompts


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="构造人物文生图提示词并存入数据库")
    parser.add_argument("--task_id", type=str, default=None, help="指定任务ID，默认使用最新任务")
    parser.add_argument("--force", action="store_true", help="强制重新构造，即使 character_prompts 已存在")
    args = parser.parse_args()

    result = build_and_save_prompts(args.task_id, force=args.force)
    print(f"\n完成，共生成 {len(result)} 个人物提示词")
