"""交付前基础校验:空文件、损坏的 Office/PDF 被拦下,失败不动上次交付副本,连续失败也不交付。"""

import asyncio
import zipfile
from types import SimpleNamespace

import pytest

from app.sandbox.errors import ContainerFileSystemError
from app.sandbox.session_paths import SessionPaths
from app.services.deliverable_check import check_deliverable
from app.tools import executor


def _ooxml(path, main, xml="<root/>"):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(main, xml)


def test_empty_file(tmp_path):
    f = tmp_path / "a.docx"
    f.write_bytes(b"")
    assert "空" in check_deliverable(f)


def test_valid_and_broken_ooxml(tmp_path):
    ok = tmp_path / "a.docx"
    _ooxml(ok, "word/document.xml")
    assert check_deliverable(ok) is None

    notzip = tmp_path / "b.docx"
    notzip.write_bytes(b"not a zip")
    assert "zip" in check_deliverable(notzip)

    wrong = tmp_path / "c.xlsx"
    _ooxml(wrong, "word/document.xml")  # docx 内容改了扩展名
    assert "xl/workbook.xml" in check_deliverable(wrong)

    badxml = tmp_path / "d.pptx"
    _ooxml(badxml, "ppt/presentation.xml", "<a><b></a>")
    assert "XML" in check_deliverable(badxml)


def test_pdf(tmp_path):
    ok = tmp_path / "a.pdf"
    ok.write_bytes(b"%PDF-1.7\n...\n%%EOF\n")
    assert check_deliverable(ok) is None
    truncated = tmp_path / "b.pdf"
    truncated.write_bytes(b"%PDF-1.7\n" + b"x" * 5000)
    assert "EOF" in check_deliverable(truncated)
    nothdr = tmp_path / "c.pdf"
    nothdr.write_bytes(b"hello %%EOF")
    assert "文件头" in check_deliverable(nothdr)


def test_other_formats_only_checked_for_emptiness(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hi")
    assert check_deliverable(f) is None


def test_json(tmp_path):
    ok = tmp_path / "a.json"
    ok.write_text('{"a": [1, 2]}', encoding="utf-8")
    assert check_deliverable(ok) is None
    bom = tmp_path / "b.json"
    bom.write_bytes(b"\xef\xbb\xbf[1]")
    assert check_deliverable(bom) is None
    bad = tmp_path / "c.json"
    bad.write_text('{"a": 1,', encoding="utf-8")
    assert "不是合法的 JSON" in check_deliverable(bad)
    gbk = tmp_path / "d.json"
    gbk.write_bytes('{"名": 1}'.encode("gbk"))
    assert "UTF-8" in check_deliverable(gbk)


def test_csv(tmp_path):
    ok = tmp_path / "a.csv"
    ok.write_text('名称,数量\n"a,b",1\n', encoding="utf-8")
    assert check_deliverable(ok) is None
    gbk = tmp_path / "b.csv"
    gbk.write_bytes("名称,数量\n苹果,1\n".encode("gbk"))
    assert check_deliverable(gbk) is None
    ragged = tmp_path / "c.csv"
    ragged.write_text("a,b\n1\n1,2,3\n", encoding="utf-8")
    assert check_deliverable(ragged) is None  # 列数不一致不算损坏
    utf16 = tmp_path / "d.csv"
    utf16.write_bytes("a,b\n1,2\n".encode("utf-16"))
    assert "NUL" in check_deliverable(utf16)
    unclosed = tmp_path / "e.csv"
    unclosed.write_text('a,b\n"oops,1\n', encoding="utf-8")
    assert "CSV 格式有误" in check_deliverable(unclosed)


@pytest.fixture
def session(tmp_path, monkeypatch):
    paths = SessionPaths.create("s1", tmp_path)
    for d in paths.subdirs:
        d.mkdir(parents=True)
    src = paths.scratchpad / "a.docx"

    async def is_file(_p):
        return True

    sess = SimpleNamespace(
        workspace_lock=asyncio.Lock(),
        paths=SimpleNamespace(
            output=paths.output,
            session_root=paths.session_root,
            virtual_root="/workspace",
            virtual_to_container_path=lambda v: v,
            virtual_to_host_path=lambda v: src,
            trusted_root=lambda v: paths.session_root,
        ),
        resolve_virtual_path=lambda raw: "/workspace/scratchpad/a.docx",
        manager_client=None,
        container_name="c",
        is_file=is_file,
    )

    async def get_or_create(_sid):
        return sess

    monkeypatch.setattr(executor.CONTAINER_MANAGER, "get_or_create", get_or_create)
    executor._check_failures.clear()
    return paths, src


async def test_broken_file_rejected_and_previous_delivery_kept(session):
    paths, src = session
    _ooxml(src, "word/document.xml", "<good/>")
    await executor._present_container_files("s1", ["scratchpad/a.docx"])
    good = (paths.output / "a.docx").read_bytes()

    src.write_bytes(b"broken")
    with pytest.raises(ContainerFileSystemError, match="未通过交付前检查"):
        await executor._present_container_files("s1", ["scratchpad/a.docx"])
    assert (paths.output / "a.docx").read_bytes() == good


async def test_fixed_file_resets_counter(session):
    _paths, src = session
    src.write_bytes(b"broken")
    with pytest.raises(ContainerFileSystemError):
        await executor._present_container_files("s1", ["scratchpad/a.docx"])
    _ooxml(src, "word/document.xml")
    await executor._present_container_files("s1", ["scratchpad/a.docx"])
    assert executor._check_failures == {}


async def test_repeated_failures_never_deliver_broken_file(session):
    """修不好的文件不能"放行并警告":前端看不到警告,坏文件会被当成正常交付。"""
    paths, src = session
    src.write_bytes(b"broken")
    for _ in range(executor.MAX_CHECK_FAILURES - 1):
        with pytest.raises(ContainerFileSystemError, match="修复机会"):
            await executor._present_container_files("s1", ["scratchpad/a.docx"])
    with pytest.raises(ContainerFileSystemError, match="不要再重试"):
        await executor._present_container_files("s1", ["scratchpad/a.docx"])
    assert not (paths.output / "a.docx").exists()
    assert executor._check_failures == {}


async def test_abandoned_failed_deliveries_have_bounded_cache(session, monkeypatch):
    _, src = session
    src.write_bytes(b"broken")
    monkeypatch.setattr(executor, "MAX_CHECK_FAILURE_ENTRIES", 2)
    for sid in ("old", "recent", "new"):
        with pytest.raises(ContainerFileSystemError):
            await executor._present_container_files(sid, ["scratchpad/a.docx"])
    assert list(executor._check_failures) == [("recent", "a.docx"), ("new", "a.docx")]


_RELS = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{}</Relationships>'
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _rel(rid, target, mode=""):
    extra = f' TargetMode="{mode}"' if mode else ""
    return f'<Relationship Id="{rid}" Type="t" Target="{target}"{extra}/>'


def _pptx(path, rels, body=None, extra=()):
    body = (
        body
        or f'<p:presentation xmlns:p="p" xmlns:r="{_R}"><p:sldIdLst><p:sldId r:id="rId1"/></p:sldIdLst></p:presentation>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", _RELS.format(_rel("rId1", "ppt/presentation.xml")))
        zf.writestr("ppt/presentation.xml", body)
        zf.writestr("ppt/_rels/presentation.xml.rels", _RELS.format("".join(rels)))
        for name in extra:
            zf.writestr(name, "<p:sld xmlns:p='p'/>")


def test_ooxml_relationship_targets_must_exist(tmp_path):
    ok = tmp_path / "ok.pptx"
    _pptx(
        ok,
        [_rel("rId1", "slides/slide1.xml"), _rel("rId2", "https://x.test", "External")],
        extra=["ppt/slides/slide1.xml"],
    )
    assert check_deliverable(ok) is None

    absolute = tmp_path / "abs.pptx"
    _pptx(absolute, [_rel("rId1", "/ppt/slides/slide1.xml")], extra=["ppt/slides/slide1.xml"])
    assert check_deliverable(absolute) is None

    missing = tmp_path / "missing.pptx"
    _pptx(missing, [_rel("rId1", "slides/slide1.xml")])
    assert "ppt/slides/slide1.xml 不存在" in check_deliverable(missing)


def test_ooxml_undefined_relationship_id(tmp_path):
    f = tmp_path / "a.pptx"
    _pptx(f, [], extra=[])  # sldId 引用的 rId1 在 .rels 里没有
    assert "rId1 没有定义" in check_deliverable(f)


def test_ooxml_every_xml_part_must_parse(tmp_path):
    f = tmp_path / "a.pptx"
    _pptx(f, [_rel("rId1", "slides/slide1.xml")], extra=["ppt/slides/slide1.xml"])
    with zipfile.ZipFile(f, "a") as zf:
        zf.writestr("ppt/slides/slide2.xml", "<p:sld><broken")
    assert "slide2.xml 不是合法的 XML" in check_deliverable(f)


class _Sandbox:
    def __init__(self, output, is_error):
        self.result = (output, is_error)
        self.calls = []

    async def _run_isolated(self, cmd, timeout):
        self.calls.append(cmd)
        return self.result


async def test_pdf_sandbox_check():
    from app.services.deliverable_check import check_pdf_in_sandbox

    path = "/workspace/scratchpad/my report.pdf"
    broken = _Sandbox(f"DESKWORK_PDF_BROKEN\nqpdf: {path}: unable to find trailer", True)
    problem = await check_pdf_in_sandbox(broken, path)
    assert "已损坏" in problem and "my report.pdf: unable to find trailer" in problem
    assert "'/workspace/scratchpad/my report.pdf'" in broken.calls[0]  # 路径经过转义
    assert await check_pdf_in_sandbox(_Sandbox("", False), path) is None
    # 沙箱自身出错(没有标记)不当作文件损坏
    assert await check_pdf_in_sandbox(_Sandbox("Error: sandbox unavailable", True), path) is None


async def test_pdf_sandbox_failure_blocks_delivery(session, monkeypatch):
    paths, src = session
    src = paths.scratchpad / "a.pdf"
    src.write_bytes(b"%PDF-1.7\nbroken\n%%EOF\n")
    sess = await executor.CONTAINER_MANAGER.get_or_create("s1")
    sess.paths.virtual_to_host_path = lambda v: src
    sess.resolve_virtual_path = lambda raw: "/workspace/scratchpad/a.pdf"
    sess._run_isolated = _Sandbox("DESKWORK_PDF_BROKEN\nqpdf: bad xref", True)._run_isolated
    with pytest.raises(ContainerFileSystemError, match="PDF 已损坏"):
        await executor._present_container_files("s1", ["scratchpad/a.pdf"])
    assert not (paths.output / "a.pdf").exists()


def test_corrupt_deflate_stream_is_reported_not_raised(tmp_path):
    # 压缩数据被破坏时 zipfile 抛 zlib.error,不是 BadZipFile;要给出"文件损坏"而不是工具异常
    f = tmp_path / "a.docx"
    with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<root>" + "x" * 5000 + "</root>")
    data = bytearray(f.read_bytes())
    with zipfile.ZipFile(f) as zf:
        info = zf.getinfo("word/document.xml")
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    for i in range(start + 2, start + 12):
        data[i] ^= 0xFF
    f.write_bytes(bytes(data))
    assert isinstance(check_deliverable(f), str)
