"""素材名翻译:LLM(阿里云百炼 DashScope)+ 数据库缓存。

用途:BGM / 音效等音频文件名(如 bgm_daily_wedding_prep_guzheng_1-14.wav)
翻成中文可读名,前台展示更清晰。图片不需要(能直观看到)。

策略:
  1. 先查 asset_name_translation 缓存表,命中直接返回;
  2. 未命中的批量丢给 LLM 一次翻完,结果写回缓存;
  3. 未配置 DASHSCOPE_API_KEY 或调用失败 → 该批返回空,前端回落本地词典。
"""
import json
import re

import requests
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from auth.deps import get_current_user
from common.config import DASHSCOPE_API_KEY, DASHSCOPE_BASE_URL, TRANSLATE_MODEL
from common.models import AssetNameTranslation, PlatformUser, engine

router = APIRouter(prefix="/api/v1", tags=["translate"])

# 单次请求最多翻译多少条,避免超长 prompt
MAX_BATCH = 60

_PROMPT = """你是游戏素材命名翻译助手。下面是一批游戏音频素材的文件名(BGM 或音效),
请把每个文件名翻译成简洁的中文展示名,让用户一眼看懂这段音频表达什么。

规则:
1. 去掉 bgm_/sfx_/amb_ 等前缀,去掉末尾的数字编号或区间(如 _25、_45-80)。
2. 只输出内容含义,不要保留下划线和英文。
3. BGM 若含情绪词(daily/suspense/battle/sorrow/horror/tender/tragedy/climax/crisis)
   译为"情绪·内容"格式;若含乐器(guzheng/erhu/dizi/pipa/taiko/strings)放在末尾括号里。
4. 音效直接译成动作短语,如"门吱呀开启""筷子落在瓷器上"。
5. 保持简短,每条不超过 14 个汉字。
6. 原文可能是任意语言,一律翻成中文。

严格只输出一个 JSON 对象,key 为原始文件名,value 为中文译名,不要任何解释或代码块标记。

文件名列表:
{names}"""


class TranslateIn(BaseModel):
    names: list[str]


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _llm_translate(names: list[str]) -> dict:
    """调 DashScope(OpenAI 兼容模式)批量翻译,失败返回空 dict(由调用方回落)。"""
    if not DASHSCOPE_API_KEY or not names:
        return {}
    prompt = _PROMPT.format(names="\n".join(names))
    try:
        resp = requests.post(
            f"{DASHSCOPE_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": TRANSLATE_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(_strip_code_fence(content))
        # 只保留请求过的 key,值必须是非空字符串
        return {
            k: str(v).strip()
            for k, v in data.items()
            if k in set(names) and str(v).strip()
        }
    except Exception as e:  # 网络/解析/额度问题都不阻断业务
        print(f"⚠️  素材名翻译失败(回落本地词典): {e}")
        return {}


@router.post("/translate/asset-names")
def translate_asset_names(
    body: TranslateIn,
    user: PlatformUser = Depends(get_current_user),
):
    """批量翻译素材文件名 → 中文展示名。返回 {原名: 译名},缺失的键前端自行回落。"""
    names = [n for n in {n.strip() for n in body.names if n and n.strip()}]
    if not names:
        return {}

    result: dict = {}
    with Session(engine) as session:
        rows = session.exec(
            select(AssetNameTranslation).where(AssetNameTranslation.source.in_(names))
        ).all()
        for r in rows:
            if r.translated:
                result[r.source] = r.translated

        missing = [n for n in names if n not in result][:MAX_BATCH]
        if missing:
            translated = _llm_translate(missing)
            for src, zh in translated.items():
                exists = session.exec(
                    select(AssetNameTranslation).where(AssetNameTranslation.source == src)
                ).first()
                if exists is None:
                    session.add(AssetNameTranslation(source=src, translated=zh))
                else:
                    exists.translated = zh
                    session.add(exists)
                result[src] = zh
            if translated:
                session.commit()

    return result
