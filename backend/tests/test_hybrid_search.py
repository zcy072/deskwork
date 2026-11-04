"""混合检索:RRF 融合、向量路的补召回、失败降级、未配置时的行为。"""

import httpx
import pytest

from app.config import settings
from app.services import doc_search
from app.services.doc_search import Bm25Index, rrf_fuse, search_index, split_chunks
from app.services.embeddings import cosine_with_norms, embed_texts, embedding_enabled

DOC = "\n".join(
    [
        "公司报销流程:员工需在费用发生后三十日内提交申请。",
        "出差期间的住宿标准为每晚四百元。",
        "年度体检安排在每年九月进行。",
    ]
)


def _index():
    return Bm25Index(split_chunks(DOC, size=40))


def test_cosine_with_norms():
    assert cosine_with_norms([1, 0], 1.0, [1, 0], 1.0) == pytest.approx(1.0)
    assert cosine_with_norms([1, 0], 1.0, [0, 1], 1.0) == pytest.approx(0.0)
    assert cosine_with_norms([0, 0], 0.0, [1, 1], 2**0.5) == 0.0


def test_rrf_prefers_chunks_ranked_by_both():
    fused = rrf_fuse([[1, 2, 3], [3, 1, 4]], top_k=3)
    assert [cid for cid, _ in fused][:2] == [1, 3]
    assert len(fused) == 3


async def test_bm25_only_when_no_vectors():
    index = _index()
    assert index.vectors is None
    hits = await search_index(index, "住宿标准", 2)
    assert "四百元" in hits[0][0].text


async def test_vector_route_recalls_what_bm25_misses(monkeypatch):
    index = _index()
    target = next(c for c in index.chunks if "住宿" in c.text)
    # 查询与 target 同向,其余正交:模拟“换了说法但语义相近”
    index.vectors = [[1.0, 0.0] if c.id == target.id else [0.0, 1.0] for c in index.chunks]

    async def fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(doc_search, "embed_texts", fake_embed)
    assert index.search("hotel", 3) == []  # BM25 完全搜不到
    hits = await search_index(index, "hotel", 3)
    assert hits and hits[0][0].id == target.id


async def test_query_embedding_failure_falls_back(monkeypatch):
    index = _index()
    index.vectors = [[1.0, 0.0] for _ in index.chunks]

    async def boom(texts):
        raise RuntimeError("down")

    monkeypatch.setattr(doc_search, "embed_texts", boom)
    hits = await search_index(index, "住宿标准", 2)
    assert "四百元" in hits[0][0].text


async def test_attach_vectors_respects_switch_and_limit(monkeypatch):
    calls = []

    async def fake_embed(texts):
        calls.append(len(texts))
        return [[1.0] for _ in texts]

    monkeypatch.setattr(doc_search, "embed_texts", fake_embed)
    index = _index()

    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "")
    await doc_search._attach_vectors(index)
    assert index.vectors is None and calls == []

    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "k")
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "http://x")
    monkeypatch.setattr(settings, "EMBEDDING_MAX_CHUNKS", 1)
    await doc_search._attach_vectors(index)
    assert index.vectors is None and calls == []

    monkeypatch.setattr(settings, "EMBEDDING_MAX_CHUNKS", 100)
    await doc_search._attach_vectors(index)
    assert len(index.vectors) == len(index.chunks)


async def test_embed_texts_batches_and_orders(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "k")
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "http://emb.test/v1")
    assert embedding_enabled()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        seen.append(len(body["input"]))
        assert request.headers["authorization"] == "Bearer k"
        # 故意乱序返回,验证按 index 排回
        data = [{"index": i, "embedding": [float(len(t))]} for i, t in enumerate(body["input"])]
        return httpx.Response(200, json={"data": list(reversed(data))})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    texts = ["a" * i for i in range(1, 71)]
    vecs = await embed_texts(texts)
    assert seen == [32, 32, 6]
    assert [v[0] for v in vecs] == [float(i) for i in range(1, 71)]
