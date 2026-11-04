"""引用的页/行定位、工具事件、文件变更校验和历史持久化。"""

import hashlib
import json
import shlex
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import app
from app.sandbox import session_paths
from app.services import doc_search
from app.services.chat_operations import _normalize_custom
from app.services.doc_search import (
    Bm25Index,
    format_results,
    source_references,
    split_chunks,
    split_paged_chunks,
)
from app.services.document_sources import read_source, source_fingerprint
from app.services.history import TurnRecorder
from app.tools import search_tools


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(session_paths, "SESSION_ROOT", tmp_path)
    p = session_paths.SessionPaths.create("source-session", tmp_path)
    for d in p.subdirs:
        d.mkdir(parents=True)
    return p


def test_text_lines_include_blanks_crlf_and_long_lines():
    text = "\r\n第一行\r\n\r\n" + "正文" * 500 + "\r\n末尾"
    chunks = split_chunks(text, size=100)
    original = text.split("\n")
    for chunk in chunks:
        assert chunk.line_start <= chunk.line_end
        context = "\n".join(original[chunk.line_start - 1 : chunk.line_end])
        assert chunk.text in context
    assert chunks[0].line_start == 2
    assert chunks[-1].line_end == 5


def test_pdf_chunks_keep_physical_pages_including_blank_pages():
    chunks = split_paged_chunks(["第一页", "", "第三页预算 2368000"])
    assert [c.page for c in chunks] == [1, 3]
    assert [c.id for c in chunks] == [0, 1]
    index = Bm25Index(chunks)
    index.source_path = "/workspace/uploads/a.pdf"
    index.fingerprint = "a" * 64
    results = index.search("预算")
    sources = source_references(index, results)
    assert sources[0]["location"] == {"kind": "page", "page": 3}
    assert f"(#source-{sources[0]['id']})" in format_results(results, len(chunks), sources)
    assert "第 3 页" in format_results(results, len(chunks), sources)
    index.fingerprint = "b" * 64
    assert source_references(index, results)[0]["id"] != sources[0]["id"]


def test_slide_sources_use_slide_location_and_label():
    chunks = split_paged_chunks(["封面", "", "第三张预算 2368000"])
    index = Bm25Index(chunks)
    index.source_path = "/workspace/uploads/deck.pptx"
    index.fingerprint = "a" * 64
    index.unit = "slide"
    results = index.search("预算")
    sources = source_references(index, results)
    assert sources[0]["location"] == {"kind": "slide", "slide": 3}
    assert "第 3 张幻灯片" in format_results(results, len(chunks), sources)


def test_pptx_script_extracts_text_per_slide_keeping_blank_slides(tmp_path):
    pptx = pytest.importorskip("pptx")
    import subprocess
    import sys

    prs = pptx.Presentation()
    blank, title_only = prs.slide_layouts[6], prs.slide_layouts[5]
    first = prs.slides.add_slide(title_only)
    first.shapes.title.text = "封面"
    prs.slides.add_slide(blank)
    third = prs.slides.add_slide(blank)
    third.shapes.add_textbox(0, 0, 100, 100).text_frame.text = "预算 2368000"
    path = tmp_path / "deck.pptx"
    prs.save(path)
    out = subprocess.run(
        [sys.executable, "-c", doc_search.PPTX_SLIDES_SCRIPT, str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    slides = json.loads(out)
    assert len(slides) == 3
    assert slides[0] == "封面" and slides[1] == "" and "预算 2368000" in slides[2]


def test_pptx_script_reads_tables_and_grouped_shapes(tmp_path):
    pptx = pytest.importorskip("pptx")
    import subprocess
    import sys

    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    table = slide.shapes.add_table(2, 2, 0, 0, 2000000, 1000000).table
    table.cell(0, 0).text = "季度"
    table.cell(1, 1).text = "表格数值 777"
    group = slide.shapes.add_group_shape()
    group.shapes.add_textbox(0, 0, 100, 100).text_frame.text = "组合内文字 888"
    path = tmp_path / "deck.pptx"
    prs.save(path)
    out = subprocess.run(
        [sys.executable, "-c", doc_search.PPTX_SLIDES_SCRIPT, str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    text = json.loads(out)[0]
    assert "季度" in text and "表格数值 777" in text and "组合内文字 888" in text


def test_uncitable_document_has_no_made_up_sources():
    index = Bm25Index(split_chunks("内容"))
    assert source_references(index, index.search("内容")) == []


def test_verified_download_rejects_changed_file(paths):
    source = paths.uploads / "a.txt"
    source.write_text("原文预算 100 元")
    digest = source_fingerprint(paths, "uploads/a.txt")
    params = {
        "session_id": paths.session_id,
        "relative_path": "uploads/a.txt",
        "fingerprint": digest,
    }
    client = TestClient(app)
    response = client.get("/files/download", params=params)
    assert response.status_code == 200
    assert response.content == source.read_bytes()
    source.write_text("原文预算 200 元")
    response = client.get("/files/download", params=params)
    assert response.status_code == 409
    assert "已变化" in response.json()["detail"]
    assert (
        client.get("/files/download", params={**params, "fingerprint": "invalid"}).status_code
        == 422
    )


def test_source_path_and_size_limits(paths, monkeypatch):
    (paths.scratchpad / "secret.txt").write_text("secret")
    with pytest.raises(ValueError):
        read_source(paths, "output/../scratchpad/secret.txt")
    (paths.uploads / "escape.txt").symlink_to(paths.session_root.parent / "outside.txt")
    with pytest.raises(ValueError):
        read_source(paths, "uploads/escape.txt")
    from app.services import document_sources

    monkeypatch.setattr(document_sources, "MAX_SOURCE_BYTES", 2)
    (paths.uploads / "a.txt").write_text("long")
    with pytest.raises(ValueError, match="50 MB"):
        read_source(paths, "uploads/a.txt")


async def test_pdf_extraction_command_and_index_preserve_page_numbers(paths, monkeypatch):
    source = paths.uploads / "report.pdf"
    source.write_bytes(b"%PDF-fixture")
    observed = []

    async def execute(command, timeout):
        observed.append(command)
        # 只替代 PDF 库的执行,其余中间文件读取与分块走真实逻辑。
        output = paths.scratchpad / "_rag" / shlex.split(command)[-1].rsplit("/", 1)[-1]
        output.parent.mkdir()
        output.write_text(json.dumps(["第一页", "", "第三页预算 100 元"]))
        return "", False

    async def is_file(_):
        return True

    fake_paths = SimpleNamespace(
        virtual_root="/workspace",
        scratchpad=paths.scratchpad,
        virtual_to_container_path=lambda p: p,
        read_host_file=paths.read_host_file,
        resolve_host_path=paths.resolve_host_path,
        session_root=paths.session_root,
    )
    session = SimpleNamespace(
        paths=fake_paths, resolve_virtual_path=lambda p: p, is_file=is_file, execute=execute
    )

    async def get_session(_):
        return session

    monkeypatch.setattr(doc_search.CONTAINER_MANAGER, "get_or_create", get_session)

    async def no_vectors(_):
        pass

    monkeypatch.setattr(doc_search, "_attach_vectors", no_vectors)
    index = await doc_search._build_index(paths.session_id, "/workspace/uploads/report.pdf")
    assert "pymupdf" in observed[0] and "get_text" in observed[0]
    assert source_references(index, index.search("预算"))[0]["location"]["page"] == 3
    assert index.fingerprint == hashlib.sha256(source.read_bytes()).hexdigest()


async def test_pptx_index_is_citable_by_slide(paths, monkeypatch):
    source = paths.uploads / "deck.pptx"
    source.write_bytes(b"PK-fixture")
    observed = []

    async def execute(command, timeout):
        observed.append(command)
        output = paths.scratchpad / "_rag" / shlex.split(command)[-1].rsplit("/", 1)[-1]
        output.parent.mkdir()
        output.write_text(json.dumps(["封面", "", "预算 100 元"]))
        return "", False

    async def is_file(_):
        return True

    fake_paths = SimpleNamespace(
        virtual_root="/workspace",
        scratchpad=paths.scratchpad,
        virtual_to_container_path=lambda p: p,
        read_host_file=paths.read_host_file,
        resolve_host_path=paths.resolve_host_path,
        session_root=paths.session_root,
    )
    session = SimpleNamespace(
        paths=fake_paths, resolve_virtual_path=lambda p: p, is_file=is_file, execute=execute
    )

    async def get_session(_):
        return session

    async def no_vectors(_):
        pass

    monkeypatch.setattr(doc_search.CONTAINER_MANAGER, "get_or_create", get_session)
    monkeypatch.setattr(doc_search, "_attach_vectors", no_vectors)
    index = await doc_search._build_index(paths.session_id, "/workspace/uploads/deck.pptx")
    assert "pptx" in observed[0] and "markitdown" not in observed[0]
    location = source_references(index, index.search("预算"))[0]["location"]
    assert location == {"kind": "slide", "slide": 3}
    assert index.fingerprint == hashlib.sha256(source.read_bytes()).hexdigest()


async def test_docx_index_is_citable_by_paragraph(paths, monkeypatch):
    source = paths.uploads / "report.docx"
    source.write_bytes(b"PK-fixture")
    observed = []

    async def execute(command, timeout):
        observed.append(command)
        output = paths.scratchpad / "_rag" / shlex.split(command)[-1].rsplit("/", 1)[-1]
        output.parent.mkdir()
        # 段内换行要压成一行,否则行号与段号错位
        # 第二段压成一行后 491 字,与第三段合计超过一块(500 字),三段各自落在不同的块
        units = ["标题" * 150, "第二段\n续行" * 82, "预算 100 元" + "说明" * 10]
        output.write_text(json.dumps(units))
        return "", False

    async def is_file(_):
        return True

    fake_paths = SimpleNamespace(
        virtual_root="/workspace",
        scratchpad=paths.scratchpad,
        virtual_to_container_path=lambda p: p,
        read_host_file=paths.read_host_file,
        resolve_host_path=paths.resolve_host_path,
        session_root=paths.session_root,
    )
    session = SimpleNamespace(
        paths=fake_paths, resolve_virtual_path=lambda p: p, is_file=is_file, execute=execute
    )

    async def get_session(_):
        return session

    async def no_vectors(_):
        pass

    monkeypatch.setattr(doc_search.CONTAINER_MANAGER, "get_or_create", get_session)
    monkeypatch.setattr(doc_search, "_attach_vectors", no_vectors)
    index = await doc_search._build_index(paths.session_id, "/workspace/uploads/report.docx")
    assert "iter_inner_content" in observed[0] and "markitdown" not in observed[0]
    results = index.search("预算")
    sources = source_references(index, results)
    assert sources[0]["location"] == {"kind": "paragraphs", "start": 3, "end": 3}
    assert [(c.line_start, c.line_end) for c in index.chunks] == [(1, 1), (2, 2), (3, 3)]
    assert "第 3 段" in format_results(results, len(index.chunks), sources)
    assert index.fingerprint == hashlib.sha256(source.read_bytes()).hexdigest()


async def test_search_emits_sources_and_recorder_preserves_them(monkeypatch):
    index = Bm25Index(split_chunks("第一行\n预算 100 元"))
    index.source_path, index.fingerprint = "/workspace/uploads/a.txt", "a" * 64

    async def get_index(*_):
        return index

    monkeypatch.setattr(search_tools, "get_index", get_index)
    emitted = []
    monkeypatch.setattr(search_tools, "get_stream_writer", lambda: emitted.append)
    runtime = SimpleNamespace(config={"configurable": {"thread_id": "s"}}, tool_call_id="call1")
    result = await search_tools.search_document.coroutine(
        "/workspace/uploads/a.txt", "预算", "查询预算", runtime
    )
    event, payload = _normalize_custom(emitted[0])
    assert event == "sources.presented"
    recorder = TurnRecorder()
    recorder.record(event, payload)
    assert recorder.parts == [{"type": "sources", "sources": payload["sources"]}]
    assert payload["sources"][0]["id"] in result.update["messages"][0].content
