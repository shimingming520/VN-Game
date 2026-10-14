"""
batch_translate.py
==================
基于 CTranslate2 + OPUS-MT 的高性能 CPU 批量翻译模块。

依赖安装：
    pip install ctranslate2 transformers sentencepiece

模型转换（INT8 量化，只需执行一次）：
    ct2-transformers-converter \
        --model Helsinki-NLP/opus-mt-zh-en \
        --output_dir ./models/opus-mt-zh-en-int8 \
        --quantization int8 \
        --force

支持的语言对（OPUS-MT 模型名规则：opus-mt-{src}-{tgt}）：
    zh -> en : Helsinki-NLP/opus-mt-zh-en
    en -> zh : Helsinki-NLP/opus-mt-en-zh
    ja -> en : Helsinki-NLP/opus-mt-ja-en
    en -> ja : Helsinki-NLP/opus-mt-en-jap
    （其他语言对按需添加到 MODEL_MAP）

用法（作为模块调用）：
    from services.translator.batch_translate import translate_text_batch
    result = translate_text_batch(text, src_lang="zh", tgt_lang="en")

用法（命令行独立运行）：
    python batch_translate.py --input novel_zh.txt --output novel_en.txt \
        --src zh --tgt en --batch_size 32
"""

import os
import re
import sys
import time
import logging
import argparse
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import List, Tuple
from services.story_planner.core.novel_text_encoding import read_novel_text

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

_MODEL_CONVERT_TIMEOUT_SECONDS = 30 * 60
_PROCESS_TERMINATE_GRACE_SECONDS = 10
_PROCESS_LOG_TAIL_BYTES = 1024 * 1024


def _positive_timeout_from_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _signal_process_group(process, *, force: bool) -> None:
    """Terminate the whole converter process group, not only its leader."""
    try:
        if os.name == "nt":
            if force:
                process.kill()
            else:
                process.terminate()
            return
        os.killpg(
            os.getpgid(process.pid),
            signal.SIGKILL if force else signal.SIGTERM,
        )
    except ProcessLookupError:
        return


def _read_process_log_tail(file_obj) -> str:
    file_obj.flush()
    size = file_obj.tell()
    file_obj.seek(max(0, size - _PROCESS_LOG_TAIL_BYTES))
    return file_obj.read().decode("utf-8", errors="backslashreplace")


def _run_managed_subprocess(
    cmd,
    *,
    timeout_seconds: int,
    terminate_grace_seconds: int = _PROCESS_TERMINATE_GRACE_SECONDS,
    popen_factory=subprocess.Popen,
):
    """Run a bounded child process and always reap it after TERM/KILL."""
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
        mode="w+b"
    ) as stderr_file:
        popen_kwargs = {
            "stdout": stdout_file,
            "stderr": stderr_file,
            "shell": False,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )
        else:
            popen_kwargs["start_new_session"] = True

        process = popen_factory(cmd, **popen_kwargs)
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _signal_process_group(process, force=False)
            try:
                process.wait(timeout=terminate_grace_seconds)
            except subprocess.TimeoutExpired:
                _signal_process_group(process, force=True)
                process.wait()
            stderr_tail = _read_process_log_tail(stderr_file)
            detail = f"；stderr_tail={stderr_tail}" if stderr_tail else ""
            raise TimeoutError(
                f"翻译模型转换超过 {timeout_seconds} 秒，已终止并回收子进程{detail}"
            ) from exc

        return subprocess.CompletedProcess(
            cmd,
            return_code,
            stdout=_read_process_log_tail(stdout_file),
            stderr=_read_process_log_tail(stderr_file),
        )

# ==================== 模型配置 ====================

# 模型存放根目录（优先读取环境变量，方便 Docker 挂载）
MODEL_BASE_DIR = Path(os.getenv("TRANSLATOR_MODEL_DIR", "/app/models"))

# OPUS-MT 模型映射：(src_lang, tgt_lang) -> HuggingFace 模型名
MODEL_MAP = {
    ("zh", "en"): "Helsinki-NLP/opus-mt-zh-en",
    ("en", "zh"): "Helsinki-NLP/opus-mt-en-zh",
    ("ja", "en"): "Helsinki-NLP/opus-mt-ja-en",
    ("en", "ja"): "Helsinki-NLP/opus-mt-en-jap",
    ("ko", "en"): "Helsinki-NLP/opus-mt-ko-en",
    ("en", "ko"): "Helsinki-NLP/opus-mt-en-ko",
    ("fr", "en"): "Helsinki-NLP/opus-mt-fr-en",
    ("en", "fr"): "Helsinki-NLP/opus-mt-en-fr",
    ("de", "en"): "Helsinki-NLP/opus-mt-de-en",
    ("en", "de"): "Helsinki-NLP/opus-mt-en-de",
    ("es", "en"): "Helsinki-NLP/opus-mt-es-en",
    ("en", "es"): "Helsinki-NLP/opus-mt-en-es",
}

# CPU 线程配置（根据服务器核心数调整）
INTER_THREADS = int(os.getenv("CT2_INTER_THREADS", "2"))   # 并发翻译批次数
INTRA_THREADS = int(os.getenv("CT2_INTRA_THREADS", "4"))   # 单批次内部并行线程数

# 单行最大字符数，超过则二次切分
MAX_LINE_CHARS = 400

# 默认批次大小
DEFAULT_BATCH_SIZE = 32


# ==================== 模型加载（懒加载，进程内缓存） ====================

_translator_cache = {}   # key: (src_lang, tgt_lang) -> ctranslate2.Translator
_tokenizer_cache = {}    # key: (src_lang, tgt_lang) -> transformers.AutoTokenizer


def _get_model_dir(src_lang: str, tgt_lang: str) -> Path:
    """返回 INT8 量化模型的本地目录路径"""
    model_name = MODEL_MAP.get((src_lang, tgt_lang))
    if not model_name:
        raise ValueError(f"不支持的语言对: {src_lang} -> {tgt_lang}。支持: {list(MODEL_MAP.keys())}")
    # 将 HuggingFace 模型名转为本地目录名（替换 / 为 -）
    dir_name = model_name.replace("/", "-") + "-int8"
    return MODEL_BASE_DIR / dir_name


def _load_translator(src_lang: str, tgt_lang: str):
    """
    加载 CTranslate2 Translator（INT8 量化，CPU 推理）。
    如果本地 INT8 模型不存在，自动从 HuggingFace 下载并转换。
    """
    import ctranslate2

    cache_key = (src_lang, tgt_lang)
    if cache_key in _translator_cache:
        return _translator_cache[cache_key]

    model_dir = _get_model_dir(src_lang, tgt_lang)

    if not model_dir.exists():
        logger.info(f"本地 INT8 模型不存在，开始自动转换: {model_dir}")
        _convert_model(src_lang, tgt_lang, model_dir)

    logger.info(f"加载 CTranslate2 模型: {model_dir}")
    translator = ctranslate2.Translator(
        str(model_dir),
        device="cpu",
        inter_threads=INTER_THREADS,
        intra_threads=INTRA_THREADS,
        compute_type="int8",
    )
    _translator_cache[cache_key] = translator
    return translator


def _load_tokenizer(src_lang: str, tgt_lang: str):
    """加载对应的 SentencePiece Tokenizer"""
    from transformers import AutoTokenizer

    cache_key = (src_lang, tgt_lang)
    if cache_key in _tokenizer_cache:
        return _tokenizer_cache[cache_key]

    model_name = MODEL_MAP[(src_lang, tgt_lang)]
    logger.info(f"加载 Tokenizer: {model_name}")
    # 优先从本地模型目录加载，避免容器内联网
    model_dir = _get_model_dir(src_lang, tgt_lang)
    tokenizer_path = str(model_dir) if model_dir.exists() else model_name
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=model_dir.exists())
    _tokenizer_cache[cache_key] = tokenizer
    return tokenizer


def _convert_model(src_lang: str, tgt_lang: str, output_dir: Path):
    """
    自动将 HuggingFace 模型转换为 CTranslate2 INT8 格式。
    等价于命令行：
        ct2-transformers-converter --model <model_name> --output_dir <dir> --quantization int8
    """
    model_name = MODEL_MAP[(src_lang, tgt_lang)]
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "ctranslate2.tools.transformers",
        "--model", model_name,
        "--output_dir", str(output_dir),
        "--quantization", "int8",
        "--force",
    ]
    logger.info(f"执行模型转换命令: {' '.join(cmd)}")
    result = _run_managed_subprocess(
        cmd,
        timeout_seconds=_positive_timeout_from_env(
            "STORY_PLANNER_TRANSLATION_MODEL_CONVERT_TIMEOUT",
            _MODEL_CONVERT_TIMEOUT_SECONDS,
        ),
    )
    if result.returncode != 0:
        raise RuntimeError(f"模型转换失败:\n{result.stderr}")
    logger.info(f"模型转换完成: {output_dir}")


# ==================== 文本预处理 ====================

def _split_long_line(line: str, max_chars: int = MAX_LINE_CHARS) -> List[str]:
    """
    将超长行按标点二次切分，防止翻译截断。
    优先在句号/问号/感叹号处切分，其次在逗号处切分。
    """
    if len(line) <= max_chars:
        return [line]

    # 按句末标点切分
    segments = re.split(r'(?<=[。！？.!?])', line)
    result = []
    current = ""
    for seg in segments:
        if len(current) + len(seg) <= max_chars:
            current += seg
        else:
            if current:
                result.append(current)
            # 如果单个 seg 仍然超长，强制按 max_chars 截断
            while len(seg) > max_chars:
                result.append(seg[:max_chars])
                seg = seg[max_chars:]
            current = seg
    if current:
        result.append(current)
    return [s for s in result if s.strip()]


def _preprocess_text(text: str) -> List[str]:
    """
    将原文切分为翻译单元列表。
    1. 按行读取，过滤空行
    2. 超长行二次切分
    """
    lines = text.splitlines()
    chunks = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        chunks.extend(_split_long_line(line))
    return chunks


# ==================== 核心翻译函数 ====================

def translate_batch_raw(
    sentences: List[str],
    src_lang: str,
    tgt_lang: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> List[str]:
    """
    对句子列表进行批量翻译，返回翻译结果列表。
    这是核心推理函数，可单独封装为 FastAPI 接口。

    Args:
        sentences: 待翻译句子列表
        src_lang: 源语言（zh/en/ja等）
        tgt_lang: 目标语言（zh/en/ja等）
        batch_size: 每批次句子数

    Returns:
        翻译结果列表，与输入一一对应
    """
    translator = _load_translator(src_lang, tgt_lang)
    tokenizer = _load_tokenizer(src_lang, tgt_lang)

    results = []
    total = len(sentences)
    processed = 0
    start_time = time.time()

    for batch_start in range(0, total, batch_size):
        batch = sentences[batch_start: batch_start + batch_size]

        try:
            # 分词：将每个句子转为 token 列表
            tokenized = [tokenizer.convert_ids_to_tokens(
                tokenizer.encode(s, add_special_tokens=True)
            ) for s in batch]

            # 批量翻译
            translations = translator.translate_batch(
                tokenized,
                beam_size=4,
                max_decoding_length=512,
            )

            # 解码：将 token 列表还原为字符串
            for trans in translations:
                tokens = trans.hypotheses[0]
                text = tokenizer.convert_tokens_to_string(tokens)
                results.append(text)

        except Exception as e:
            # 单批次失败：记录错误，用原文占位，继续处理
            logger.error(f"批次 [{batch_start}:{batch_start+len(batch)}] 翻译失败: {e}")
            results.extend(batch)  # 回退到原文

        processed += len(batch)
        elapsed = time.time() - start_time
        speed = processed / elapsed if elapsed > 0 else 0
        logger.info(f"进度: {processed}/{total} ({processed/total*100:.1f}%) | 速度: {speed:.1f} 句/秒")

    return results


def translate_text_batch(
    text: str,
    src_lang: str,
    tgt_lang: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> str:
    """
    翻译整段文本（字符串输入/输出），供 story_planner 直接调用。

    Args:
        text: 原文字符串
        src_lang: 源语言（zh/en/ja等）
        tgt_lang: 目标语言
        batch_size: 批次大小

    Returns:
        翻译后的文本字符串（段落间以换行分隔）
    """
    if src_lang == tgt_lang:
        return text

    logger.info(f"开始翻译: {src_lang} -> {tgt_lang}，原文长度: {len(text)} 字符")
    chunks = _preprocess_text(text)
    logger.info(f"预处理完成，共 {len(chunks)} 个翻译单元")

    translated_chunks = translate_batch_raw(chunks, src_lang, tgt_lang, batch_size)
    return "\n".join(translated_chunks)


# ==================== 文件翻译（命令行入口） ====================

def translate_file(
    input_path: str,
    output_path: str,
    src_lang: str,
    tgt_lang: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
):
    """
    翻译本地文件，结果实时追加写入输出文件（防止内存积压）。

    Args:
        input_path: 输入文件路径（如 novel_zh.txt）
        output_path: 输出文件路径（如 novel_en.txt）
        src_lang: 源语言
        tgt_lang: 目标语言
        batch_size: 批次大小
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    logger.info(f"读取文件: {input_path}")
    encoding_result = read_novel_text(input_path, convert_to_utf8=True)
    text = encoding_result.text
    if encoding_result.converted:
        logger.info(
            "输入文件已从 %s 转换为无 BOM UTF-8",
            encoding_result.source_encoding,
        )
    chunks = _preprocess_text(text)
    total = len(chunks)
    logger.info(f"共 {total} 个翻译单元，批次大小={batch_size}")

    translator = _load_translator(src_lang, tgt_lang)
    tokenizer = _load_tokenizer(src_lang, tgt_lang)

    start_time = time.time()
    processed = 0

    with open(output_path, "w", encoding="utf-8") as out_f:
        for batch_start in range(0, total, batch_size):
            batch = chunks[batch_start: batch_start + batch_size]

            try:
                tokenized = [tokenizer.convert_ids_to_tokens(
                    tokenizer.encode(s, add_special_tokens=True)
                ) for s in batch]

                translations = translator.translate_batch(
                    tokenized,
                    beam_size=4,
                    max_decoding_length=512,
                )

                for trans in translations:
                    tokens = trans.hypotheses[0]
                    translated_line = tokenizer.convert_tokens_to_string(tokens)
                    out_f.write(translated_line + "\n")

                out_f.flush()  # 实时写入，防止内存积压

            except Exception as e:
                logger.error(f"批次 [{batch_start}:{batch_start+len(batch)}] 翻译失败: {e}，写入原文")
                for line in batch:
                    out_f.write(line + "\n")
                out_f.flush()

            processed += len(batch)
            elapsed = time.time() - start_time
            chars_done = sum(len(c) for c in chunks[:processed])
            total_chars = sum(len(c) for c in chunks)
            speed_chars = chars_done / elapsed if elapsed > 0 else 0
            logger.info(
                f"批次 {batch_start//batch_size + 1} | "
                f"进度: {processed}/{total} ({processed/total*100:.1f}%) | "
                f"速度: {speed_chars:.0f} 字/秒 | "
                f"已用时: {elapsed:.0f}s"
            )

    total_time = time.time() - start_time
    logger.info(f"翻译完成！输出: {output_path}，总用时: {total_time:.1f}s")


# ==================== 命令行入口 ====================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CTranslate2 + OPUS-MT 批量翻译脚本")
    parser.add_argument("--input", required=True, help="输入文件路径（如 novel_zh.txt）")
    parser.add_argument("--output", required=True, help="输出文件路径（如 novel_en.txt）")
    parser.add_argument("--src", default="zh", help="源语言代码（默认: zh）")
    parser.add_argument("--tgt", default="en", help="目标语言代码（默认: en）")
    parser.add_argument("--batch_size", type=int, default=DEFAULT_BATCH_SIZE, help=f"批次大小（默认: {DEFAULT_BATCH_SIZE}）")
    args = parser.parse_args()

    translate_file(
        input_path=args.input,
        output_path=args.output,
        src_lang=args.src,
        tgt_lang=args.tgt,
        batch_size=args.batch_size,
    )
