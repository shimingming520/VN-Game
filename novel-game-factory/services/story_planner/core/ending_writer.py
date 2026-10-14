"""ending_writer.py - 高质量结局生成：分场大纲 → 循环扩写"""
import json
import re
from services.story_planner.core.llm_client import LLMClient, get_lang_instruction


def _narrative_pov_prompt_block(contract: dict | None) -> str:
    """Render the shared narrative POV contract for ending prose prompts.

    The contract applies only to generated prose.  Player-facing choice labels,
    quoted dialogue, letters and diary excerpts may legitimately use a
    first-person pronoun even when the surrounding narration is third-person.
    """
    if not isinstance(contract, dict):
        return ""
    perspective = str(
        contract.get("perspective")
        or contract.get("mode")
        or contract.get("pov")
        or "unknown"
    ).strip()
    protagonist = str(
        contract.get("protagonist_name")
        or contract.get("pov_character")
        or "主角"
    ).strip()
    rule_text = str(contract.get("rule_text") or "").strip()
    if not rule_text:
        if perspective == "first_person":
            rule_text = (
                f"保持主角{protagonist}的第一人称叙事；正文中的‘我’只能指向{protagonist}，"
                "不得把叙述者改为其他角色。"
            )
        elif perspective == "third_person_limited":
            rule_text = (
                f"保持围绕{protagonist}的第三人称限知叙事；正文叙述使用姓名、他/她等第三人称，"
                "不得把叙述者改成‘我’。"
            )
        elif perspective == "third_person_omniscient":
            rule_text = (
                f"保持围绕{protagonist}的第三人称全知叙事；不得无铺垫切换成第一人称旁白。"
            )
        elif perspective == "mixed":
            rule_text = "原著允许有限多视角；每个场景必须明确跟随一个视角，不得无标记切换人称。"
        else:
            rule_text = "原著视角未确定；优先模仿相邻原文，禁止在同一场景无理由切换人称。"
    switch_rule = (
        "如原著允许章节级切换，只能在明确的章节/场景边界切换。"
        if bool(contract.get("allow_switch"))
        else "当前契约禁止章节或场景内切换叙事视角。"
    )
    return (
        "【叙事视角契约（仅约束正文，不约束 choice_text UI 文案）】\n"
        f"perspective={perspective}; protagonist={protagonist}\n"
        f"{rule_text}\n{switch_rule}\n"
        "对白、书信、日记中的人称按原文保留；不要把按钮文案的‘我’写进第三人称正文。\n\n"
    )


def _context_narrative_pov_contract(novel_context, explicit: dict | None = None) -> dict:
    """Resolve a POV contract from an explicit argument or NovelContext."""
    if isinstance(explicit, dict):
        return explicit
    contract = getattr(novel_context, "narrative_pov_contract", None)
    if isinstance(contract, dict) and contract:
        return contract
    worldview = getattr(novel_context, "worldview", {}) if novel_context else {}
    contract = worldview.get("narrative_pov_contract") if isinstance(worldview, dict) else None
    return contract if isinstance(contract, dict) else {}


# 元文本泄漏关键词：正文里绝不该出现的游戏机制/创作过程词
_META_LEAK_WORDS = [
    "affection", "维度", "好感度", "阈值", "专属画面", "象限",
    "低维度", "高维度", "数值兑现", "兑现阶段", "触发变量", "变量",
]


def _find_protagonist_name(worldview: dict | None) -> str:
    """从世界观中找出主角名；缺少结构化角色卡时使用中性称呼。"""
    if not isinstance(worldview, dict):
        return "主角"
    for character in worldview.get("characters", []):
        if not isinstance(character, dict):
            continue
        role = str(character.get("role", ""))
        name = str(character.get("name", "")).strip()
        if name and role.startswith("主"):
            return name
    return "主角"


def build_variable_legacy_requirement(
    trigger_variable_info: str,
    worldview: dict | None,
    ui_mode: str | None = None,
) -> str:
    """把抽象数值轴转换成能够推动结局行动的、可验证的物质遗产。"""
    trigger = str(trigger_variable_info or "").lower()
    protagonist = _find_protagonist_name(worldview)
    ancient = str(ui_mode or "").strip().lower() == "ancient"
    if "affection_" in trigger:
        return (
            f"【恋爱路线关系证据】请让 {protagonist} 通过主动坦白、守约、公开确认关系、共同计划未来"
            "或在关键时刻明确选择对方等可观察行动，证明此前选择确实积累成了这条关系；"
            "结局转折必须由主角与目标角色当下的互动推动，不得改写成遗物、遗言或旁人代为完成。"
        )
    if "courage" in trigger:
        if ancient:
            return (
                f"【勇气轴古风可验证凭证】{protagonist}必须主动留下血书、亲笔求援信、告发状、"
                "按印供词或约定信物中的至少一种；县衙、官府、家族或关键行动者必须通过笔迹、"
                "印信、证人或旧约核验，并据此采取营救、重审、揭发或昭雪行动。"
            )
        return (
            f"【勇气轴可验证遗产】{protagonist}必须在生前主动留下署名信、录音或治疗请求中的至少一种；"
            "结局主要行动者必须因为发现并核验这份遗产而采取最终行动，不能只依赖日记、旧书或回信。"
        )
    if "self_worth" in trigger:
        if ancient:
            return (
                f"【自我价值轴古风可验证凭证】{protagonist}必须通过契书、字据、账本、族谱、户籍、"
                "官府文书或证人供词主动确认自己的姓名、身份、清白、财产权或经营权；凭证必须被掌柜、"
                "族老或官府核验，并实际推动其自立、恢复名誉、掌握产业或拒绝再次被他人支配。"
            )
        return (
            f"【自我价值轴可验证遗产】{protagonist}必须主动澄清身世与流言，并留下公开遗言、校方证词"
            "或其他可核验记录；结局主要行动者必须由这份遗产推动，不能复用勇气路线的证据链。"
        )
    if "playfulness" in trigger:
        return (
            f"【玩心轴可观察兑现】请让 {protagonist} 主动开玩笑、调侃、俏皮回应、善意恶作剧"
            "或用幽默重新解释眼前困局；该行为必须可观察，并实际缓和冲突、改变某人的决定、"
            "修复关系或打开新的行动路径。不得只写主角心里轻松，也不得强行留下信件、录音等实体凭证。"
        )
    return (
        f"【可验证遗产要求】请为 {protagonist} 设计一份只属于当前触发轴的可验证物质遗产，"
        "并让结局主要行动者在核验该遗产后才采取最终行动；不得与其他平行结局复用同一证据链。"
    )


def review_variable_legacy_evidence(
    *,
    ending_text: str,
    variable_key: str,
    ui_mode: str,
    provider: str,
    variable_definition: dict | None = None,
    worldview: dict | None = None,
) -> dict:
    """Use one bounded semantic review for the current ending evidence chain."""
    text = str(ending_text or "")
    excerpt = text if len(text) <= 16000 else text[:6000] + "\n\n[中段省略]\n\n" + text[-10000:]
    variable = variable_definition if isinstance(variable_definition, dict) else {}
    client = LLMClient(provider=provider)
    try:
        result = client.chat_completion_json(
            system_prompt=(
                "你是视觉小说结局证据链审核器。你只判断叙事功能，不要求固定现代关键词。"
                "不同题材中的血书、契书、族谱、县衙卷宗、录音、档案等可以承担等价功能。"
                "四个核心条件必须同时成立：存在具体证据；证据源自主角主动行动或此前玩家路径；"
                "能被其他人物以明确方式核验；核验结果实际推动结局行动。"
                "不要因为文体不同而误判，也不要把普通道具或只有心理描写的内容判为通过。"
            ),
            user_prompt=(
                f"UI_mode={ui_mode}\n"
                f"变量={variable_key}\n"
                f"变量定义={variable}\n"
                f"主角={_find_protagonist_name(worldview)}\n\n"
                "请审核以下结局，只输出 JSON：\n"
                "{\n"
                '  "passed": true或false,\n'
                '  "confidence": 0到1,\n'
                '  "reason": "简短理由",\n'
                '  "core_chain": {\n'
                '    "evidence_exists": true或false,\n'
                '    "protagonist_origin": true或false,\n'
                '    "verifiable": true或false,\n'
                '    "causal_payoff": true或false\n'
                "  },\n"
                '  "evidence": {\n'
                f'    "variable": "{variable_key}",\n'
                '    "evidence_type": "抽象证据类型",\n'
                '    "artifact_name": "正文中的凭证或行为",\n'
                '    "creator": "主动创建者",\n'
                '    "action": "主动行为",\n'
                '    "verifier": "核验者",\n'
                '    "verification_method": "核验方式",\n'
                '    "story_effect": "推动的结局行动",\n'
                '    "evidence_quote": "正文短引文"\n'
                "  }\n"
                "}\n\n"
                f"结局正文：\n{excerpt}"
            ),
            temperature=0.0,
            max_tokens=1200,
            timeout=180,
        )
    except Exception as exc:
        return {
            "passed": False,
            "confidence": 0.0,
            "reason": f"语义复核调用失败：{exc}",
            "core_chain": {},
            "evidence": {},
        }
    return result if isinstance(result, dict) else {
        "passed": False,
        "confidence": 0.0,
        "reason": "语义复核返回格式无效",
        "core_chain": {},
        "evidence": {},
    }


def review_ending_variable_causality(
    *,
    ending_text: str,
    variable_definition: dict | None,
    provider: str,
    worldview: dict | None = None,
    context: dict | None = None,
) -> dict:
    """Semantically review a variable-driven ending's causal payoff.

    No fixed vocabulary is used for the decision.  The model evaluates whether
    the ending turns the selected variable into an observable, route-specific
    consequence, and must return a short quote so the caller can ground the
    decision in the generated artifact.
    """
    text = str(ending_text or "")
    excerpt = text if len(text) <= 18000 else text[:7000] + "\n\n[中段省略]\n\n" + text[-11000:]
    variable = variable_definition if isinstance(variable_definition, dict) else {}
    client = LLMClient(provider=provider)
    try:
        result = client.chat_completion_json(
            system_prompt=(
                "你是视觉小说发布级结局因果语义校验器。"
                "绝对不要依赖固定关键词、词频或字符距离判断；必须理解变量定义、人物行动、"
                "因果链和结局回报。不同题材可以用完全不同的自然表达兑现同一变量。"
                "必须引用结局正文中的连续原文短句，禁止凭摘要、常识或变量名称补全正文没有写出的事实。"
            ),
            user_prompt=(
                "只输出 JSON，不要输出 Markdown。\n"
                f"变量定义：{json.dumps(variable, ensure_ascii=False, default=str)}\n"
                f"辅助上下文（仅用于理解，不可替代正文证据）：{json.dumps(context or {}, ensure_ascii=False, default=str)}\n"
                f"世界观角色信息：{json.dumps(worldview or {}, ensure_ascii=False, default=str)}\n\n"
                "请按以下结构返回（所有布尔值必须是 JSON true/false）：\n"
                "{\n"
                '  "passed": true 或 false,\n'
                '  "confidence": 0到1之间的小数,\n'
                '  "reason": "简短说明",\n'
                '  "core_chain": {\n'
                '    "variable_behavior_linked": true 或 false,\n'
                '    "observable_payoff": true 或 false,\n'
                '    "causal_payoff": true 或 false,\n'
                '    "route_specific": true 或 false,\n'
                '    "target_linked": true 或 false,\n'
                '    "relationship_confirmation": true 或 false,\n'
                '    "shared_future": true 或 false,\n'
                '    "fatal_state_consistent": true 或 false\n'
                "  },\n"
                '  "evidence": {\n'
                '    "target_character": "正文对应的角色（无则为空）",\n'
                '    "payoff_type": "自然语言描述",\n'
                '    "evidence_quote": "正文中的连续原文短句"\n'
                "  }\n"
                "}\n\n"
                f"结局正文：\n{excerpt}"
            ),
            temperature=0.0,
            max_tokens=1000,
            timeout=180,
        )
    except Exception as exc:
        return {
            "passed": False,
            "confidence": 0.0,
            "reason": f"结局变量语义审查调用失败：{exc}",
            "core_chain": {},
            "evidence": {},
        }
    return result if isinstance(result, dict) else {
        "passed": False,
        "confidence": 0.0,
        "reason": "结局变量语义审查返回格式无效",
        "core_chain": {},
        "evidence": {},
    }


def review_romance_route_payoff(
    *,
    ending_text: str,
    variable_definition: dict | None,
    provider: str,
    worldview: dict | None = None,
    context: dict | None = None,
) -> dict:
    """Backward-compatible alias for the generic variable reviewer."""
    return review_ending_variable_causality(
        ending_text=ending_text,
        variable_definition=variable_definition,
        provider=provider,
        worldview=worldview,
        context=context,
    )


def build_ending_summary_context(chapter_summaries: list, query: str = "") -> str:
    """结局只携带末章附近上下文，并检索与当前结局证据相关的远端决策。"""
    if not chapter_summaries:
        return ""
    from services.story_planner.core.chapter_context import build_layered_summary_block

    return build_layered_summary_block(
        chapter_summaries,
        current_index=len(chapter_summaries) - 1,
        query=query or " ".join(str(item or "") for item in chapter_summaries[-2:]),
        neighbor_radius=2,
        remote_limit=2,
    )


def _sanitize_meta_leak(text: str, title: str = "") -> str:
    """
    删除结局正文里泄漏的元文本（给 AI 的机制词/舞台指令）。
    - 整句含机制关键词 → 删除该句；
    - 括号内的创作说明/舞台指令 → 删除整个括号；
    - 影射好感阈值的『六十五次/六十五个…』计数 → 删除该短句。
    返回清洗后的文本；有改动时打印告警。
    """
    if not text:
        return text
    original = text

    # 1) 删除括号内的舞台指令/创作注释（中文括号），如“（场景转换：进入夺权线）”
    #    仅当括号内出现机制/导演词时才删，避免误删正常括号补充。
    def _strip_paren(m):
        inner = m.group(1)
        if re.search(r"(场景转换|切换到|进入.{0,6}线|数值|维度|好感|兑现|象限|此处|体现最高|变量|镜头)", inner):
            return ""
        return m.group(0)
    text = re.sub(r"（([^）]*)）", _strip_paren, text)

    # 2) 按句切分，丢弃含机制关键词的整句
    #    中文句末标点后切分，保留标点
    sentences = re.split(r"(?<=[。！？\n])", text)
    kept = []
    for s in sentences:
        if any(w in s for w in _META_LEAK_WORDS):
            continue
        # 影射阈值 65 的计数表达
        if re.search(r"六十五(次|个|道|遍|回|日|夜|年|下)", s):
            continue
        kept.append(s)
    text = "".join(kept)

    # 3) 收拾多余空行
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if text != original:
        removed = len(original) - len(text)
        print(f"   🧹 [{title}] 清洗元文本泄漏，删除约 {removed} 字")
    return text


def generate_ending_content(
    node: dict,
    pivot_chapter_text: str,
    style_ref: str,
    provider: str,
    scenes_count: int = None,
    novel_context=None,
    target_chars: int = None,
    chapter_summaries: list = None,
    original_sibling_summary: str = "",
    trigger_variable_info: str = "",
    output_language: str = None,
    sibling_endings: list = None,
    pivot_terminal_states: dict = None,
    pivot_chapter_index: int | None = None,
    ui_mode: str | None = None,
    narrative_pov_contract: dict | None = None,
) -> str:
    """
    为结局节点生成高质量长文（分场大纲 → 循环扩写，每场2000-3000字）。

    node: topology 节点（type=ending, is_original=false）
    pivot_chapter_text: 倒数第二章全文，作为结局的直接上文
    style_ref: 原著前500字，风格对齐
    scenes_count: 场次数量，None 时根据 target_chars 自适应
    novel_context: NovelContext，提供角色卡、关系线、世界规则（防幻觉锚定）
    target_chars: 目标字数（对齐原著末章长度），优先于 scenes_count
    chapter_summaries: 各章节摘要列表，供 LLM 了解全书剧情脉络
    """
    # 根据目标字数自适应场次：每场约2500字，最少2场最多6场
    if target_chars is not None:
        scenes_count = max(2, min(5, round(target_chars / 2500)))
    elif scenes_count is None:
        scenes_count = 4
    client = LLMClient(provider=provider)
    title = node.get("title", "结局")
    summary = node.get("summary", "")
    lang_instr = get_lang_instruction(output_language)
    narrative_pov_contract = _context_narrative_pov_contract(
        novel_context,
        narrative_pov_contract,
    )
    narrative_pov_block = _narrative_pov_prompt_block(narrative_pov_contract)

    # 构建角色/世界锚点（防止 LLM 虚构角色或违背设定）
    char_cards = getattr(novel_context, "char_cards", "") if novel_context else ""
    relationship_map = getattr(novel_context, "relationship_map", "") if novel_context else ""
    world_rules = getattr(novel_context, "world_rules", "") if novel_context else ""
    factions_text = getattr(novel_context, "factions_text", "") if novel_context else ""
    hidden_threads_text = getattr(novel_context, "hidden_threads_text", "") if novel_context else ""
    character_secrets_text = getattr(novel_context, "character_secrets_text", "") if novel_context else ""
    narrative_arc = getattr(novel_context, "narrative_arc", "") if novel_context else ""
    character_behavior_anchors = getattr(novel_context, "character_behavior_anchors", "") if novel_context else ""
    if character_behavior_anchors:
        from services.story_planner.core.novel_reader import (
            filter_behavior_anchors_for_ending,
        )

        character_behavior_anchors = filter_behavior_anchors_for_ending(
            character_behavior_anchors,
            pivot_chapter_index=pivot_chapter_index,
        )
    ending_dimensions = getattr(novel_context, "ending_dimensions", []) if novel_context else []
    is_romance_oriented = bool(getattr(novel_context, "is_romance_oriented", False)) if novel_context else False
    worldview = getattr(novel_context, "worldview", {}) if novel_context else {}
    romance_targets = worldview.get("romance_targets", []) if isinstance(worldview, dict) else []
    variable_legacy = (
        build_variable_legacy_requirement(
            trigger_variable_info,
            worldview,
            ui_mode=ui_mode,
        )
        if trigger_variable_info else ""
    )
    romance_death_safety = (
        "恋爱向结局不得为了区别路线新增死亡、病逝、遗物、葬礼或墓地；"
        "除非原著直接前情已经明确发生死亡，否则路线差异必须来自主角选择的对象、"
        "关系确认方式、双方行动与共同未来。"
        if is_romance_oriented else ""
    )

    # 恋爱向：把 affection_<key> ←→ 角色名 的映射拼成查找表，给 LLM 用
    romance_lookup_text = ""
    if is_romance_oriented and romance_targets:
        lines = []
        for t in romance_targets:
            if not isinstance(t, dict):
                continue
            name = t.get("name", "")
            label = t.get("role_label", "")
            arc = t.get("affection_arc", "")
            lines.append(f"  - 与 {name}（{label}）相关：affection_<{name} 拼音>，情感发展轨迹：{arc}")
        if lines:
            romance_lookup_text = (
                "【恋爱向角色变量映射（数值兑现时必须明确指出对应的人）】\n"
                + "\n".join(lines) + "\n\n"
            )

    anchor_block = ""
    if narrative_arc:
        anchor_block += f"【全书叙事弧线】\n{narrative_arc}\n\n"
    if world_rules:
        anchor_block += f"【世界规则/设定（严格遵守，不得违背）】\n{str(world_rules)}\n\n"
    if factions_text:
        anchor_block += f"{factions_text}\n\n"
    if hidden_threads_text:
        anchor_block += f"{hidden_threads_text}\n\n"
    if character_secrets_text:
        anchor_block += f"{character_secrets_text}\n\n"
    if char_cards:
        anchor_block += f"{char_cards}\n\n"
    if relationship_map:
        anchor_block += f"{relationship_map}\n\n"
    if character_behavior_anchors:
        anchor_block += f"{character_behavior_anchors}\n\n"

    # 结局矩阵骨架（用于命运象限推理）
    ending_matrix_block = ""
    if ending_dimensions:
        lines = []
        for d in ending_dimensions:
            if isinstance(d, dict):
                lines.append(f"  - {d.get('dimension','')}：{', '.join(d.get('options', []))}")
        if lines:
            ending_matrix_block = "【全书结局维度矩阵（你的结局必须占据未被原著/其他平行结局占据的象限）】\n" + "\n".join(lines) + "\n\n"

    # ── Step 1: 生成分场大纲 ──────────────────────────────────────
    # 五管齐下：防撞车锚点 + 数值情感结算
    if original_sibling_summary and is_romance_oriented:
        anti_collision = (
            f"\n🚨 【防撞车强警告】原著在这一阶段的剧情是：【{original_sibling_summary}】。"
            "本路线必须在主角选择的对象、关系确认方式、双方主动行为或共同未来上形成明确差异；"
            "不得靠新增死亡、病逝或遗物制造差异。\n"
        )
    else:
        anti_collision = (
            f"\n🚨 【防撞车强警告】原著在这一阶段的剧情是：【{original_sibling_summary}】。"
            "你的大纲中，主角的抉择、最终的命运、核心人物的生死，【绝对不能】与上述原著剧情重合！必须走向另一个极端或另一种可能性！\n"
            if original_sibling_summary else ""
        )
    sibling_block = ""
    if sibling_endings and is_romance_oriented:
        lines = "\n".join(f"  - {s}" for s in sibling_endings if s)
        sibling_block = (
            "\n🚨 【恋爱路线横向对比】本书还有以下角色路线：\n"
            f"{lines}\n"
            "请通过目标角色、关系确认动作、双方承担的代价和未来安排区分路线；"
            "禁止通过核心人物死亡、遗物或葬礼强行制造差异。\n"
        )
    elif sibling_endings:
        lines = "\n".join(f"  - {s}" for s in sibling_endings if s)
        sibling_block = (
            f"\n🚨 【横向对比警告】本书还有以下其他平行结局，你的大纲必须与它们在「核心人物生死」和「情感基调」上截然不同：\n"
            f"{lines}\n"
            "禁止与上述任何一条在生死结果或情感基调上重合，必须占据不同的命运象限。\n"
        )
    stat_checkout = (
        f"\n【数值叙事兑现】玩家是因为在软分支中持续积累了【{trigger_variable_info}】才解锁此结局的。"
        "这意味着玩家在整个游戏过程中，反复在这个人格维度上做出了一致的选择——这种积累必须在结局高潮里有具体的叙事回报：\n"
        "  - 不是「因为羁绊深」这种空洞描述，而是：这个维度的积累在关键时刻改变了某个角色的决定，或者让主角做出了原著中不可能做出的选择。\n"
        "  - 结局的转折点必须能让玩家感受到「我之前的每一次选择都指向了这一刻」。\n"
        "  - 人格轴属于主角：核心回报必须由主角自己的可观察行动触发，不能把最终行动者换成旁人整理遗物、代替发声。\n"
        "  - 若高分状态/结局指向没有明确要求死亡，不得用主角突然病逝、牺牲或只留下遗物作为所谓回报。\n"
        if trigger_variable_info else ""
    )
    outline_system = (
        "你是顶级视觉小说结局编剧 + 命运象限设计师。"
        "你的任务是为一个**与原著结局走向截然不同**的平行结局规划完整分场剧本大纲。\n"
        "每场必须有具体场景、冲突、情感弧，而非模糊方向。所有角色行为符合角色卡设定，不得新增角色。\n\n"
        + narrative_pov_block
        + "【内部推理顺序 — 不输出，仅用于规划】\n"
        + "  1. 读【原著末章摘要】（已置于 user prompt 中，请认真识别），确定原著结局占据的象限（主角生死/复仇成败/恋爱归宿/情感基调）。\n"
        + "  2. 读【全书结局维度矩阵】+【横向对比警告】中其他平行结局，列出**未被占据**的象限。\n"
        + "  3. 本结局必须落在某个未被占据的象限，且每个核心维度都与原著相反或正交（不能同时与原著一致）。\n"
        + "  4. 触发变量是 affection_X（恋爱向）→ 结局核心必须是与 X 的关系收束，且要让玩家感受到「正是因为我一路偏向 X 才有这个结局」。\n"
        + "  5. 触发变量是 courage/calmness 等人格轴 → 结局核心必须是主角人格的命运回响，且高分维度的具体行为在结局里清晰可见。\n"
        + "  6. 推理完成后，再开始输出 scenes 大纲。\n\n"
        + anti_collision
        + sibling_block
        + stat_checkout
        + (f"\n【恋爱路线生死模板隔离】{romance_death_safety}\n" if romance_death_safety else "")
        + (f"\n{variable_legacy}\n" if variable_legacy else "")
        + ("\n" + lang_instr if lang_instr else "")
    )
    summaries_block = build_ending_summary_context(
        chapter_summaries or [],
        query=" ".join(item for item in (summary, trigger_variable_info, variable_legacy) if item),
    )

    romance_template_hint = (
        "【模板：恋爱向】触发变量是 affection_X，结局必须以主角与 X 的关系收束作为情感主线。"
        + (f"\n【生死模板隔离】{romance_death_safety}" if romance_death_safety else "")
        + "\n\n"
        if is_romance_oriented else
        "【模板：人格向】触发变量是人格轴，结局必须呈现主角人格的命运回响（高分维度的行为在结局清晰可见）。\n\n"
    )

    # pivot 末角色状态硬约束：结局开始那一刻的事实，不可违反
    from services.story_planner.core.novel_reader import format_terminal_states_for_prompt
    pivot_states_block = format_terminal_states_for_prompt(
        pivot_terminal_states or {},
        label="结局开始时的角色状态（倒数第二章末态，本结局不可违反）",
    )

    outline_user = (
        f"{narrative_pov_block}"
        f"{anchor_block}"
        f"{ending_matrix_block}"
        f"{romance_lookup_text}"
        f"{summaries_block}"
        f"{romance_template_hint}"
        f"{variable_legacy + chr(10) + chr(10) if variable_legacy else ''}"
        f"{pivot_states_block}"
        f"## 原著风格参考\n{style_ref}\n\n"
        f"## 直接前情（倒数第二章全文，结局大纲的第一场【必须】紧接着此处的最后一句话发生）\n{pivot_chapter_text.strip()}\n\n"
        f"## 结局走向\n{summary}\n\n"
        f"## 任务\n"
        f"请为《{title}》规划 {scenes_count} 个场次的详细剧本大纲。"
        "每场承载约 2500 字，必须覆盖：时间/地点、出场人物、核心冲突、具体动作脉络（4-6 条）、情感落点。\n"
        "整体结构要求：\n"
        "  - **第 1 场**：紧接直接前情最后一句话发生，并在开场 30% 处呈现『触发变量高分』的具象状态（数值兑现阶段 A）\n"
        "  - **中段**：情感爆发，让积累的高分维度在关键决策点产生不可替代的作用（数值兑现阶段 B）\n"
        "  - **倒数第二场**：高潮抉择/对决，写出『一个只有走过这条路的主角才会做出的决定』\n"
        "  - **尾场**：余韵收束，用一个『如果维度低就不会发生的画面』作为收尾（数值兑现阶段 C）\n"
        "【严禁】重复叙述直接前情已发生的情节；从前情结束的那一刻往后写。\n"
        "【严禁】结局走向与原著末章重合（参考上方【★原著末章】摘要做反向）。\n"
        "【严禁】违反【结局开始时的角色状态】——只有 life_status=death_confirmed 才按确认死亡处理，"
        "不得在现实层复活、对话或主动决定；death_reported/death_suspected 仍是未确认信息，不得擅自坐实。"
        "确认存活的角色可以在本结局推进伤情/赴死/自我救赎，但必须有直接证据和合理铺垫。\n\n"
        "输出严格 JSON：\n"
        '{"scenes": [{"scene_no": 1, "time_location": "", "characters": [], '
        '"core_conflict": "", "action_beats": "", "emotional_note": "", '
        '"stat_payoff_phase": "A/B/C 中之一，标注本场承担数值兑现哪一阶段"}]}'
    )
    try:
        outline = client.chat_completion_json(
            system_prompt=outline_system,
            user_prompt=outline_user,
            temperature=0.45,
            max_tokens=2500,
        )
        # LLM 有时直接返回列表而非 {"scenes": [...]}
        if isinstance(outline, list):
            scenes = outline
        else:
            scenes = outline.get("scenes", [])
        if not scenes:
            raise ValueError("大纲为空")
        print(f"   ✅ {title} 分场大纲完成，共{len(scenes)}场")
    except Exception as e:
        print(f"   ⚠️  {title} 大纲失败（{e}），使用多场兜底（{scenes_count} 场结构化骨架）")
        # 多场化 fallback：从 worldview 取关键人物 + 结局 summary 拼出 N 场骨架
        worldview_cf = getattr(novel_context, "worldview", {}) if novel_context else {}
        chars_list = worldview_cf.get("characters", []) if isinstance(worldview_cf, dict) else []
        protagonist_name = next(
            (c.get("name", "主角") for c in chars_list if isinstance(c, dict) and c.get("role", "").startswith("主")),
            "主角"
        )
        important_chars = [c.get("name", "") for c in chars_list[:5] if isinstance(c, dict) and c.get("name")]
        # 数值兑现三段式映射到场次：开局A、中段B、收束C；中间场补 A
        phase_template = {
            0: ("A", "起 — 数值兑现阶段A：让主角处境/心境呈现该维度高分的具象状态"),
            1: ("B", "承 — 数值兑现阶段B：高分维度在关键决策点产生不可替代的作用"),
            2: ("B", "转 — 数值高分让某个角色做出原著中不会做的决定"),
            3: ("C", "合 — 数值兑现阶段C：用一个『如果维度低就不会发生的画面』作收尾"),
        }
        scenes = []
        for idx in range(scenes_count):
            phase_key = phase_template.get(idx, ("C", "余韵收束"))
            scenes.append({
                "scene_no": idx + 1,
                "time_location": "（请基于前情自然推导）",
                "characters": important_chars[:3],
                "core_conflict": (
                    f"{phase_key[1]}；与原著结局走向不同（参考结局 summary）：{summary}"
                    if idx == 0 else
                    f"{phase_key[1]}；承接上一场推进结局走向"
                ),
                "action_beats": (
                    f"主角 {protagonist_name} 在数值兑现阶段 {phase_key[0]} 的核心动作；"
                    f"{trigger_variable_info or '基于触发变量'}的具象体现"
                ),
                "emotional_note": phase_key[1],
                "stat_payoff_phase": phase_key[0],
            })

    # ── Step 2: 循环扩写每场 ──────────────────────────────────────
    write_system = (
        "你是顶级视觉小说作家，正在撰写一部高质量结局章节。\n"
        + narrative_pov_block
        + "写作铁律：\n"
        + "1. 每场严格 2500-2800 字，用细节填满——环境光影、肢体动作、说话时的停顿、呼吸节奏。\n"
        + "2. 对话必须有潜台词，人物说的不是全部心里想的。\n"
        + "3. 心理活动要深入，用具体的画面/动作外化情绪，不要写『他很难过』这类直白结论。\n"
        "   ⚠️ 但严禁反复使用同一个比喻模板——尤其禁止「念头/思绪像一根(生锈的)刺/针/钝刀，在他呼吸/转身的瞬间扎进去，"
        "   等他意识到已经流了血」这一类句式，以及「他的手没有抖，三个月前还抖得握不住，现在稳得像磐石」这一类句式。"
        "   同一篇结局里，一个比喻只能用一次，换着法子写。\n"
        "4. 节奏要沉，画面要慢，每个重要时刻都要展开，不要跳过。\n"
        "5. 不写场景标题，不写总结，直接输出正文，在画面或台词上戛然收住，等待下一场接续。\n"
        "6. 严格遵守角色卡设定，不得创造新角色，不得让角色做出与其性格矛盾的行为。\n"
        "7. **数值兑现要藏进画面，不能露出机制**：本场若标注了数值兑现阶段，必须把它化成具体的、"
        "   可指认的人物行为或细节（如『他的手没有抖』），而不是写『他凭着勇气做出了选择』这种空话。\n"
        "8. **与原著结局走向截然不同**：核心人物生死、事件走向、情感基调，至少有 2 项与原著相反或正交。\n"
        "══════════════════════════════════════\n"
        "🚫【绝对禁止的元文本 — 违反即为废稿】🚫\n"
        "══════════════════════════════════════\n"
        "正文是给玩家看的成品小说，严禁出现任何游戏机制/创作过程的词汇。以下词一个都不许写进正文：\n"
        "  affection、维度、好感度、数值、阈值、专属画面、低维度/高维度、梦魇维度、象限、变量、玩家、选项、分支、兑现、触发。\n"
        "尤其禁止把阈值数字写成叙事，例如『六十五次』『六十五个日夜』——不要出现任何影射好感阈值(65)的计数。\n"
        "也禁止括号里写创作说明/舞台指令，如『（场景转换：进入夺权线）』『（此处体现最高好感）』。\n"
        "情感的累积只能通过具体的人、事、物、回忆来体现，绝不能用机制术语点破。"
        + (f"\n【恋爱路线生死模板隔离】{romance_death_safety}" if romance_death_safety else "")
        + ("\n" + lang_instr if lang_instr else "")
    )

    final_content = ""
    prev_context = pivot_chapter_text.strip()

    for i, scene in enumerate(scenes):
        payoff_phase = scene.get("stat_payoff_phase", "")
        payoff_hint = ""
        if payoff_phase == "A":
            payoff_hint = "本场是数值兑现阶段 A：让主角处境/心境呈现该维度高分的具象状态，让玩家立刻感受到积累。"
        elif payoff_phase == "B":
            payoff_hint = "本场是数值兑现阶段 B：高分维度在关键决策点产生不可替代的作用——某个角色的决定/主角做出原著中不会做的选择。"
        elif payoff_phase == "C":
            payoff_hint = "本场是数值兑现阶段 C：用一个『如果维度低就不会发生的画面』作收尾，让玩家在结尾看见命运的回响。"

        write_user = (
            f"{narrative_pov_block}"
            f"{anchor_block}"
            f"{romance_lookup_text}"
            f"{pivot_states_block}"
            f"## 原著风格参考\n{style_ref}\n\n"
            f"## 前情提要（必须自然衔接，不要复述）\n{prev_context}\n\n"
            f"## 本场次拍摄指南\n"
            f"时间/地点：{scene.get('time_location', '')}\n"
            f"出场人物：{', '.join(scene.get('characters', []))}\n"
            f"核心冲突：{scene.get('core_conflict', '')}\n"
            f"动作脉络：{scene.get('action_beats', '')}\n"
            f"情感落点：{scene.get('emotional_note', '')}\n"
            + (f"【数值兑现要求】{payoff_hint}\n" if payoff_hint else "")
            + (f"【触发变量信息】{trigger_variable_info}\n" if trigger_variable_info else "")
            + (f"【本结局不可替代的因果证据】{variable_legacy}\n" if variable_legacy else "")
            + (f"【恋爱路线生死模板隔离】{romance_death_safety}\n" if romance_death_safety else "")
            + f"\n## 写作要求\n"
            f"全力撰写本场戏正文，2500-2800 字。"
            "本场涉及的角色生死必须严格遵循上方【结局开始时的角色状态】——只有确认死亡者最多以回忆/幻觉/尸体出现，不得在现实层复活对话；传闻/疑似死亡不得擅自坐实；确认存活者可推进命运但需合理铺垫。"
            f"{'这是最后一场，请给出有重量的结束画面或台词，让读者久久回味。结尾必须是『如果维度低就不会发生的画面』。' if i == len(scenes)-1 else '不要给这场戏写结尾总结，只写透当下切片，让下一场自然接续。'}"
        )
        # 写场 retry：字数 < 2200 视为不达标
        scene_text = ""
        last_short = ""
        for attempt in range(2):
            current_user = write_user
            if attempt > 0 and last_short:
                current_user += (
                    f"\n\n⚠️ 上次输出仅 {len(last_short)} 字，远低于 2500-2800 字目标。"
                    "请重新撰写，必须达到字数要求，禁止草草收束、跳跃叙述。"
                )
            try:
                candidate = client.chat_completion(
                    system_prompt=write_system,
                    user_prompt=current_user,
                    temperature=0.85,
                    max_tokens=5500,
                ).strip()
            except Exception as e:
                print(f"   ⚠️  {title} 第{i+1}场生成失败（attempt={attempt+1}, {e}）")
                continue
            if len(candidate) >= 2200:
                scene_text = candidate
                print(f"   ✅ {title} 第{i+1}/{len(scenes)}场完成（{len(scene_text)}字, attempt={attempt+1}）")
                break
            else:
                print(f"   🔁 {title} 第{i+1}场字数不足（{len(candidate)}字），retry...")
                last_short = candidate
                scene_text = candidate  # 留作兜底
        if not scene_text:
            # 两次都失败：直接跳过该场，绝不把 core_conflict（内部指令）等元文本
            # 写进正文。此前的占位会泄漏“（…内心…的和解）/故事在此延续”到成品。
            print(f"   ⚠️  {title} 第{i+1}场两次生成均失败，跳过该场（避免元文本泄漏进正文）")
            continue

        # 清洗元文本泄漏（affection/维度/六十五次/舞台指令等）后再入正文
        scene_text = _sanitize_meta_leak(scene_text, title)

        final_content += scene_text + "\n\n"
        # 下一场前情：用本场全文，不截断
        prev_context = scene_text

    result_text = final_content.strip()

    # 一致性质量门禁：检查 -> 定点修复 -> 再检查，复检失败则阻止发布。
    if result_text and char_cards:
        from services.story_planner.core.novel_reader import repair_content_consistency_or_raise
        fixed_facts = character_behavior_anchors
        result_text = repair_content_consistency_or_raise(
            result_text,
            title,
            char_cards,
            fixed_facts,
            provider,
            output_language,
        )

    return result_text
