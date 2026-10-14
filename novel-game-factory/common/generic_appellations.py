"""泛称识别：判断一个称呼该整体保留、只换姓，还是当完整人名改名。

换名的目标是脱敏真实人名。泛称（"系统"、"司机"、"爸爸"）不含人名信息，
换掉反而破坏语义——"司机"变"沈机"、"系统"变"沈统"都是错的。
但"王司机"这类"姓 + 职业/关系词"含真实姓氏，只换姓即可。
"""

import re
from typing import Optional, Tuple

# 职业、身份、关系类通用名词。作为整体出现时是泛称；跟在姓氏后面时只换姓。
GENERIC_ROLE_WORDS = {
    # 职业
    "司机", "医生", "护士", "警察", "老师", "教师", "教授", "校长", "主任",
    "厂长", "经理", "老板", "秘书", "会计", "律师", "法官", "记者", "编辑",
    "作家", "画家", "歌手", "演员", "导演", "厨师", "服务员", "保安", "保姆",
    "快递员", "外卖员", "售货员", "店长", "店员", "农民", "工人", "士兵",
    "队长", "班长", "组长", "科长", "处长", "局长", "县长", "市长", "村长",
    "郡守", "县令", "将军", "元帅", "统领", "首领", "领兵者", "官员", "太监",
    "掌柜", "小二", "郎中", "大夫", "稳婆", "镖师", "车夫", "船夫", "樵夫",
    # 亲属与关系
    "爸爸", "妈妈", "父亲", "母亲", "爹", "娘", "爹爹", "娘亲", "老爸", "老妈",
    "爷爷", "奶奶", "外公", "外婆", "姥姥", "姥爷", "太爷", "太奶",
    "哥哥", "姐姐", "弟弟", "妹妹", "兄长", "大哥", "大姐", "二哥", "小弟",
    "叔叔", "阿姨", "伯伯", "舅舅", "婶婶", "姑姑", "姑妈", "姨妈", "舅妈",
    "表哥", "表姐", "表弟", "表妹", "堂哥", "堂姐", "堂弟", "堂妹",
    "儿子", "女儿", "孙子", "孙女", "侄子", "侄女", "外甥", "外甥女",
    "丈夫", "妻子", "老公", "老婆", "夫君", "娘子", "未婚妻", "未婚夫",
    "同学", "同事", "室友", "邻居", "朋友", "哥们", "闺蜜",
    # 下人、随从、师门
    "丫鬟", "丫头", "侍女", "侍从", "书童", "家丁", "管家", "护卫", "保镖",
    "随从", "手下", "部下", "下属", "仆人", "佣人", "婢女", "小厮", "下人",
    "门客", "亲信", "心腹", "跟班", "副手", "属下",
    "师父", "师傅", "师兄", "师姐", "师弟", "师妹", "徒弟", "弟子", "门徒",
    "师叔", "师伯", "师祖", "长老", "前辈", "晚辈", "宗主",
    # 敬称与身份
    "大人", "小姐", "公子", "先生", "夫人", "太太", "老爷", "少爷", "客官",
    "娘子", "相公", "郎君", "公主", "王妃", "贵妃", "陛下", "殿下",
}

# 不含任何人名、必须整体保留的泛称。
GENERIC_STANDALONE = {
    # 系统与旁白
    "系统", "旁白", "画外音", "提示", "声音", "男主", "女主", "主角",
    # 泛指人物
    "男人", "女人", "男子", "女子", "汉子", "壮汉", "少年", "少女", "青年",
    "老者", "老人", "老头", "老妇", "老婆婆", "老太太", "孩子", "娃子",
    "姑娘", "美人", "佳人", "书生", "秀才", "仙女", "仙子",
    "路人", "陌生人", "外人", "众人", "旁人", "来人", "某人", "他人",
    "身影", "影子", "未知", "无名氏", "无名",
    # 代词
    "他", "她", "它", "你", "我", "咱", "俺", "那人", "此人",
    "对方", "大家", "诸位",
}

_COMPOUND_SURNAMES = {
    "欧阳", "司马", "上官", "诸葛", "东方", "皇甫", "尉迟", "公孙",
    "慕容", "宇文", "司徒", "司空", "令狐", "轩辕", "南宫", "夏侯",
}

# 昵称前缀，如"小明"、"老张"。这类称呼的核心才是人名。
_NICKNAME_PREFIXES = ("阿", "小", "老")

# 常见汉族姓氏。姓氏是封闭集合，泛称词表却穷举不完，所以模型不可用时的兜底换姓
# 只认这里的姓，宁可少改也不能把"家长""提问者"拆成姓+名换出"裴长""谢问者"。
COMMON_SURNAMES = {
    "赵", "钱", "孙", "李", "周", "吴", "郑", "王", "冯", "陈", "褚", "卫",
    "蒋", "沈", "韩", "杨", "朱", "秦", "尤", "许", "何", "吕", "施", "张",
    "孔", "曹", "严", "华", "金", "魏", "陶", "姜", "戚", "谢", "邹", "喻",
    "柏", "水", "窦", "章", "云", "苏", "潘", "葛", "奚", "范", "彭", "郎",
    "鲁", "韦", "昌", "马", "苗", "凤", "花", "方", "俞", "任", "袁", "柳",
    "酆", "鲍", "史", "唐", "费", "廉", "岑", "薛", "雷", "贺", "倪", "汤",
    "滕", "殷", "罗", "毕", "郝", "邬", "安", "常", "乐", "于", "时", "傅",
    "皮", "齐", "康", "伍", "余", "元", "顾", "孟", "平", "黄", "和", "穆",
    "萧", "尹", "姚", "邵", "湛", "汪", "祁", "毛", "禹", "狄", "米", "贝",
    "明", "臧", "计", "伏", "成", "戴", "谈", "宋", "茅", "庞", "熊", "纪",
    "舒", "屈", "项", "祝", "董", "梁", "杜", "阮", "蓝", "闵", "季", "贾",
    "路", "娄", "危", "江", "童", "颜", "郭", "梅", "盛", "林", "钟", "徐",
    "邱", "骆", "高", "夏", "蔡", "田", "樊", "胡", "凌", "霍", "虞", "万",
    "支", "柯", "管", "卢", "莫", "房", "缪", "干", "解", "应", "宗", "丁",
    "宣", "邓", "郁", "单", "杭", "洪", "包", "诸", "左", "石", "崔",
    "吉", "钮", "龚", "程", "嵇", "邢", "裴", "陆", "荣", "翁", "荀", "羊",
    "甄", "曲", "封", "储", "靳", "汲", "邴", "糜", "松", "井", "段", "富",
    "巫", "乌", "焦", "巴", "弓", "牧", "隗", "山", "谷", "车", "侯", "宓",
    "蓬", "全", "郗", "班", "仰", "秋", "仲", "伊", "宫", "宁", "仇", "栾",
    "暴", "甘", "厉", "戎", "祖", "武", "符", "刘", "景", "詹", "束", "龙",
    "叶", "幸", "司", "韶", "黎", "蓟", "薄", "印", "宿", "白", "怀", "蒲",
    "邰", "从", "索", "赖", "卓", "蔺", "屠", "池", "乔", "阴", "胥", "能",
    "苍", "双", "岳", "帅", "缑", "亢", "况", "后", "有", "琴", "商",
    "牟", "佘", "佴", "伯", "赏", "墨", "哈", "谯", "笪", "年", "爱", "阳",
    "慕", "楚", "闫", "覃", "翟", "谭", "贡", "劳", "逄", "姬", "申", "扶",
    "堵", "冉", "宰", "郦", "雍", "桑", "寿", "通", "燕", "浦", "尚", "农",
    "温", "别", "庄", "晏", "柴", "瞿", "阎", "充", "慎", "戈", "廖", "庾",
    "终", "暨", "居", "衡", "步", "都", "耿", "满", "弘", "匡", "国", "文",
    "寇", "广", "禄", "阙", "东", "欧", "殳", "沃", "利", "蔚", "越", "夔",
    "隆", "师", "巩", "厍", "聂", "晁", "勾", "敖", "融", "冷", "訾", "辛",
    "阚", "毋", "沙", "乜", "养", "鞠", "须", "丰", "巢", "关", "蒯", "相",
    "查", "荆", "红", "游", "竺", "权", "逯", "盖", "益", "桓", "公",
}
COMMON_SURNAMES |= _COMPOUND_SURNAMES

_CHINESE_ONLY = re.compile(r"^[一-龥]+$")

# 音译全名的名/姓分隔符（"林雷·巴鲁克""艾丽斯·达夫"）。
# extract_plots_to_db 的描述短语判定也用同一个字符集，从这里导入，
# 两处判据必须一致：那边认定为音译名而豁免长度兜底的写法，这边必须能切分。
INTERPUNCT_RE = re.compile(r"[·‧・•∙]")

# 与 extract_plots_to_db._descriptive_length_core 相同的音译名判据。
_TRANSLITERATED_MAX_SEGMENTS = 3
_TRANSLITERATED_MAX_SEGMENT_LEN = 4


def split_transliterated_name(name: str) -> Optional[list]:
    """把音译复合名按间隔号切成段列表，不是音译名则返回 None。

    例："林雷·巴鲁克" -> ["林雷", "巴鲁克"]。

    判据与 extract_plots_to_db 的音译名豁免保持一致（2-3 段、每段 ≤4 字）：
    那里靠它把音译全名从"描述短语"里摘出来，这里靠它定位家族名段。两边不一致
    会让一个名字在一处被当人名、在另一处被当描述短语。
    """
    value = str(name or "").strip()
    if not value or not INTERPUNCT_RE.search(value):
        return None
    segments = [part for part in INTERPUNCT_RE.split(value) if part.strip()]
    if not 2 <= len(segments) <= _TRANSLITERATED_MAX_SEGMENTS:
        return None
    if any(len(part) > _TRANSLITERATED_MAX_SEGMENT_LEN for part in segments):
        return None
    return [part.strip() for part in segments]


def family_segment(name: str) -> Optional[str]:
    """返回音译复合名的家族名段（最后一段），不是音译名则返回 None。

    音译名里家族名是共享标识："巴鲁克家族"、"巴鲁克家的长子" 都由它派生，
    所以它和中文姓氏一样是"同族角色必须一起换"的那一段。
    """
    segments = split_transliterated_name(name)
    return segments[-1] if segments else None


def is_generic_appellation(name: str) -> bool:
    """整体是泛称、完全不该改名时返回 True。"""
    value = str(name or "").strip()
    if not value:
        return False
    if value in GENERIC_STANDALONE or value in GENERIC_ROLE_WORDS:
        return True
    # "小明的爸爸""教数学的老师"等描述性称谓：含"的"且不以具名开头时无法确定指谁，
    # 其人名部分由主名替换规则统一处理，这里不当作可改名的完整人名。
    if "的" in value:
        return True
    return False


def split_surname_and_role(name: str) -> Optional[Tuple[str, str]]:
    """把"姓 + 职业/关系词"拆成 (姓, 身份词)，否则返回 None。

    例："王司机" -> ("王", "司机")；"欧阳老师" -> ("欧阳", "老师")。
    只有这种形式才适用"只换姓、保留身份词"。
    """
    value = str(name or "").strip()
    if not value or not _CHINESE_ONLY.match(value):
        return None
    if value in GENERIC_ROLE_WORDS or value in GENERIC_STANDALONE:
        return None
    for surname_len in (2, 1):
        if len(value) <= surname_len:
            continue
        surname, remainder = value[:surname_len], value[surname_len:]
        if surname_len == 2 and surname not in _COMPOUND_SURNAMES:
            continue
        if surname in _NICKNAME_PREFIXES:
            continue
        if remainder in GENERIC_ROLE_WORDS:
            return surname, remainder
    return None


def split_person_name(name: str) -> Optional[Tuple[str, str]]:
    """把完整中文人名拆成 (姓, 名)，泛称和"姓+身份词"返回 None。

    返回非 None 表示该名字应当姓和名都换。
    """
    value = str(name or "").strip()
    if not re.fullmatch(r"[一-龥]{2,4}", value):
        return None
    if value.startswith(_NICKNAME_PREFIXES):
        return None
    if is_generic_appellation(value) or split_surname_and_role(value):
        return None
    # 含泛指身份词的名字（如"张大人"）不按人名整体改。
    if any(word in value for word in GENERIC_ROLE_WORDS | GENERIC_STANDALONE):
        return None
    surname_len = 2 if value[:2] in _COMPOUND_SURNAMES else 1
    if len(value) <= surname_len:
        return None
    return value[:surname_len], value[surname_len:]


def split_confident_person_name(name: str) -> Optional[Tuple[str, str]]:
    """只在首字（或前两字）确实是姓氏时才拆成 (姓, 名)，否则返回 None。

    `split_person_name` 采取宽松策略：凡不在泛称词表里的 2-4 字中文都当人名，
    因为词表穷举不完、模型判断优先。但没有模型可依赖时（兜底换姓）必须反过来，
    宁可漏改也不能错改——"家长"→"裴长"、"提问者"→"谢问者"这种破坏语义的结果
    比不脱敏更糟。姓氏是封闭集合，用它做门槛可以挡掉绝大多数误判。
    """
    parts = split_person_name(name)
    if not parts:
        return None
    return parts if parts[0] in COMMON_SURNAMES else None
