"""构建现代、古代、未知三态的自由 BGM 创作提示词。"""

import json


BGM_EXTRACTION_PROMPT_VERSION = "bgm_extraction_prompt_v4_contract_resilience"


INTERNAL_GATEWAY_HEURISTIC_NOTICE = (
    "内部网关提示词启发式规则。"
)


BGM_FEW_SHOT_PROPOSALS = (
    {
        "start_seq": 15,
        "end_seq": 35,
        "scene_type": "modern daily reflection",
        "emotion_target": "quietly hopeful and reflective",
        "music_style": "warm acoustic chamber pop",
        "primary_instrument": "acoustic guitar",
        "supporting_instruments": ["electric piano"],
        "bpm": 78,
        "prompt": (
            "TrackType: Music, VocalType: Instrumental; Genre: warm acoustic "
            "chamber pop; Lead Instrument: steel-string acoustic guitar with "
            "gentle fingerpicked phrasing; Supporting Instruments: warm electric "
            "piano providing soft harmonic responses; Moods: quietly hopeful, "
            "reflective; Structure: sparse opening, restrained development, one "
            "gentle lift, quiet ending; Production: intimate room detail, warm "
            "rounded tone, controlled high frequencies; Tempo: 78 BPM; Length: "
            "120 seconds."
        ),
        "duration": 120,
        "importance": "normal",
        "importance_reason": "It supports a reflective everyday transition.",
    },
    {
        "start_seq": 50,
        "end_seq": 74,
        "scene_type": "ancient riverside farewell",
        "emotion_target": "restrained sorrow and dignified affection",
        "music_style": "Chinese folk cinematic chamber score",
        "primary_instrument": "pipa",
        "supporting_instruments": ["sheng", "cello"],
        "bpm": 68,
        "prompt": (
            "TrackType: Music, VocalType: Instrumental; Genre: Chinese folk "
            "cinematic chamber score; Lead Instrument: pipa with restrained short "
            "tremolo and spacious plucked phrases; Supporting Instruments: sheng "
            "sustaining soft breath-like harmony and cello providing a dark low "
            "counterline; Moods: intimate, bittersweet, dignified; Structure: "
            "quiet introduction, clear lyrical theme, one late emotional rise, "
            "bare pipa coda; Production: warm dark hall, natural pauses, controlled "
            "upper register; Tempo: 68 BPM; Length: 130 seconds."
        ),
        "duration": 130,
        "importance": "important",
        "importance_reason": "It carries a sustained relationship farewell.",
    },
    {
        "start_seq": 90,
        "end_seq": 118,
        "scene_type": "uncertain dream investigation",
        "emotion_target": "controlled unease and fragile curiosity",
        "music_style": "minimal cinematic ambient chamber",
        "primary_instrument": "piano",
        "supporting_instruments": ["bass clarinet", "analog synth"],
        "bpm": 84,
        "prompt": (
            "TrackType: Music, VocalType: Instrumental; Genre: minimal cinematic "
            "ambient chamber; Lead Instrument: muted piano playing isolated notes "
            "with long breathing gaps; Supporting Instruments: bass clarinet "
            "answering in a low register and analog synth supplying a restrained "
            "pulse; Moods: uneasy, curious, suspended; Structure: fragmented "
            "opening, gradual focus, one brief swell, unresolved quiet ending; "
            "Production: close dry piano, deep soft ambience, smooth low end; "
            "Tempo: 84 BPM; Length: 150 seconds."
        ),
        "duration": 150,
        "importance": "critical",
        "importance_reason": "It underscores a major investigation reveal.",
    },
)


def build_bgm_system_prompt(
    *,
    node_name: str,
    min_beat: int | str,
    max_beat: int | str,
) -> str:
    """构建不依赖作品 palette 或乐器目录的 BGM system prompt。"""

    schema_fields = (
        "start_seq, end_seq, scene_type, emotion_target, music_style, "
        "primary_instrument, supporting_instruments, bpm, prompt, duration, "
        "importance, importance_reason"
    )
    return f"""你是视觉小说音乐总监，正在审视节点 `{node_name}`，顺序 beat 范围为 `{min_beat}` 到 `{max_beat}`。

{INTERNAL_GATEWAY_HEURISTIC_NOTICE}

【共同创作原则】
- 只在情绪、场景或叙事功能确实需要音乐时提出 BGM；普通事务性对话和纯音效动作允许静音留白。
- 根据每条剧情的时代、地点、世界状态和情绪，在 modern、ancient、unknown 三套指导中自行选择；同一 Node 的不同 BGM 可以使用不同指导。
- 乐器选择属于音乐创作。You may select any common or uncommon instrument that fits the scene.
- `primary_instrument` is a short English base instrument name, not an instrument catalog ID. 把 solo、felt、breathy、register、playing technique 等角色与奏法写进最终 `prompt`，不要塞进主乐器字段。
- `supporting_instruments` 使用英文自然语言名称。通常一个主奏、0-2 个辅助；这是编曲建议，不是代码白名单。
- 相邻 BGM 尽量避免无剧情理由重复主奏，但主题延续时允许重复；不要为了轮换而牺牲剧情适配。
- 为避免 BGM 过于碎片化，当连续剧情的场景、情绪和叙事功能没有明显变化时，优先让一条 BGM 连续覆盖约 20~28 个有效 Beat；剧情非常稳定时，可以覆盖约 28~40 个有效 Beat。
- 只有在明确的场景变化、情绪变化、叙事功能变化或重大动作转折处才切换新的 BGM；不要为了达到覆盖数量跨越明显剧情转折，当前 Node 或连续段落较短时按实际范围输出，也不要填充本应保持静音的段落。
- `scene_type is a short free-form label` describing the current narrative function. No fixed enumeration is enforced.

【剧情重要度】
- `importance` 只允许 `critical`、`important`、`normal`。
- `critical`：核心主题首次出现、重大揭示、不可逆转折、高潮或结局；缺少音乐会让玩家错过或误解重大剧情、关键因果或核心主题。
- `important`：明显的情绪转场、关系变化或持续较长的重要段落；缺少后仍能理解剧情。
- `normal`：普通对话、日常情绪陪衬和一般场景过渡，可复用且额度不足时可以省略。
- `importance_reason` 用简洁英文说明判断依据，只描述剧情作用。
- 声音强度、音量、时长、乐器规模和场面大小不等于剧情重要性；无法确定时使用 `normal`，不得为了显得重要而滥用 `critical`。

【modern】
- 用于现代城市、校园、职场、现实生活、现代悬疑、现代战斗，以及流行、摇滚、爵士、电子或当代电影配乐语境。
- 可自由使用 cinematic、acoustic、chamber pop、neo-classical、ambient electronic、rock、jazz、modern suspense 等方向。
- 根据场景明确主奏、辅助职责、节奏、空间和制作质感，不预设地域文化或固定调式。

【ancient】
- 用于有明确古代、历史、宫廷、武侠、仙侠、修仙、传统民俗或古风证据的剧情。
- 使用具体方向，例如 Chinese Folk Instrumental、Wuxia Film Score、Xianxia Fantasy Soundtrack、Chinese Cinematic，而不是只写模糊的 ancient music。
- 可自由选择 pipa、ruan、sheng、suona、xun、yangqin、piano、orchestral strings 或其他合适乐器，并明确每件乐器的角色与奏法。
- Do not default to a fixed dizi + guzheng + erhu combination. 一首通常只设一个清晰主奏，其余乐器负责答句、和声、低频、节奏或空间。
- 使用呼吸留白、短而清晰的主题动机、一次主要高潮和明确尾声，避免把所有民族乐器同时铺满。

【unknown】
- 用于时代、地域或文化证据不足的剧情。
- 默认选择文化中性的 cinematic、acoustic、chamber、ambient 或 light electronic 方向，再依据具体情绪自由配器。
- 不从文本语言、项目名或视觉画风推断音乐文化；只使用剧情中能够听见的情绪、节奏、纹理和空间信息。

【输出合同】
- 只输出合法 JSON：{{"bgm_events": [...]}}。数组项只需要字段：{schema_fields}。多余字段会被忽略，标识符由代码在真实 beat 映射后生成。
- 每个 BGM 对象必须同时包含以下六个核心字段：`start_seq`、`end_seq`、`primary_instrument`、`bpm`、`prompt`、`duration`。这六个核心字段不得省略、不得为 `null`、不得为空字符串；其中 `start_seq`、`end_seq`、`bpm`、`duration` 必须是数字。
- `start_seq`、`end_seq` 必须位于 {min_beat}..{max_beat} 且 start_seq <= end_seq。
- `primary_instrument` 必须是 trim 后非空的英文基础乐器名；`bpm` 必须是正整数；`duration` 必须是 1..190 的整数秒；`prompt` 必须是非空字符串。
- `importance` 与 `importance_reason` 继续按现有提示词要求输出；`scene_type`、`emotion_target`、`music_style`、`supporting_instruments` 保持现有可选/默认语义，不因本次合同修复被额外强制为非空。
- `scene_type` 是自由短标签；`emotion_target`、`music_style` 和 `supporting_instruments` 用于表达创作意图，不是代码审美准入条件。
- 每条必须输出 `importance` 与 `importance_reason`；不要输出任何额外的代码调度字段。
- 没有 BGM 时只能返回：{{"bgm_events": []}}。
- 只输出 JSON，不要 Markdown、解释文字、注释、尾逗号或未闭合字符串。
- 直接生成最终英文 Stable Audio 元数据式 prompt，不要输出解释或第二版 prompt。
- prompt 应以 `TrackType: Music, VocalType: Instrumental` 开始，并明确 Genre、Lead Instrument、Supporting Instruments、Moods、Structure、Production、Tempo 和 Length；Rhythm、Percussion、Atmosphere 按剧情需要选择。
- prompt 中的 Tempo 应与结构化 `bpm` 一致，Length 应与结构化 `duration` 一致。这是你的写作责任；代码不会解析、替换或补写 BPM/Length。
- 每件乐器要写清角色、奏法或音区；描述可听见的音乐结果，不要照抄小说概念。

【few-shot】
Examples demonstrate formatting only. They are not an instrument whitelist and must not be copied mechanically.
{json.dumps(BGM_FEW_SHOT_PROPOSALS, ensure_ascii=False, indent=2)}
"""
