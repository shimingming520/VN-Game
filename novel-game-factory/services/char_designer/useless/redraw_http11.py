#!/usr/bin/env python3
"""redraw_character 的 HTTP/1.1 包装入口。

背景：本环境的 requests 底层是 urllib3-future，会自动协商 HTTP/2；而图生图
(/v1/images/edits) 的 multipart 上传在上游 HTTP/2 网关会被 RST_STREAM(0x1)
在约 60s 掐断，导致重绘每次都失败。文生图不受影响。此包装脚本在导入 redraw
之前，全局给 requests 的连接池禁用 h2/h3，强制走 HTTP/1.1，然后把命令行参数
原样透传给 redraw_character.main()。

使用方式
--------
建议在项目根目录执行本包装入口（不要直接执行 redraw_character.py）：

    cd /Users/slaine/Documents/gitlab/juzhen/SJ_novel
    python novel-game-factory/services/char_designer/useless/redraw_http11.py \\
        --task-id 9e99c7c7944a \\
        --char 余淮 \\
        --mode all_from_anchor \\
        --force

``--char`` 可以传中文角色名，也可以传角色的英文 ``speaker_id``，例如：

    --char YuHuai

``all_from_anchor`` 是“从人物 Anchor 重新生成全部内容”的模式；如果目录中已有图，
请加 ``--force`` 才会从 Anchor 重新开始：

* 自动从任务本地目录或 OSS 对应的 ``style_anchors/character_anchor.png`` 获取 Anchor；
* 全部步骤均使用图生图，连基础组合的第一张 calm 图也不会退回文生图；
* 先生成基础组合，再生成全部 ``active`` 变体；
* 按每个变体在 ``character_prompts.emotions`` 中配置的情绪生成对应情绪图；
* 所有 calm 和情绪 LS 生成完成后，按 ``character_prompts.height`` 在人物上方追加透明画布；
* 对每张图生成请求的景别（LS/MS/CS）；LS 是模型生成源图，MS/CS 从 LS 裁切派生；
* 输出到 ``storage/outputs/<task_id>/images/characters/``；使用 ``--force`` 时同名文件会被覆盖。

默认会复用已有且有效的 calm、情绪和 MS/CS 图片，只补生成缺失文件；加上
``--force`` 后会忽略已有图片，重新图生图/裁切并覆盖：

    python novel-game-factory/services/char_designer/useless/redraw_http11.py \\
        --task-id <task_id> --char YuHuai --mode all_from_anchor --force

运行过程中会持续在终端打印当前阶段，例如基础 calm、某个变体 calm、某个情绪
以及 LS/MS/CS 裁切；看到 ``已存在，跳过`` 表示该文件被复用，看到 ``完成`` 表示
该步骤已经写入输出目录。

常用参数示例：

# 只生成远景，不派生 MS/CS
python novel-game-factory/services/char_designer/useless/redraw_http11.py \\
        --task-id <task_id> --char YuHuai --mode all_from_anchor --shots LS

# 明确生成三种景别（默认值）
    python novel-game-factory/services/char_designer/useless/redraw_http11.py \\
        --task-id <task_id> --char YuHuai --mode all_from_anchor --shots LS,MS,CS

    # 指定模型参数
python novel-game-factory/services/char_designer/useless/redraw_http11.py --task-id <task_id> --char YuHuai --mode all_from_anchor \\
        --model gpt-image-2 --quality high --timeout 300 --force

也支持 ``redraw_character.py`` 的其它模式，例如 ``calm_only``、
``all_except_base_calm``、``variant_emotions`` 和 ``emotions_only``；所有命令行
参数都会原样透传给核心实现。

运行前请确认项目 ``.env`` 至少配置：

    GPT_IMAGE_API_KEY=...
    GPT_IMAGE_BASE_URL=https://ai.farmmx.com

本文件会先强制 requests 使用 HTTP/1.1，再调用
``services.char_designer.core.redraw_character.main``，用于规避图生图 multipart
请求在 HTTP/2 下被上游重置的问题。

"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

# 加载 .env（GPT_IMAGE_API_KEY / GPT_IMAGE_BASE_URL 等）
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass


def _force_http11() -> None:
    """给 requests 的全局连接池禁用 HTTP/2 与 HTTP/3。

    仅在 urllib3-future 上生效；标准 urllib3 本就只有 HTTP/1.1，import 失败即跳过。
    """
    import requests
    from requests.adapters import HTTPAdapter

    try:
        from urllib3 import HttpVersion, PoolManager
    except Exception:
        print(
            "[redraw_http11] 未检测到 urllib3-future 的 HttpVersion，沿用默认 HTTP/1.1",
            file=sys.stderr,
            flush=True,
        )
        return

    disabled = {HttpVersion.h2, HttpVersion.h3}

    class _Http11Adapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            pool_kwargs["disabled_svn"] = disabled
            self.poolmanager = PoolManager(
                num_pools=connections,
                maxsize=maxsize,
                block=block,
                **pool_kwargs,
            )

    # requests.post/get 等每次新建 Session，默认 mount 标准 HTTPAdapter。
    # 直接替换默认 adapter 类，令后续所有 Session 都禁用 h2/h3。
    original_init = requests.sessions.Session.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        adapter = _Http11Adapter()
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    requests.sessions.Session.__init__ = patched_init
    print(
        "[redraw_http11] 已全局禁用 HTTP/2/3，requests 强制走 HTTP/1.1",
        file=sys.stderr,
        flush=True,
    )


def main() -> None:
    _force_http11()
    # 透传 sys.argv 给真正的 redraw 入口
    from services.char_designer.core.redraw_character import main as redraw_main

    redraw_main()


if __name__ == "__main__":
    main()
