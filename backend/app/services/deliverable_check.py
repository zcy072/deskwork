"""交付前的基础校验:文件能否按其格式打开、是否为空。

JSON 要求能解析;CSV 没有"非法"的标准写法,只查读不出来的情况(编码不是 UTF-8/GBK、含 NUL、
引号未闭合),不检查列数是否一致。

只用标准库做结构检查(不引入 Office 解析依赖),不判断内容是否符合任务要求。
校验的是交付源文件,失败时调用方不应动 output/ 里上一次的交付副本。
"""

import csv
import io
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree

# 扩展名 -> OOXML 主部件;缺少它说明不是对应格式的有效文件
_OOXML_MAIN = {
    ".docx": "word/document.xml",
    ".xlsx": "xl/workbook.xml",
    ".pptx": "ppt/presentation.xml",
}
_PDF_TAIL_BYTES = 2048
_MAX_XML_BYTES = 20 * 1024 * 1024  # 主部件过大时跳过解析,只确认存在
_MAX_TEXT_BYTES = 20 * 1024 * 1024  # 文本类文件过大时跳过解析
# 常见中文表格导出用 GBK,UTF-8 BOM 由 utf-8-sig 吃掉
_CSV_ENCODINGS = ("utf-8-sig", "gb18030")


def check_deliverable(path: Path) -> str | None:
    """返回问题描述;None 表示通过(或该格式没有可做的检查)。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"无法读取文件:{exc}"
    if size == 0:
        return "文件是空的(0 字节)"
    suffix = path.suffix.lower()
    if suffix in _OOXML_MAIN:
        return _check_ooxml(path, _OOXML_MAIN[suffix])
    if suffix == ".pdf":
        return _check_pdf(path, size)
    if suffix == ".json" and size <= _MAX_TEXT_BYTES:
        return _check_json(path)
    if suffix == ".csv" and size <= _MAX_TEXT_BYTES:
        return _check_csv(path)
    return None


def _read_bytes(path: Path) -> tuple[bytes | None, str | None]:
    try:
        return path.read_bytes(), None
    except OSError as exc:
        return None, f"无法读取文件:{exc}"


def _check_json(path: Path) -> str | None:
    raw, err = _read_bytes(path)
    if raw is None:
        return err
    try:
        json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return "JSON 文件不是 UTF-8 编码"
    except ValueError as exc:
        return f"不是合法的 JSON:{exc}"
    return None


def _check_csv(path: Path) -> str | None:
    raw, err = _read_bytes(path)
    if raw is None:
        return err
    if b"\x00" in raw:
        return "CSV 含有 NUL 字节,可能是二进制文件或 UTF-16 编码"
    text = None
    for encoding in _CSV_ENCODINGS:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return "CSV 的编码无法识别(不是 UTF-8 或 GBK)"
    try:
        for _ in csv.reader(io.StringIO(text, newline=""), strict=True):
            pass
    except csv.Error as exc:
        return f"CSV 格式有误:{exc}"
    return None


def _check_ooxml(path: Path, main_part: str) -> str | None:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "[Content_Types].xml" not in names or main_part not in names:
                return f"不是有效的 {path.suffix} 文件:缺少 {main_part}"
            info = zf.getinfo(main_part)
            if info.file_size <= _MAX_XML_BYTES:
                ElementTree.fromstring(zf.read(main_part))
            bad = zf.testzip()
            if bad:
                return f"压缩包内的 {bad} 已损坏"
    except zipfile.BadZipFile:
        return f"不是有效的 {path.suffix} 文件:不是完整的 zip 压缩包"
    except ElementTree.ParseError as exc:
        return f"{main_part} 不是合法的 XML:{exc}"
    except (OSError, zipfile.LargeZipFile) as exc:
        return f"无法读取文件:{exc}"
    return None


def _check_pdf(path: Path, size: int) -> str | None:
    try:
        with path.open("rb") as f:
            head = f.read(8)
            f.seek(max(0, size - _PDF_TAIL_BYTES))
            tail = f.read()
    except OSError as exc:
        return f"无法读取文件:{exc}"
    if not head.startswith(b"%PDF-"):
        return "不是有效的 PDF:缺少 %PDF- 文件头"
    if b"%%EOF" not in tail:
        return "PDF 不完整:缺少文件结尾标记 %%EOF(可能写入中断)"
    return None
