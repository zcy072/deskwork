"""交付前的基础校验:文件能否按其格式打开、是否为空。

JSON 要求能解析;CSV 没有"非法"的标准写法,只查读不出来的情况(编码不是 UTF-8/GBK、含 NUL、
引号未闭合),不检查列数是否一致。

Office(docx/xlsx/pptx)只用标准库查结构:zip 完整、各 XML 部件能解析、关系(_rels)指向的内部部件存在、
部件里引用的关系 ID(r:id、r:embed 等,工作表/幻灯片/图片都靠它挂上)都有定义。
PDF 这里只查文件头和结尾标记;交付流程另在沙箱里跑 qpdf --check(check_pdf_in_sandbox),
后端不引入 PDF 解析依赖。只查结构,不判断内容是否符合任务要求。
校验的是交付源文件,失败时调用方不应动 output/ 里上一次的交付副本。
"""

import csv
import io
import json
import os
import posixpath
import shlex
import zipfile
import zlib
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote
from xml.etree import ElementTree

from app.sandbox.host_files import open_regular

# 扩展名 -> OOXML 主部件;缺少它说明不是对应格式的有效文件
_OOXML_MAIN = {
    ".docx": "word/document.xml",
    ".xlsx": "xl/workbook.xml",
    ".pptx": "ppt/presentation.xml",
}
_PDF_TAIL_BYTES = 2048
_MAX_XML_BYTES = 20 * 1024 * 1024  # 单个部件过大时跳过解析,只确认存在
_MAX_XML_TOTAL = 100 * 1024 * 1024  # 解析的 XML 总量上限,超出的部件只查存在
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_RELS_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_MAX_TEXT_BYTES = 20 * 1024 * 1024  # 文本类文件过大时跳过解析
# 常见中文表格导出用 GBK,UTF-8 BOM 由 utf-8-sig 吃掉
_CSV_ENCODINGS = ("utf-8-sig", "gb18030")


def check_deliverable(path: Path, root: Path | None = None) -> str | None:
    """返回问题描述;None 表示通过(或该格式没有可做的检查)。

    root 给出时从可信根逐段打开(源文件在 agent 可写目录里,中间目录可能被换成链接)。
    """
    try:
        opened = open_regular(root, path) if root is not None else path.open("rb")
        with opened as fh:
            return _check_open(fh, path.suffix.lower())
    except (OSError, ValueError) as exc:
        return f"无法读取文件:{exc}"


def _check_open(fh: BinaryIO, suffix: str) -> str | None:
    size = os.fstat(fh.fileno()).st_size
    if size == 0:
        return "文件是空的(0 字节)"
    if suffix in _OOXML_MAIN:
        return _check_ooxml(fh, suffix, _OOXML_MAIN[suffix])
    if suffix == ".pdf":
        return _check_pdf(fh, size)
    if suffix == ".json" and size <= _MAX_TEXT_BYTES:
        return _check_json(fh.read())
    if suffix == ".csv" and size <= _MAX_TEXT_BYTES:
        return _check_csv(fh.read())
    return None


def _check_json(raw: bytes) -> str | None:
    try:
        json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return "JSON 文件不是 UTF-8 编码"
    except ValueError as exc:
        return f"不是合法的 JSON:{exc}"
    return None


def _check_csv(raw: bytes) -> str | None:
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


def _check_ooxml(fh: BinaryIO, suffix: str, main_part: str) -> str | None:
    try:
        with zipfile.ZipFile(fh) as zf:
            names = set(zf.namelist())
            if "[Content_Types].xml" not in names or main_part not in names:
                return f"不是有效的 {suffix} 文件:缺少 {main_part}"
            bad = zf.testzip()
            if bad:
                return f"压缩包内的 {bad} 已损坏"
            return _check_parts(zf, names, main_part)
    except (zipfile.BadZipFile, zlib.error, EOFError):
        # 压缩数据损坏时 testzip/read 抛 zlib.error 或 EOFError,不是 BadZipFile
        return f"不是有效的 {suffix} 文件:不是完整的 zip 压缩包"
    except (OSError, zipfile.LargeZipFile, RuntimeError, NotImplementedError) as exc:
        return f"无法读取文件:{exc}"


def _rels_path(part: str) -> str:
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", f"{name}.rels")


def _check_parts(zf: zipfile.ZipFile, names: set[str], main_part: str) -> str | None:
    """解析各 XML 部件,检查关系目标存在、部件里引用的关系 ID 有定义。"""
    xml_parts = sorted(n for n in names if n.endswith((".xml", ".rels")))
    # 主部件和关系文件先解析,预算用完时它们不会被跳过
    xml_parts.sort(key=lambda n: (n != main_part and not n.endswith(".rels"), n))
    budget = _MAX_XML_TOTAL
    trees = {}
    for name in xml_parts:
        size = zf.getinfo(name).file_size
        if size > _MAX_XML_BYTES or size > budget:
            continue
        budget -= size
        try:
            trees[name] = ElementTree.fromstring(zf.read(name))
        except ElementTree.ParseError as exc:
            return f"{name} 不是合法的 XML:{exc}"

    rel_ids: dict[str, set[str]] = {}
    for rels_name, tree in trees.items():
        if not rels_name.endswith(".rels"):
            continue
        folder = posixpath.dirname(posixpath.dirname(rels_name))  # 去掉 _rels/
        ids = rel_ids.setdefault(rels_name, set())
        for rel in tree.iter(_RELS_NS):
            ids.add(rel.get("Id", ""))
            target = rel.get("Target", "")
            if rel.get("TargetMode") == "External" or not target or "://" in target:
                continue
            target = unquote(target.split("#", 1)[0])
            if not target:
                continue
            resolved = posixpath.normpath(
                target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target)
            )
            if resolved not in names:
                source = rels_name.replace("_rels/", "").removesuffix(".rels") or "包"
                return f"{source} 引用的部件 {resolved} 不存在"

    attr_prefix = "{" + _REL_NS + "}"
    for part, tree in trees.items():
        if part.endswith(".rels"):
            continue
        defined = rel_ids.get(_rels_path(part), set())
        for elem in tree.iter():
            for attr, value in elem.attrib.items():
                if attr.startswith(attr_prefix) and value and value not in defined:
                    return f"{part} 引用的关系 {value} 没有定义(对应的图片/工作表/幻灯片会丢失)"
    return None


def _check_pdf(f: BinaryIO, size: int) -> str | None:
    try:
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


async def check_pdf_in_sandbox(session, virtual_path: str) -> str | None:
    """在会话沙箱里跑 qpdf --check:返回 2 表示无法修复(交叉引用表坏了、页面树缺失等)。

    返回 3 只是警告(阅读器能自动修复),放行。沙箱不可用或超时也放行:不能因为检查工具故障而拒绝交付。
    调用方须已持有 workspace_lock(用不加锁的 _run_isolated)。
    """
    marker = "DESKWORK_PDF_BROKEN"
    cmd = (
        f"qpdf --check {shlex.quote(virtual_path)} >/dev/null 2>/tmp/qpdf.err; "
        f"[ $? -eq 2 ] && {{ echo {marker}; tail -n 2 /tmp/qpdf.err; exit 2; }}; exit 0"
    )
    try:
        output, is_error = await session._run_isolated(cmd, PDF_CHECK_TIMEOUT_SEC)
    except Exception:
        return None
    if not is_error or marker not in output:
        return None
    detail = (
        output.split(marker, 1)[1].strip().replace(virtual_path, posixpath.basename(virtual_path))
    )
    detail = " ".join(line.removeprefix("qpdf: ") for line in detail.splitlines())[:300]
    return f"PDF 已损坏,无法打开:{detail}" if detail else "PDF 已损坏,无法打开"


PDF_CHECK_TIMEOUT_SEC = 30
