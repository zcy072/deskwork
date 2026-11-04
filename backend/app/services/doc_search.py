"""长文档检索:分块 + BM25 关键词检索,可选叠加向量检索(混合),供 Agent 按需查阅。

默认只用 BM25:中文用字的一元 + 二元组分词,英文/数字按单词;代价是不理解同义词。
配置了 EMBEDDING_API_KEY 后,再用向量相似度召回一路,与 BM25 用 RRF 融合;
向量化失败或段数太多时自动退回 BM25。
"""

import asyncio
import contextlib
import hashlib
import json
import math
import re
import shlex
import uuid
from collections import Counter, OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path

from app.config import settings
from app.sandbox import CONTAINER_MANAGER
from app.services.document_sources import source_fingerprint
from app.services.embeddings import cosine_with_norms, embed_texts, embedding_enabled
from app.utils import logger

CHUNK_CHARS = 500
MAX_FILE_CHARS = 5_000_000
BM25_K1 = 1.5
BM25_B = 0.75
RRF_K = 60  # 倒数排名融合的平滑常数
CANDIDATES = 20  # 每一路取前多少名参与融合
PAGED_SUFFIXES = {".pdf", ".pptx"}  # 转换后保留页/幻灯片编号,可生成原文引用
PARAGRAPH_SUFFIXES = {".docx"}  # 转换后每段一行,行号即段号,可生成原文引用
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log", ".tsv"}
CONVERT_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx", ".doc", ".xls", ".ppt", ".html"}

# 按幻灯片提取文本的沙箱脚本:输出每张幻灯片一个字符串的 JSON 数组(空白页保留为空串)
PPTX_SLIDES_SCRIPT = """
import json, sys
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

def walk(shapes):
    for sh in shapes:
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from walk(sh.shapes)
        elif getattr(sh, "has_table", False) and sh.has_table:
            for row in sh.table.rows:
                yield " | ".join(c.text.strip() for c in row.cells)
        elif getattr(sh, "has_text_frame", False) and sh.has_text_frame:
            yield sh.text_frame.text

prs = Presentation(sys.argv[1])
json.dump(
    ["\\n".join(t for t in walk(s.shapes) if t.strip()) for s in prs.slides],
    sys.stdout, ensure_ascii=False,
)
"""

# 按段落提取 Word 正文的沙箱脚本:输出 JSON 数组,按文档顺序每个非空段落一项,
# 整张表格算一项(行内单元格用 " | " 连接,行之间用 " / ")。段内换行换成空格,
# 拼接后每项正好一行。页眉页脚、脚注、批注和文本框不在其中。
DOCX_PARAGRAPHS_SCRIPT = """
import json, sys
import docx
from docx.table import Table

def flat(text):
    return " ".join(text.split())

units = []
for block in docx.Document(sys.argv[1]).iter_inner_content():
    if isinstance(block, Table):
        rows = [" | ".join(flat(c.text) for c in row.cells) for row in block.rows]
        text = " / ".join(r for r in rows if r.strip(" |"))
    else:
        text = flat(block.text)
    if text:
        units.append(text)
json.dump(units, sys.stdout, ensure_ascii=False)
"""

_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]+")


@dataclass(frozen=True)
class Chunk:
    id: int
    text: str
    start: int  # 在全文中的字符偏移
    page: int | None = None  # PDF 物理页,或 pptx 幻灯片编号(由索引的 unit 区分)
    line_start: int = 1
    line_end: int = 1


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for piece in _TOKEN_RE.findall(text.lower()):
        if "一" <= piece[0] <= "鿿":
            tokens.extend(piece)
            tokens.extend(piece[i : i + 2] for i in range(len(piece) - 1))
        else:
            tokens.append(piece)
    return tokens


def split_chunks(text: str, size: int = CHUNK_CHARS, *, page: int | None = None) -> list[Chunk]:
    """按段落聚合到约 size 字符;超长段落硬切。保留字符偏移便于定位。"""
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_start = 0
    buf_len = 0
    first_line = last_line = 1

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            chunks.append(
                Chunk(len(chunks), "\n".join(buf), buf_start, page, first_line, last_line)
            )
        buf, buf_len = [], 0

    pos = 0
    for line_number, line in enumerate(text.split("\n"), 1):
        line_start = pos
        pos += len(line) + 1
        if not line.strip():
            continue
        for off in range(0, len(line), size):
            part = line[off : off + size]
            if buf and buf_len + len(part) > size:
                flush()
            if not buf:
                buf_start = line_start + off
                first_line = line_number
            buf.append(part)
            buf_len += len(part)
            last_line = line_number
    flush()
    return chunks


def split_paged_chunks(pages: list[str]) -> list[Chunk]:
    """按页(PDF)或幻灯片(pptx)分块,编号从 1 开始,空白页也占号。"""
    chunks: list[Chunk] = []
    for page_number, text in enumerate(pages, 1):
        for chunk in split_chunks(text, page=page_number):
            chunks.append(replace(chunk, id=len(chunks)))
    return chunks


class Bm25Index:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.source_path = ""
        # 引用单位:"page"(PDF)、"slide"(pptx)、"paragraph"(docx,行号即段号)
        self.unit = "page"
        self.fingerprint = ""
        self._tf = [Counter(tokenize(c.text)) for c in chunks]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(chunks)) if chunks else 0.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(chunks)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self._vectors: list[list[float]] | None = None
        self._norms: list[float] = []

    @property
    def vectors(self) -> list[list[float]] | None:
        """与 chunks 对齐的向量;None = 没有向量。赋值时顺带预计算范数,检索时不再重复开方。"""
        return self._vectors

    @vectors.setter
    def vectors(self, value: list[list[float]] | None) -> None:
        self._vectors = value
        self._norms = [math.sqrt(sum(x * x for x in v)) for v in value] if value else []

    def similarities(self, qv: list[float]) -> list[tuple[int, float]]:
        qn = math.sqrt(sum(x * x for x in qv))
        return [
            (i, cosine_with_norms(qv, qn, v, n))
            for i, (v, n) in enumerate(zip(self._vectors or [], self._norms, strict=True))
        ]

    def search(self, query: str, top_k: int = 5) -> list[tuple[Chunk, float]]:
        terms = set(tokenize(query))
        scored: list[tuple[Chunk, float]] = []
        for chunk, tf, length in zip(self.chunks, self._tf, self._len, strict=True):
            score = 0.0
            for t in terms:
                f = tf.get(t)
                if not f:
                    continue
                norm = f + BM25_K1 * (1 - BM25_B + BM25_B * length / (self._avg or 1))
                score += self._idf[t] * f * (BM25_K1 + 1) / norm
            if score > 0:
                scored.append((chunk, score))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]


def rrf_fuse(rankings: list[list[int]], top_k: int) -> list[tuple[int, float]]:
    """倒数排名融合:每一路排名靠前的段落得分高,多路都靠前的更高。返回 (段 id, 融合分)。"""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])[:top_k]


async def search_index(index: Bm25Index, query: str, top_k: int = 5) -> list[tuple[Chunk, float]]:
    """有向量就做混合检索,否则(或查询向量化失败)只用 BM25。"""
    bm25 = await asyncio.to_thread(index.search, query, CANDIDATES)
    if index.vectors is None:
        return bm25[:top_k]
    try:
        (qv,) = await embed_texts([query])
    except Exception as exc:
        logger.warning(f"query embedding failed, fallback to BM25: {exc}")
        return bm25[:top_k]
    sims = await asyncio.to_thread(
        lambda: sorted(index.similarities(qv), key=lambda x: -x[1])[:CANDIDATES]
    )
    fused = rrf_fuse([[c.id for c, _ in bm25], [i for i, _ in sims]], top_k)
    return [(index.chunks[cid], score) for cid, score in fused]


async def _attach_vectors(index: Bm25Index) -> None:
    if not embedding_enabled() or not index.chunks:
        return
    if len(index.chunks) > settings.EMBEDDING_MAX_CHUNKS:
        logger.info(f"{len(index.chunks)} chunks > EMBEDDING_MAX_CHUNKS, BM25 only")
        return
    try:
        index.vectors = await embed_texts([c.text for c in index.chunks])
    except Exception as exc:
        logger.warning(f"chunk embedding failed, BM25 only: {exc}")


# (会话, 路径, 修改时间) -> 索引;文件变了自然失效。LRU:命中时移到末尾,满了淘汰最久未用的
_CACHE: OrderedDict[tuple[str, str, tuple[int, ...]], Bm25Index] = OrderedDict()
_CACHE_MAX = 8
# 同一个 key 正在构建的任务:并发的 search_document 共用一次分块 / 向量化
_INFLIGHT: dict[tuple[str, str, tuple[int, ...]], asyncio.Task[Bm25Index]] = {}


async def _load_text(session_id: str, virtual_path: str) -> tuple[str | list[str], Path]:
    session = await CONTAINER_MANAGER.get_or_create(session_id)
    virtual_path = session.resolve_virtual_path(virtual_path)
    container_path = session.paths.virtual_to_container_path(virtual_path)
    if not await session.is_file(container_path):
        raise RuntimeError(f"File not found: {virtual_path}")
    suffix = Path(virtual_path).suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return await session.read_container_file(container_path), Path(container_path)
    if suffix not in CONVERT_SUFFIXES:
        raise RuntimeError(f"不支持检索 {suffix or '无扩展名'} 文件")
    work = f"{session.paths.virtual_root}/scratchpad/_rag"
    # 每次调用一个文件名:并行转换(含同一文件的不同版本)不会覆盖或删掉对方的中间文件
    tag = uuid.uuid4().hex
    paged = suffix in PAGED_SUFFIXES
    paragraphs = suffix in PARAGRAPH_SUFFIXES
    converted = f"{tag}.json" if paged or paragraphs else f"{tag}.md"
    out = f"{work}/{converted}"
    if suffix == ".pdf":
        script = (
            "import json, sys, pymupdf; "
            "doc = pymupdf.open(sys.argv[1]); "
            "json.dump([p.get_text() for p in doc], sys.stdout, ensure_ascii=False); doc.close()"
        )
        command = f"python3 -c {shlex.quote(script)} {shlex.quote(virtual_path)}"
    elif suffix == ".pptx":
        command = f"python3 -c {shlex.quote(PPTX_SLIDES_SCRIPT)} {shlex.quote(virtual_path)}"
    elif paragraphs:
        command = f"python3 -c {shlex.quote(DOCX_PARAGRAPHS_SCRIPT)} {shlex.quote(virtual_path)}"
    else:
        command = f"markitdown {shlex.quote(virtual_path)}"
    host_out = session.paths.scratchpad / "_rag" / converted
    try:
        out_text, is_error = await session.execute(
            f"mkdir -p {work} && {command} > {shlex.quote(out)}",
            timeout=90,  # 沙箱执行上限(EXEC_TIMEOUT_SEC)
        )
        if is_error:
            raise RuntimeError(f"文档转换失败: {out_text[:300]}")
        try:
            data = await asyncio.to_thread(
                session.paths.read_host_file, host_out, MAX_FILE_CHARS * 4 + 1
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"读取转换结果失败: {exc}") from exc
    finally:
        # 索引已缓存在内存里;中间文件留着只会占配额、进快照
        with contextlib.suppress(OSError):
            await asyncio.to_thread(host_out.unlink, missing_ok=True)
    if len(data) > MAX_FILE_CHARS * 4:
        raise RuntimeError("文档转换结果过大")
    text = data.decode("utf-8", errors="replace")
    if paged or paragraphs:
        units = json.loads(text)
        if not isinstance(units, list) or not all(isinstance(p, str) for p in units):
            raise RuntimeError("文档分页文本转换结果无效")
        if paragraphs:
            # 每段一行,split_chunks 给出的行号就是段号
            return "\n".join(" ".join(u.split()) for u in units), host_out
        return units, host_out
    return text, host_out


def _host_mtime(host: Path) -> tuple[int, ...]:
    try:
        st = host.stat()
        return st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino
    except FileNotFoundError:
        return ()


async def _build_index(session_id: str, virtual_path: str) -> Bm25Index:
    session = await CONTAINER_MANAGER.get_or_create(session_id)
    relative = virtual_path.removeprefix(session.paths.virtual_root.rstrip("/") + "/")
    citable = (
        relative.startswith(("uploads/", "output/"))
        and Path(virtual_path).suffix.lower() in TEXT_SUFFIXES | PAGED_SUFFIXES | PARAGRAPH_SUFFIXES
    )
    fingerprint = (
        await asyncio.to_thread(source_fingerprint, session.paths, relative) if citable else ""
    )
    text, _ = await _load_text(session_id, virtual_path)
    length = sum(map(len, text)) if isinstance(text, list) else len(text)
    if length > MAX_FILE_CHARS:
        raise RuntimeError(f"文档过大({length} 字符,上限 {MAX_FILE_CHARS})")
    index = await asyncio.to_thread(
        lambda: Bm25Index(
            split_paged_chunks(text) if isinstance(text, list) else split_chunks(text)
        )
    )
    if citable:
        if fingerprint != await asyncio.to_thread(source_fingerprint, session.paths, relative):
            raise RuntimeError("检索期间文件发生变化,请重新检索")
        index.source_path, index.fingerprint = virtual_path, fingerprint
        suffix = Path(virtual_path).suffix.lower()
        if suffix == ".pptx":
            index.unit = "slide"
        elif suffix in PARAGRAPH_SUFFIXES:
            index.unit = "paragraph"
    await _attach_vectors(index)
    return index


async def get_index(session_id: str, virtual_path: str) -> Bm25Index:
    session = await CONTAINER_MANAGER.get_or_create(session_id)
    virtual_path = session.resolve_virtual_path(virtual_path)
    host = session.paths.uploads.parent / virtual_path.removeprefix(
        session.paths.virtual_root.rstrip("/") + "/"
    )
    mtime = await asyncio.to_thread(_host_mtime, host)
    key = (session_id, virtual_path, mtime)
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    task = _INFLIGHT.get(key)
    if task is None:
        task = asyncio.create_task(_build_index(session_id, virtual_path))
        _INFLIGHT[key] = task

        def _done(t: asyncio.Task, key=key) -> None:
            _INFLIGHT.pop(key, None)
            if not t.cancelled() and t.exception() is None:
                _CACHE[key] = t.result()
                while len(_CACHE) > _CACHE_MAX:
                    _CACHE.popitem(last=False)
            elif not t.cancelled():
                t.exception()  # 已取走异常,等待者各自会收到

        task.add_done_callback(_done)
    # shield:某个调用方被取消不应该打断其它等待同一份索引的调用方
    return await asyncio.shield(task)


def source_references(index: Bm25Index, results: list[tuple[Chunk, float]]) -> list[dict]:
    if not index.source_path or not index.fingerprint:
        return []
    sources = []
    for chunk, _ in results:
        identity = f"{index.source_path}:{index.fingerprint}:{chunk.id}"
        if chunk.page is not None:
            location = {"kind": index.unit, index.unit: chunk.page}
        else:
            kind = "paragraphs" if index.unit == "paragraph" else "lines"
            location = {"kind": kind, "start": chunk.line_start, "end": chunk.line_end}
        sources.append(
            {
                "id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                "file_name": Path(index.source_path).name,
                "container_path": index.source_path,
                "fingerprint": index.fingerprint,
                "location": location,
                "excerpt": chunk.text,
            }
        )
    return sources


def _range_label(start: int, end: int, unit: str) -> str:
    return f"第 {start} {unit}" if start == end else f"第 {start}–{end} {unit}"


def format_results(
    results: list[tuple[Chunk, float]], total_chunks: int, sources: list[dict] | None = None
) -> str:
    if not results:
        return f"没有找到相关段落(共 {total_chunks} 段)。换用原文中可能出现的词再搜,或确认文档确实没有该内容。"
    lines = [f"共 {total_chunks} 段,返回最相关的 {len(results)} 段:"]
    for i, (chunk, score) in enumerate(results):
        citation = ""
        if sources:
            source = sources[i]
            loc = source["location"]
            if loc["kind"] == "page":
                label = f"第 {loc['page']} 页"
            elif loc["kind"] == "slide":
                label = f"第 {loc['slide']} 张幻灯片"
            elif loc["kind"] == "paragraphs":
                label = _range_label(loc["start"], loc["end"], "段")
            else:
                label = f"第 {loc['start']}–{loc['end']} 行"
            citation = f"\n引用链接:[原文 · {label}](#source-{source['id']})"
        lines.append(
            f"\n[段 {chunk.id} | 偏移 {chunk.start} | 分数 {score:.3f}]{citation}\n{chunk.text}"
        )
    return "\n".join(lines)
