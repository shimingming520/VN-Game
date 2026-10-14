"""Strict novel text encoding detection and UTF-8 normalization."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile


class NovelEncodingError(ValueError):
    """Raised when a novel file cannot safely be treated as text."""


@dataclass(frozen=True)
class NovelTextReadResult:
    text: str
    source_encoding: str
    converted: bool
    byte_count: int


_BOMS = (
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xfe\xff", "utf-16"),
    (b"\xff\xfe", "utf-16"),
)
_ALLOWED_CONTROLS = {"\t", "\n", "\r", "\f"}


def _validate_text(text: str) -> None:
    if not text:
        return
    suspicious = sum(
        1
        for char in text
        if (ord(char) < 32 and char not in _ALLOWED_CONTROLS)
        or 0x7F <= ord(char) <= 0x9F
    )
    if suspicious / len(text) > 0.01:
        raise NovelEncodingError("文件包含过多控制字符，疑似二进制文件")


def detect_novel_encoding(raw: bytes) -> str:
    """Return a strict decoder name for supported novel text bytes."""
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return encoding

    if raw and raw.count(b"\x00") / len(raw) > 0.001:
        raise NovelEncodingError("文件包含空字节且没有文本 BOM，疑似二进制文件")

    try:
        raw.decode("utf-8", errors="strict")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    try:
        raw.decode("gb18030", errors="strict")
        return "gb18030"
    except UnicodeDecodeError as exc:
        raise NovelEncodingError(
            "无法识别小说编码，仅支持 UTF-8、UTF-8 BOM、GB18030/GBK 和带 BOM 的 UTF-16/32"
        ) from exc


def _atomic_write_utf8(path: Path, text: str) -> None:
    file_mode = path.stat().st_mode
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    destination_was_writable = bool(file_mode & 0o200)
    try:
        with os.fdopen(descriptor, "wb") as temp_file:
            temp_file.write(text.encode("utf-8"))
            temp_file.flush()
            os.fsync(temp_file.fileno())
        # Windows refuses replacing a read-only destination. Temporarily make
        # the destination writable, then restore its original mode afterward.
        if not destination_was_writable:
            os.chmod(path, file_mode | 0o200)
        os.chmod(temp_path, file_mode | 0o200)
        os.replace(temp_path, path)
        if not destination_was_writable:
            os.chmod(path, file_mode)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.chmod(temp_path, temp_path.stat().st_mode | 0o200)
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        if not destination_was_writable:
            try:
                os.chmod(path, file_mode)
            except OSError:
                pass
        raise


def read_novel_text(
    path: Path | str,
    *,
    convert_to_utf8: bool = True,
) -> NovelTextReadResult:
    """Read a novel strictly and optionally normalize its file to BOM-less UTF-8."""
    novel_path = Path(path)
    try:
        raw = novel_path.read_bytes()
    except OSError as exc:
        raise NovelEncodingError(f"无法读取小说文件: {novel_path}") from exc

    source_encoding = detect_novel_encoding(raw)
    try:
        text = raw.decode(source_encoding, errors="strict")
    except UnicodeDecodeError as exc:
        raise NovelEncodingError(
            f"小说文件无法按检测到的编码 {source_encoding} 严格解码"
        ) from exc
    _validate_text(text)

    should_convert = convert_to_utf8 and source_encoding != "utf-8"
    if should_convert:
        try:
            _atomic_write_utf8(novel_path, text)
        except OSError as exc:
            raise NovelEncodingError(f"小说文件转换为 UTF-8 失败: {novel_path}") from exc

    return NovelTextReadResult(
        text=text,
        source_encoding=source_encoding,
        converted=should_convert,
        byte_count=len(raw),
    )
