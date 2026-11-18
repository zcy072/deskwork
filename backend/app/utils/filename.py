import re
import unicodedata
from pathlib import Path

MAX_NAME_BYTES = 200
MAX_SUFFIX_CHARS = 16
# 这些字符在 shell 和 Windows 文件名里有特殊含义;模型拼命令时容易漏引号,统一换掉
_UNSAFE_CHARS = re.compile(r"[\s()<>:\"|?*$`;&!'\[\]{}#~^%]+")


def _visible(text: str) -> str:
    """NFC 归一化并去掉控制字符和格式字符(含 NUL、ANSI 转义、RLO 等双向覆盖符)。"""
    text = unicodedata.normalize("NFC", text)
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))


def _truncate_utf8(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def normalize_upload_filename(raw: str) -> str:
    """把上传文件名变成安全的单层文件名:只取最后一段,去掉不可见字符和 shell 特殊字符,
    不以点或连字符开头(避免隐藏文件和被当成命令选项),UTF-8 不超过 MAX_NAME_BYTES 字节。
    结果仍需在命令里加引号使用,这里只是降低出错概率。"""
    name = _visible(raw).replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem.strip("._- "):
        stem, suffix = name, ""
    suffix = _UNSAFE_CHARS.sub("", suffix)[:MAX_SUFFIX_CHARS]
    suffix = f".{suffix}" if suffix else ""

    stem = _UNSAFE_CHARS.sub("_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_").lstrip(".-")
    stem = _truncate_utf8(stem, MAX_NAME_BYTES - len(suffix.encode("utf-8"))).rstrip("_.")
    return f"{stem or 'upload'}{suffix}"


def unique_upload_name(
    uploads_dir: Path,
    filename: str,
    reserved: set[str] | None = None,
) -> str:
    """Return a filename that does not collide with existing or reserved names."""
    taken = reserved or set()
    if filename not in taken and not (uploads_dir / filename).exists():
        return filename

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 2
    while True:
        candidate = f"{stem}_{counter}{suffix}"
        if candidate not in taken and not (uploads_dir / candidate).exists():
            return candidate
        counter += 1
