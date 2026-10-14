"""作品级内容提示的检测、归一化和受控本地化。

本模块只负责生成供客户端消费的结构化元数据，不负责替代目标渠道的
内容政策审核。检测失败时返回可诊断的 ``review_required`` 结果，而不是
让长篇 Planner 任务因为一个附加提示步骤失败。
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence


CONTENT_WARNING_SCHEMA_VERSION = 1
CONTENT_WARNING_DETECTOR_VERSION = "work-content-warning-v1"
CONTENT_WARNING_PROMPT_VERSION = "work-content-warning-prompt-v1"
CONTENT_WARNING_LOCALIZATION_VERSION = "work-content-warning-l10n-v1"

ALLOWED_CATEGORY_CODES = frozenset(
    {
        "violence",
        "gore",
        "dismemberment",
        "cannibalism",
        "infection_or_contagion",
        "sexual_assault",
        "sexual_coercion",
        "incest",
        "minor_abuse",
        "minor_sexual_abuse",
        "forced_abortion",
        "fetal_or_corpse_imagery",
        "self_harm",
        "suicide",
        "substance_abuse",
    }
)
ALLOWED_PRESENCE = frozenset({"mentioned", "implied", "depicted", "graphic"})
ALLOWED_SEVERITIES = frozenset({"none", "low", "moderate", "high"})
ALLOWED_STATUSES = frozenset({"none", "detected", "review_required"})

_PRESENCE_RANK = {"mentioned": 1, "implied": 2, "depicted": 3, "graphic": 4}
_SEVERITY_RANK = {"none": 0, "low": 1, "moderate": 2, "high": 3}

_CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "dismemberment": (
        r"碎尸",
        r"肢解",
        r"分尸",
        r"断肢",
        r"剁成(?:肉泥|碎块)",
        r"砍下.{0,8}(?:头|手|腿|脚|四肢)",
        # English source text may reach chapter_branch after the legacy
        # translation step overwrites the task's working file.
        r"\bdismember(?:ed|ing|ment)?\b",
        r"\bmutilat(?:ed|ing|ion)\b",
        # Japanese source/translated text.
        r"切断",
        r"解体",
        r"バラバラ",
    ),
    "gore": (
        r"血肉模糊",
        r"血腥",
        r"内脏",
        r"肠子",
        r"残肢",
        r"尸块",
        r"鲜血四溅",
        r"\bgor(?:e|y)\b",
        r"\bblood(?:y|shed|stained)\b",
        r"\bblood\s+(?:covered|splattered|spattered)\b",
        r"血まみれ",
        r"流血",
        r"血飛沫",
        r"内臓",
    ),
    "cannibalism": (
        r"食人",
        r"吃人",
        r"人肉",
        r"吞食.{0,8}(?:尸体|尸块|人)",
        r"\bcannibal(?:ism|ize|ized|izing)?\b",
        r"\beat(?:ing)? human flesh\b",
        r"人を食べ",
        r"食人",
    ),
    "infection_or_contagion": (
        r"传染",
        r"感染",
        r"瘟疫",
        r"病毒",
        r"病原",
        r"发烧.{0,10}(?:隔离|传染|感染)",
        r"\b(?:infection|infected|contagion|contagious|epidemic|virus|quarantine)\b",
        r"感染",
        r"伝染",
        r"感染症",
        r"隔離",
    ),
    "sexual_assault": (
        r"强奸",
        r"强暴",
        r"性侵",
        r"性侵犯",
        r"猥亵",
        r"侵犯.{0,4}性",
        r"\b(?:sexual assault|sexually assaulted|rape|raped|molest(?:ed|ation)?)\b",
        r"性的暴行",
        r"性的虐待",
    ),
    "sexual_coercion": (
        r"强制性交",
        r"逼迫.{0,4}(?:发生关系|性交)",
        r"胁迫.{0,4}性",
        r"\b(?:sexual coercion|forced sex|forced sexual activity)\b",
        r"性的強要",
        r"無理やり.{0,6}(?:性交|性行為)",
    ),
    "incest": (
        r"乱伦",
        r"近亲(?:性|关系)",
        r"父女乱伦",
        r"兄妹乱伦",
        r"\bincest(?:uous)?\b",
        r"近親相姦",
    ),
    "minor_sexual_abuse": (
        r"未成年.{0,8}(?:性侵|强奸|猥亵|性行为)",
        r"儿童.{0,8}(?:性侵|强奸|猥亵)",
        r"幼女.{0,8}(?:性侵|强奸|猥亵)",
        r"\b(?:sexual abuse|rape|raped|molest(?:ed|ation)?)\b.{0,16}\b(?:minor|child|underage)\b",
        r"\b(?:minor|child|underage)\b.{0,16}\b(?:sexual abuse|rape|raped|molest(?:ed|ation)?)\b",
        r"未成年者.{0,8}(?:性的暴行|性的虐待)",
    ),
    "minor_abuse": (
        r"虐待儿童",
        r"虐待未成年人",
        r"殴打儿童",
        r"\b(?:abuse|beating|beaten|assault)\b.{0,16}\b(?:minor|child|underage)\b",
        r"未成年者への虐待",
    ),
    "forced_abortion": (
        r"强制流产",
        r"逼迫.{0,5}(?:流产|堕胎|打胎)",
        r"逼着.{0,5}(?:流产|堕胎|打胎)",
        r"\bforced abortion\b",
        r"\bcoerced abortion\b",
        r"強制中絶",
    ),
    "fetal_or_corpse_imagery": (
        r"胎儿",
        r"婴尸",
        r"尸体",
        r"尸骸",
        r"遗体",
        r"\b(?:corpse|corpses|dead body|human remains|remains of the dead|"
        r"bod(?:y|ies) of (?:the )?(?:victim|deceased|dead))\b",
        r"遺体",
        r"死体",
    ),
    "self_harm": (
        r"自残",
        r"自伤",
        r"割腕",
        r"划开手腕",
        r"\bself[- ]harm(?:ed|ing)?\b",
        r"\bcut(?:ting)? (?:their|his|her) wrist\b",
        r"自傷",
    ),
    "suicide": (
        r"自杀",
        r"寻死",
        r"跳楼自尽",
        r"上吊自尽",
        r"\bsuicid(?:e|al|ed)\b",
        r"自殺",
    ),
    "substance_abuse": (
        r"吸毒",
        r"注射毒品",
        r"过量服药",
        r"酗酒",
        r"毒瘾",
        r"\b(?:drug abuse|substance abuse|overdose|alcoholism|drunk)\b",
        r"薬物乱用",
        r"アルコール依存",
    ),
    "violence": (
        r"杀人",
        r"杀死",
        r"行凶",
        r"殴打",
        r"暴力",
        r"刺杀",
        r"砍伤",
        r"\b(?:violence|violent|killed|killing|murder|murdered|stabbed|beaten|assaulted)\b",
        r"暴力",
        r"殺人",
        r"刺殺",
        r"殴打",
    ),
}

_COMPILED_PATTERNS = {
    code: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
    for code, patterns in _CATEGORY_PATTERNS.items()
}

_MENTION_MARKERS = re.compile(
    r"(?:传闻|听说|据说|提到|提及|回忆|想起|梦见|梦中|幻觉|曾经|如果|据传|"
    r"\b(?:rumou?red|hearsay|mentioned|recalled|remembered|dream(?:ed|t)|"
    r"hallucination|once|if)\b|噂|聞いた|言及|回想|夢|幻覚|かつて)",
    re.IGNORECASE,
)
_IMPLIED_MARKERS = re.compile(
    r"(?:暗示|似乎|可能|仿佛|疑似|据称|\b(?:implied|seemed|seems|possibly|"
    r"apparently|suspected|alleged)\b|暗示|らしい|可能性|疑い)",
    re.IGNORECASE,
)

_STATIC_SEVERITY = {
    "cannibalism": "high",
    "dismemberment": "high",
    "sexual_assault": "high",
    "sexual_coercion": "high",
    "incest": "high",
    "minor_abuse": "high",
    "minor_sexual_abuse": "high",
    "forced_abortion": "high",
    "fetal_or_corpse_imagery": "high",
    "suicide": "high",
    "gore": "high",
    "infection_or_contagion": "moderate",
    "self_harm": "high",
    "violence": "moderate",
    "substance_abuse": "moderate",
}

_LOCALIZED_TEXT: dict[str, dict[str, tuple[str, str]]] = {
    "zh": {
        "violence": ("暴力", "作品中包含暴力或伤害行为。"),
        "gore": ("血腥内容", "作品中包含血腥或令人不适的画面描写。"),
        "dismemberment": ("肢解与碎尸", "作品中包含肢解、碎尸或残肢相关描写。"),
        "cannibalism": ("食人", "作品中包含食人相关情节。"),
        "infection_or_contagion": ("感染与传染", "作品中包含感染、传染病或隔离相关情节。"),
        "sexual_assault": ("性侵害", "作品中包含性侵害相关情节。"),
        "sexual_coercion": ("性胁迫", "作品中包含性胁迫或强制性行为相关情节。"),
        "incest": ("乱伦", "作品中包含乱伦或近亲关系相关情节。"),
        "minor_abuse": ("未成年人受虐", "作品中包含未成年人受虐相关情节。"),
        "minor_sexual_abuse": ("未成年人性侵害", "作品中包含未成年人性侵害相关情节。"),
        "forced_abortion": ("强制流产", "作品中包含强制流产或堕胎相关情节。"),
        "fetal_or_corpse_imagery": ("胎儿或尸体画面", "作品中包含胎儿、遗体或尸体相关描写。"),
        "self_harm": ("自残", "作品中包含自残或自伤相关情节。"),
        "suicide": ("自杀", "作品中包含自杀或自杀未遂相关情节。"),
        "substance_abuse": ("药物或酒精滥用", "作品中包含药物、毒品或酒精滥用相关情节。"),
    },
    "en": {
        "violence": ("Violence", "The work contains violence or physical harm."),
        "gore": ("Gore", "The work contains bloody or disturbing imagery."),
        "dismemberment": ("Dismemberment", "The work contains dismemberment or mutilation."),
        "cannibalism": ("Cannibalism", "The work contains cannibalism-related scenes."),
        "infection_or_contagion": ("Infection or contagion", "The work contains infection, contagion, or quarantine themes."),
        "sexual_assault": ("Sexual assault", "The work contains sexual-assault-related content."),
        "sexual_coercion": ("Sexual coercion", "The work contains sexual coercion or forced sexual activity."),
        "incest": ("Incest", "The work contains incest-related content."),
        "minor_abuse": ("Abuse of a minor", "The work contains abuse involving a minor."),
        "minor_sexual_abuse": ("Sexual abuse of a minor", "The work contains sexual abuse involving a minor."),
        "forced_abortion": ("Forced abortion", "The work contains forced-abortion or coerced-pregnancy content."),
        "fetal_or_corpse_imagery": ("Fetal or corpse imagery", "The work contains fetal, corpse, or remains imagery."),
        "self_harm": ("Self-harm", "The work contains self-harm-related content."),
        "suicide": ("Suicide", "The work contains suicide or attempted-suicide content."),
        "substance_abuse": ("Substance abuse", "The work contains drug or alcohol abuse-related content."),
    },
    "ja": {
        "violence": ("暴力", "暴力や身体的な加害を含みます。"),
        "gore": ("流血表現", "流血や不快感を与える描写を含みます。"),
        "dismemberment": ("身体の損壊", "身体の切断や損壊に関する描写を含みます。"),
        "cannibalism": ("食人", "食人に関する場面を含みます。"),
        "infection_or_contagion": ("感染・伝染", "感染症、伝染、隔離に関する内容を含みます。"),
        "sexual_assault": ("性的暴行", "性的暴行に関する内容を含みます。"),
        "sexual_coercion": ("性的強要", "性的な強要や同意のない行為を含みます。"),
        "incest": ("近親関係", "近親関係に関する内容を含みます。"),
        "minor_abuse": ("未成年者への虐待", "未成年者への虐待に関する内容を含みます。"),
        "minor_sexual_abuse": ("未成年者への性的被害", "未成年者への性的被害に関する内容を含みます。"),
        "forced_abortion": ("強制的な中絶", "強制的な中絶に関する内容を含みます。"),
        "fetal_or_corpse_imagery": ("胎児・遺体の描写", "胎児や遺体、遺骨に関する描写を含みます。"),
        "self_harm": ("自傷", "自傷行為に関する内容を含みます。"),
        "suicide": ("自殺", "自殺や自殺未遂に関する内容を含みます。"),
        "substance_abuse": ("薬物・アルコール乱用", "薬物やアルコールの乱用に関する内容を含みます。"),
    },
}


def _language_prefix(language: Any) -> str:
    value = str(language or "en-US").strip().lower().replace("_", "-")
    return value.split("-", 1)[0] or "en"


def _as_chapter_list(chapters: Sequence[str] | None, text: str) -> list[str]:
    values = [str(chapter or "") for chapter in (chapters or [])]
    values = [chapter for chapter in values if chapter.strip()]
    return values or [str(text or "")]


def _infer_presence(code: str, context: str) -> str:
    if _MENTION_MARKERS.search(context):
        return "mentioned"
    if _IMPLIED_MARKERS.search(context):
        return "implied"
    if code in {
        "gore",
        "dismemberment",
        "cannibalism",
        "minor_sexual_abuse",
        "fetal_or_corpse_imagery",
    }:
        return "graphic"
    return "depicted"


def _deterministic_categories(chapters: Sequence[str]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for chapter_number, chapter in enumerate(chapters, start=1):
        for code, patterns in _COMPILED_PATTERNS.items():
            for pattern in patterns:
                match = pattern.search(chapter)
                if not match:
                    continue
                start = max(0, match.start() - 32)
                end = min(len(chapter), match.end() + 32)
                presence = _infer_presence(code, chapter[start:end])
                candidate = found.setdefault(
                    code,
                    {
                        "code": code,
                        "presence": presence,
                        "severity": _STATIC_SEVERITY.get(code, "moderate"),
                        "confidence": 0.78,
                        "evidence_chapters": [],
                    },
                )
                if _PRESENCE_RANK[presence] > _PRESENCE_RANK[candidate["presence"]]:
                    candidate["presence"] = presence
                candidate["evidence_chapters"].append(chapter_number)
                break
    for item in found.values():
        item["evidence_chapters"] = sorted(set(item["evidence_chapters"]))
    return sorted(found.values(), key=lambda item: item["code"])


def _llm_prompt(
    *,
    chapter_summaries: Sequence[str],
    evidence: Sequence[dict[str, Any]],
) -> tuple[str, str]:
    summary_text = "\n\n".join(
        f"【第{index + 1}章】{str(summary)[:700]}"
        for index, summary in enumerate(chapter_summaries)
        if str(summary or "").strip()
    )[:30000]
    evidence_text = "\n".join(
        f"候选类别={item['code']}，章节={item['evidence_chapters']}，"
        f"出现方式={item['presence']}"
        for item in evidence
    )
    system = (
        "你是作品内容提示审核器。请依据章节摘要和候选信号，判断整部作品是否含有需要在入口提示的敏感内容。"
        "只输出一个 JSON 对象，不要 markdown，不要解释。不要因为传闻、梦境、回忆或否定句就把内容判为明确呈现；"
        "如果只是提及，presence 用 mentioned 或 implied；正文明确出现用 depicted；细节强烈时用 graphic。"
        "允许的 code：violence,gore,dismemberment,cannibalism,infection_or_contagion,"
        "sexual_assault,sexual_coercion,incest,minor_abuse,minor_sexual_abuse,forced_abortion,"
        "fetal_or_corpse_imagery,self_harm,suicide,substance_abuse。"
        '格式：{"categories":[{"code":"...","presence":"mentioned|implied|depicted|graphic",'
        '"severity":"low|moderate|high","confidence":0.0,"evidence_chapters":[1]}]}'
    )
    user = (
        f"章节摘要：\n{summary_text or '无章节摘要'}\n\n"
        f"规则召回候选：\n{evidence_text or '无候选信号'}"
    )
    return system, user


def _llm_categories(
    *,
    chapter_summaries: Sequence[str],
    evidence: Sequence[dict[str, Any]],
    provider: str | None,
) -> tuple[list[dict[str, Any]] | None, str]:
    if not str(provider or "").strip() or str(provider).strip().lower() in {
        "none",
        "disabled",
        "off",
    }:
        return None, "llm_disabled"
    try:
        from services.story_planner.core.llm_client import LLMClient

        system_prompt, user_prompt = _llm_prompt(
            chapter_summaries=chapter_summaries,
            evidence=evidence,
        )
        client = LLMClient(provider=provider, temperature=0.0, max_tokens=1600)
        response = client.chat_completion_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0,
            max_tokens=1600,
            timeout=120,
        )
        if not isinstance(response, Mapping):
            raise ValueError("内容提示 LLM 返回不是 JSON 对象")
        categories = response.get("categories")
        if not isinstance(categories, list):
            raise ValueError("内容提示 LLM 缺少 categories 数组")
        return [item for item in categories if isinstance(item, Mapping)], "ok"
    except Exception as exc:  # 附加提示不能阻塞长篇主流程
        return None, str(exc)


def _merge_categories(
    deterministic: Iterable[Mapping[str, Any]],
    llm_items: Iterable[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in list(deterministic) + list(llm_items or []):
        normalized = _normalize_category(item, chapter_count=None)
        if not normalized:
            continue
        code = normalized["code"]
        existing = merged.get(code)
        if existing is None:
            merged[code] = normalized
            continue
        if _PRESENCE_RANK[normalized["presence"]] > _PRESENCE_RANK[existing["presence"]]:
            existing["presence"] = normalized["presence"]
        if _SEVERITY_RANK[normalized["severity"]] > _SEVERITY_RANK[existing["severity"]]:
            existing["severity"] = normalized["severity"]
        existing["confidence"] = max(existing["confidence"], normalized["confidence"])
        existing["evidence_chapters"] = sorted(
            set(existing["evidence_chapters"]) | set(normalized["evidence_chapters"])
        )
    return sorted(merged.values(), key=lambda item: item["code"])


def _normalize_category(
    raw: Any,
    *,
    chapter_count: int | None,
) -> dict[str, Any] | None:
    if isinstance(raw, str):
        raw = {"code": raw}
    if not isinstance(raw, Mapping):
        return None
    aliases = {
        "blood": "gore",
        "corpse": "fetal_or_corpse_imagery",
        "contagion": "infection_or_contagion",
        "minor_sexual_abuse": "minor_sexual_abuse",
        "sexual_violence": "sexual_assault",
    }
    code = str(raw.get("code") or raw.get("category") or "").strip().lower()
    code = aliases.get(code, code)
    if code not in ALLOWED_CATEGORY_CODES:
        return None
    presence = str(raw.get("presence") or "depicted").strip().lower()
    presence_aliases = {"提及": "mentioned", "暗示": "implied", "明确呈现": "depicted", "强烈": "graphic"}
    presence = presence_aliases.get(presence, presence)
    if presence not in ALLOWED_PRESENCE:
        presence = "depicted"
    severity = str(raw.get("severity") or _STATIC_SEVERITY.get(code, "moderate")).strip().lower()
    if severity not in ALLOWED_SEVERITIES or severity == "none":
        severity = _STATIC_SEVERITY.get(code, "moderate")
    try:
        confidence = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    evidence_raw = raw.get("evidence_chapters")
    if evidence_raw is None:
        evidence_raw = raw.get("chapters")
    if isinstance(evidence_raw, (str, int, float)):
        evidence_raw = [evidence_raw]
    evidence: list[int] = []
    for value in evidence_raw or []:
        try:
            chapter = int(value)
        except (TypeError, ValueError):
            continue
        if chapter < 1 or (chapter_count is not None and chapter > chapter_count):
            continue
        evidence.append(chapter)
    return {
        "code": code,
        "presence": presence,
        "severity": severity,
        "confidence": round(confidence, 4),
        "evidence_chapters": sorted(set(evidence)),
    }


def _max_severity(categories: Sequence[Mapping[str, Any]]) -> str:
    if not categories:
        return "none"
    return max(
        (str(item.get("severity") or "moderate") for item in categories),
        key=lambda value: _SEVERITY_RANK.get(value, 0),
    )


def _revision(work: Mapping[str, Any]) -> str:
    detector = work.get("detector") if isinstance(work.get("detector"), Mapping) else {}
    stable = {
        "status": work.get("status"),
        "has_sensitive_content": work.get("has_sensitive_content"),
        "severity": work.get("severity"),
        "categories": work.get("categories", []),
        "display_policy": work.get("display_policy", {}),
        "detector": {
            "version": detector.get("version"),
            "method": detector.get("method"),
            "review_required": detector.get("review_required"),
        },
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def content_warning_ruleset_digest() -> str:
    """Return a digest of all deterministic detection inputs.

    The digest is persisted with the checkpoint so changing a pattern, marker,
    severity mapping, or prompt contract cannot accidentally reuse an old
    result merely because the source novel stayed the same.
    """

    payload = {
        "schema_version": CONTENT_WARNING_SCHEMA_VERSION,
        "detector_version": CONTENT_WARNING_DETECTOR_VERSION,
        "prompt_version": CONTENT_WARNING_PROMPT_VERSION,
        "patterns": _CATEGORY_PATTERNS,
        "mention_markers": _MENTION_MARKERS.pattern,
        "implied_markers": _IMPLIED_MARKERS.pattern,
        "static_severity": _STATIC_SEVERITY,
        "localized_version": CONTENT_WARNING_LOCALIZATION_VERSION,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _localized_payload(
    categories: Sequence[Mapping[str, Any]],
    *,
    language: str,
    show: bool,
) -> dict[str, Any]:
    prefix = _language_prefix(language)
    dictionary = _LOCALIZED_TEXT.get(prefix) or _LOCALIZED_TEXT["en"]
    if prefix == "zh":
        title = "内容提示"
        summary = "本作品包含可能令人不适的敏感内容。"
        acknowledge = "我已了解，继续进入作品"
        back = "返回"
    elif prefix == "ja":
        title = "コンテンツに関する注意"
        summary = "この作品には一部のプレイヤーに不快感を与える可能性のある内容が含まれます。"
        acknowledge = "内容を理解しました。続ける"
        back = "戻る"
    else:
        title = "Content Warning"
        summary = "This work contains sensitive themes that may be disturbing to some players."
        acknowledge = "I understand. Continue"
        back = "Return"
    items = []
    for category in categories:
        code = str(category.get("code") or "")
        label, description = dictionary.get(
            code,
            (code.replace("_", " ").title(), "This work contains potentially sensitive content."),
        )
        items.append({"code": code, "label": label, "description": description})
    if not show:
        # 无敏感内容时仍保留稳定字段，但不向 UI 暗示需要弹窗。
        summary = "" if prefix != "zh" else ""
    return {
        "title": title if show else "",
        "summary": summary,
        "items": items,
        "acknowledge_text": acknowledge,
        "return_text": back,
    }


def normalize_content_warnings(
    raw: Any,
    *,
    chapter_count: int | None = None,
) -> dict[str, Any]:
    """将任意候选结果归一化为完整的作品级提示协议。"""

    value = raw if isinstance(raw, Mapping) else {}
    if isinstance(value.get("content_warnings"), Mapping):
        value = value["content_warnings"]
    work_raw = value.get("work") if isinstance(value.get("work"), Mapping) else value
    categories_raw = work_raw.get("categories") if isinstance(work_raw, Mapping) else []
    categories: list[dict[str, Any]] = []
    for item in categories_raw or []:
        normalized = _normalize_category(item, chapter_count=chapter_count)
        if normalized:
            categories.append(normalized)
    categories = _merge_categories(categories, None)
    explicit_has = work_raw.get("has_sensitive_content") if isinstance(work_raw, Mapping) else None
    explicit_status = str(work_raw.get("status") or "").strip().lower() if isinstance(work_raw, Mapping) else ""
    if explicit_status not in ALLOWED_STATUSES:
        explicit_status = ""
    has_sensitive = bool(categories) if explicit_has is None else bool(explicit_has)
    if categories:
        has_sensitive = True
        status = "detected" if explicit_status != "review_required" else "review_required"
    elif explicit_status == "review_required":
        status = "review_required"
    elif has_sensitive:
        status = "review_required"
    else:
        status = "none"
    severity = _max_severity(categories)
    if status == "review_required" and severity == "none":
        severity = "moderate"
    show = bool(categories) or status == "review_required"
    policy_raw = work_raw.get("display_policy") if isinstance(work_raw, Mapping) else {}
    policy_raw = policy_raw if isinstance(policy_raw, Mapping) else {}
    display_policy = {
        "show_at_work_entry": bool(policy_raw.get("show_at_work_entry", show)) or show,
        # A work-level warning is an acknowledgement gate by contract.  The
        # consumer may dismiss the modal after acknowledging it, but generated
        # metadata must never silently advertise sensitive content without a
        # confirmation step.
        "requires_acknowledgement": True if show else False,
        "dismissible": bool(policy_raw.get("dismissible", True)),
        "allow_continue": bool(policy_raw.get("allow_continue", True)),
        "allow_return": bool(policy_raw.get("allow_return", True)),
        "default_collapsed": bool(policy_raw.get("default_collapsed", False)),
        "spoiler_level": str(policy_raw.get("spoiler_level") or "minimal"),
    }
    detector_raw = work_raw.get("detector") if isinstance(work_raw, Mapping) else {}
    detector_raw = detector_raw if isinstance(detector_raw, Mapping) else {}
    detector = {
        "version": str(detector_raw.get("version") or CONTENT_WARNING_DETECTOR_VERSION),
        "method": str(detector_raw.get("method") or "rules"),
        "review_required": bool(detector_raw.get("review_required", status == "review_required")),
    }
    # Optional operational diagnostics are intentionally kept separate from
    # policy fields.  They let operators distinguish a clean scan from a
    # detector outage without exposing raw exceptions to the player.
    if "verification_status" in detector_raw:
        detector["verification_status"] = str(
            detector_raw.get("verification_status") or "unknown"
        )
    if "failure_reason" in detector_raw:
        detector["failure_reason"] = str(detector_raw.get("failure_reason") or "")[:500]
    work = {
        "status": status,
        "has_sensitive_content": has_sensitive,
        "severity": severity,
        "categories": categories,
        "display_policy": display_policy,
        "localized": _localized_payload(categories, language="zh-CN", show=show),
        "detector": detector,
    }
    work["detector"]["revision"] = _revision(work)
    return {
        "schema_version": CONTENT_WARNING_SCHEMA_VERSION,
        "work": work,
        "chapters": dict(value.get("chapters") or {}) if isinstance(value.get("chapters"), Mapping) else {},
    }


def build_work_content_warnings(
    text: str,
    *,
    chapters: Sequence[str] | None = None,
    source_text: str | None = None,
    source_chapters: Sequence[str] | None = None,
    chapter_summaries: Sequence[str] | None = None,
    worldview: Mapping[str, Any] | None = None,
    provider: str | None = None,
    output_language: str | None = None,
) -> dict[str, Any]:
    """从原文召回并审核作品级敏感内容。

    规则召回保证低成本和可解释性；若提供 provider，再用一次有界 LLM
    复核语义和出现方式。任何远端失败都只降级为 ``review_required``。
    """

    del worldview, output_language
    # ``text``/``chapters`` are the working (possibly translated) content used
    # by the generation pipeline.  Translation can erase source-language
    # lexical signals, so callers may provide the canonical pre-translation
    # text separately.  Evidence chapter numbers always refer to the supplied
    # source chapter sequence and therefore remain stable across locales.
    chapter_values = _as_chapter_list(chapters, text)
    if source_chapters is not None or source_text is not None:
        detection_chapters = _as_chapter_list(
            source_chapters,
            source_text if source_text is not None else text,
        )
    else:
        detection_chapters = chapter_values
    try:
        deterministic = _deterministic_categories(detection_chapters)
    except Exception as exc:
        # This metadata is additive: a malformed detector rule, unexpected
        # source value, or future implementation error must never abort a
        # multi-hour Planner run.  Keep the warning visible and require a
        # human review instead of claiming the work is clean.
        reason = re.sub(r"\s+", " ", str(exc)).strip()[:500] or "detector_error"
        return normalize_content_warnings(
            {
                "work": {
                    "status": "review_required",
                    "has_sensitive_content": False,
                    "detector": {
                        "method": "rules",
                        "review_required": True,
                        "verification_status": "failed",
                        "failure_reason": reason,
                    },
                }
            },
            chapter_count=len(detection_chapters),
        )
    # Do not spend a gateway request on a clean work.  Verification is bounded
    # to candidate works and can be disabled in constrained environments.
    verify_enabled = str(__import__("os").getenv("STORY_PLANNER_CONTENT_WARNING_LLM_VERIFY", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if deterministic and verify_enabled:
        llm_items, llm_status = _llm_categories(
            chapter_summaries=[str(item or "") for item in (chapter_summaries or [])],
            evidence=deterministic,
            provider=provider,
        )
    else:
        llm_items, llm_status = None, "llm_disabled"
    categories = _merge_categories(deterministic, llm_items)
    review_required = False
    verification_status = "skipped" if llm_status == "llm_disabled" else "ok"
    verification_failure_reason = ""
    if llm_status not in {"ok", "llm_disabled"}:
        review_required = True
        verification_status = "failed"
        verification_failure_reason = re.sub(r"\s+", " ", str(llm_status)).strip()[:500]
    if any(float(item.get("confidence", 0.0)) < 0.55 for item in categories):
        review_required = True
        if verification_status == "ok":
            verification_status = "low_confidence"
    if review_required and not categories:
        raw = {
            "work": {
                "status": "review_required",
                "has_sensitive_content": False,
                "detector": {
                    "method": "rules+llm",
                    "review_required": True,
                    "verification_status": verification_status,
                    "failure_reason": verification_failure_reason,
                },
            }
        }
    else:
        raw = {
            "work": {
                "status": "review_required" if review_required else ("detected" if categories else "none"),
                "has_sensitive_content": bool(categories),
                "categories": categories,
                "detector": {
                    "method": "rules+llm" if llm_status != "llm_disabled" else "rules",
                    "review_required": review_required,
                    "verification_status": verification_status,
                    "failure_reason": verification_failure_reason,
                },
            }
        }
    return normalize_content_warnings(raw, chapter_count=len(detection_chapters))


def localize_content_warnings(
    warnings: Mapping[str, Any] | None,
    language: str,
) -> dict[str, Any]:
    """仅翻译受控的玩家提示文案，永不改动 category code 或证据。"""

    normalized = normalize_content_warnings(warnings)
    work = normalized["work"]
    work["localized"] = _localized_payload(
        work.get("categories", []),
        language=language,
        show=bool(work.get("display_policy", {}).get("show_at_work_entry")),
    )
    # revision 只基于机器字段，保证不同语言版本拥有相同确认指纹。
    work.setdefault("detector", {})["revision"] = _revision(work)
    return normalized


def _safe_schema_version(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def validate_content_warnings_schema(
    warnings: Any,
    *,
    require_localized: bool = True,
) -> list[str]:
    """返回结构问题列表；不抛异常，便于发布级代码降级处理。"""

    errors: list[str] = []
    if not isinstance(warnings, Mapping):
        return ["content_warnings 不是 JSON object"]
    try:
        schema_version = int(warnings.get("schema_version", 0) or 0)
    except (TypeError, ValueError):
        schema_version = 0
    if schema_version != CONTENT_WARNING_SCHEMA_VERSION:
        errors.append("content_warnings.schema_version 不受支持")
    work = warnings.get("work")
    if not isinstance(work, Mapping):
        return errors + ["content_warnings.work 缺失或不是 object"]
    status = str(work.get("status") or "")
    if status not in ALLOWED_STATUSES:
        errors.append("content_warnings.work.status 无效")
    if not isinstance(work.get("has_sensitive_content"), bool):
        errors.append("content_warnings.work.has_sensitive_content 必须为 bool")
    if str(work.get("severity") or "") not in ALLOWED_SEVERITIES:
        errors.append("content_warnings.work.severity 无效")
    categories = work.get("categories")
    if not isinstance(categories, list):
        errors.append("content_warnings.work.categories 必须为数组")
        categories = []
    category_codes: list[str] = []
    for index, item in enumerate(categories):
        if not isinstance(item, Mapping):
            errors.append(f"content_warnings.work.categories[{index}] 不是 object")
            continue
        code = str(item.get("code") or "")
        category_codes.append(code)
        if code not in ALLOWED_CATEGORY_CODES:
            errors.append(f"content_warnings.work.categories[{index}].code 无效")
        if str(item.get("presence") or "") not in ALLOWED_PRESENCE:
            errors.append(f"content_warnings.work.categories[{index}].presence 无效")
        if str(item.get("severity") or "") not in {"low", "moderate", "high"}:
            errors.append(f"content_warnings.work.categories[{index}].severity 无效")
        try:
            confidence = float(item.get("confidence"))
            if not 0 <= confidence <= 1:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"content_warnings.work.categories[{index}].confidence 无效")
        if not isinstance(item.get("evidence_chapters"), list):
            errors.append(f"content_warnings.work.categories[{index}].evidence_chapters 必须为数组")
        else:
            evidence = item.get("evidence_chapters")
            if any(
                isinstance(chapter, bool)
                or not isinstance(chapter, int)
                or chapter < 1
                for chapter in evidence
            ):
                errors.append(
                    f"content_warnings.work.categories[{index}].evidence_chapters 必须是正整数数组"
                )
            elif evidence != sorted(set(evidence)):
                errors.append(
                    f"content_warnings.work.categories[{index}].evidence_chapters 必须去重并升序"
                )
    if len(category_codes) != len(set(category_codes)):
        errors.append("content_warnings.work.categories 存在重复 code")
    if category_codes and not bool(work.get("has_sensitive_content")):
        errors.append("存在 categories 时 has_sensitive_content 必须为 true")
    if status == "none" and category_codes:
        errors.append("status=none 时 categories 必须为空")
    if category_codes and status not in {"detected", "review_required"}:
        errors.append("存在 categories 时 status 必须为 detected 或 review_required")
    if not category_codes and status == "detected":
        errors.append("status=detected 时 categories 不能为空")
    if not category_codes and bool(work.get("has_sensitive_content")):
        errors.append("无 categories 时 has_sensitive_content 必须为 false")
    category_max_severity = _max_severity(categories)
    work_severity = str(work.get("severity") or "none")
    if category_codes and _SEVERITY_RANK.get(work_severity, 0) < _SEVERITY_RANK.get(
        category_max_severity, 0
    ):
        errors.append("work.severity 不能低于 categories 中的最高严重度")
    if not category_codes and status == "none" and work_severity != "none":
        errors.append("无敏感类别且 status=none 时 severity 必须为 none")
    policy = work.get("display_policy")
    if not isinstance(policy, Mapping):
        errors.append("content_warnings.work.display_policy 缺失或不是 object")
    else:
        for field in ("show_at_work_entry", "requires_acknowledgement", "dismissible", "allow_continue", "allow_return"):
            if not isinstance(policy.get(field), bool):
                errors.append(f"content_warnings.work.display_policy.{field} 必须为 bool")
        if work.get("has_sensitive_content") and not policy.get("show_at_work_entry"):
            errors.append("存在敏感内容时必须在作品入口展示提示")
        if policy.get("show_at_work_entry") and not policy.get("requires_acknowledgement"):
            errors.append(
                "content_warnings.work.display_policy.requires_acknowledgement："
                "存在作品级提示时必须要求用户确认"
            )
    if require_localized:
        localized = work.get("localized")
        if not isinstance(localized, Mapping):
            errors.append("content_warnings.work.localized 缺失或不是 object")
        else:
            for field in ("title", "summary", "items", "acknowledge_text", "return_text"):
                if field not in localized:
                    errors.append(f"content_warnings.work.localized 缺少 {field}")
            items = localized.get("items")
            if not isinstance(items, list):
                errors.append("content_warnings.work.localized.items 必须为数组")
            else:
                localized_codes = [str(item.get("code") or "") for item in items if isinstance(item, Mapping)]
                if set(localized_codes) != set(category_codes):
                    errors.append("localized.items 与 categories code 不一致")
                for index, item in enumerate(items):
                    if not isinstance(item, Mapping):
                        errors.append(f"content_warnings.work.localized.items[{index}] 不是 object")
                        continue
                    if not str(item.get("code") or "").strip():
                        errors.append(
                            f"content_warnings.work.localized.items[{index}].code 不能为空"
                        )
                    for field in ("label", "description"):
                        if not isinstance(item.get(field), str) or not item.get(field).strip():
                            errors.append(
                                f"content_warnings.work.localized.items[{index}].{field} 必须为非空字符串"
                            )
            for field in ("title", "summary", "acknowledge_text", "return_text"):
                if field in localized and not isinstance(localized.get(field), str):
                    errors.append(f"content_warnings.work.localized.{field} 必须为字符串")
    detector = work.get("detector")
    if not isinstance(detector, Mapping):
        errors.append("content_warnings.work.detector 缺失或不是 object")
    else:
        for field in ("version", "method"):
            if not str(detector.get(field) or "").strip():
                errors.append(f"content_warnings.work.detector.{field} 不能为空")
        if not isinstance(detector.get("review_required"), bool):
            errors.append("content_warnings.work.detector.review_required 必须为 bool")
        if not str(detector.get("revision") or "").strip():
            errors.append("content_warnings.work.detector.revision 不能为空")
    chapters = warnings.get("chapters")
    if not isinstance(chapters, Mapping):
        errors.append("content_warnings.chapters 必须为 object")
    return errors


def content_warnings_payload_digest(warnings: Any) -> str:
    """Return the canonical digest used by the warning checkpoint contract."""

    encoded = json.dumps(
        warnings,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def is_reusable_content_warnings_checkpoint(
    checkpoint: Any,
    *,
    input_digest: str,
    detector_version: str = CONTENT_WARNING_DETECTOR_VERSION,
    ruleset_digest: str | None = None,
) -> bool:
    """Return whether a persisted warning result is complete and untampered.

    A matching input digest alone is insufficient: a truncated or manually
    edited output must be regenerated.  This mirrors the stronger checkpoint
    rules used by the rest of ``chapter_branch`` while remaining independent of
    the ordered pipeline-step version.
    """

    if not isinstance(checkpoint, Mapping):
        return False
    output = checkpoint.get("output")
    if (
        _safe_schema_version(checkpoint.get("schema_version"))
        != CONTENT_WARNING_SCHEMA_VERSION
        or str(checkpoint.get("input_digest") or "") != str(input_digest or "")
        or str(checkpoint.get("detector_version") or "") != str(detector_version or "")
        or (
            ruleset_digest is not None
            and str(checkpoint.get("ruleset_digest") or "")
            != str(ruleset_digest or "")
        )
        or not isinstance(output, Mapping)
        or str(checkpoint.get("output_digest") or "")
        != content_warnings_payload_digest(output)
        or validate_content_warnings_schema(output, require_localized=True)
    ):
        return False
    output_detector = output.get("work", {}).get("detector", {})
    return (
        isinstance(output_detector, Mapping)
        and str(output_detector.get("version") or "") == str(detector_version or "")
        and str(output_detector.get("revision") or "") == _revision(
            output.get("work", {})
        )
    )


__all__ = [
    "ALLOWED_CATEGORY_CODES",
    "CONTENT_WARNING_DETECTOR_VERSION",
    "CONTENT_WARNING_LOCALIZATION_VERSION",
    "CONTENT_WARNING_PROMPT_VERSION",
    "CONTENT_WARNING_SCHEMA_VERSION",
    "build_work_content_warnings",
    "content_warnings_payload_digest",
    "content_warning_ruleset_digest",
    "is_reusable_content_warnings_checkpoint",
    "localize_content_warnings",
    "normalize_content_warnings",
    "validate_content_warnings_schema",
]
