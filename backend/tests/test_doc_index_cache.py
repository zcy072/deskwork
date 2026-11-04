"""get_index:缓存命中、mtime 失效、并发去重、LRU;向量范数预计算。"""

import asyncio
import os
from types import SimpleNamespace

import pytest

from app.services import doc_search
from app.services.doc_search import Bm25Index, Chunk


@pytest.fixture
def env(tmp_path, monkeypatch):
    f = tmp_path / "uploads" / "a.md"
    f.parent.mkdir()
    f.write_text("青鸟预算 2368000\n其他内容")
    session = SimpleNamespace(
        resolve_virtual_path=lambda p: p,
        paths=SimpleNamespace(
            uploads=f.parent,
            virtual_root="/workspace",
            session_root=tmp_path,
            resolve_host_path=lambda relative: (tmp_path / relative).resolve(),
            read_host_file=lambda path, limit: path.read_bytes()[:limit],
        ),
    )

    async def get_or_create(_sid):
        return session

    monkeypatch.setattr(doc_search.CONTAINER_MANAGER, "get_or_create", get_or_create)
    builds = []

    async def load_text(_sid, vpath):
        builds.append(vpath)
        await asyncio.sleep(0.02)
        return f.read_text(), f

    monkeypatch.setattr(doc_search, "_load_text", load_text)

    async def no_vec(_index):
        return None

    monkeypatch.setattr(doc_search, "_attach_vectors", no_vec)
    doc_search._CACHE.clear()
    doc_search._INFLIGHT.clear()
    yield f, builds
    doc_search._CACHE.clear()


async def test_cache_hit_and_mtime_invalidation(env):
    f, builds = env
    a = await doc_search.get_index("s", "/workspace/uploads/a.md")
    b = await doc_search.get_index("s", "/workspace/uploads/a.md")
    assert a is b and len(builds) == 1
    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + 10))
    c = await doc_search.get_index("s", "/workspace/uploads/a.md")
    assert c is not a and len(builds) == 2


async def test_concurrent_calls_share_one_build(env):
    _, builds = env
    results = await asyncio.gather(
        *(doc_search.get_index("s", "/workspace/uploads/a.md") for _ in range(4))
    )
    assert len(builds) == 1 and all(r is results[0] for r in results)


async def test_same_mtime_rewrite_invalidates_cache(env):
    f, builds = env
    before = await doc_search.get_index("s", "/workspace/uploads/a.md")
    st = f.stat()
    f.write_text("新的预算 100 元")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = await doc_search.get_index("s", "/workspace/uploads/a.md")
    assert after is not before and len(builds) == 2
    assert after.fingerprint != before.fingerprint


async def test_file_change_during_indexing_is_not_cached(env, monkeypatch):
    f, _ = env

    async def changing(*_):
        text = f.read_text()
        f.write_text("检索时已变化")
        return text, f

    monkeypatch.setattr(doc_search, "_load_text", changing)
    with pytest.raises(RuntimeError, match="发生变化"):
        await doc_search.get_index("s", "/workspace/uploads/a.md")
    assert not doc_search._CACHE


async def test_cache_is_lru(env, monkeypatch):
    f, _ = env
    monkeypatch.setattr(doc_search, "_CACHE_MAX", 2)
    for name in ("a", "b"):
        (f.parent / f"{name}.md").write_text(name)
    await doc_search.get_index("s", "/workspace/uploads/a.md")
    await doc_search.get_index("s", "/workspace/uploads/b.md")
    await doc_search.get_index("s", "/workspace/uploads/a.md")  # a 最近使用过
    await asyncio.sleep(0)
    keys = {k[1] for k in doc_search._CACHE}
    (f.parent / "c.md").write_text("c")
    await doc_search.get_index("s", "/workspace/uploads/c.md")
    assert {k[1] for k in doc_search._CACHE} == {
        "/workspace/uploads/a.md",
        "/workspace/uploads/c.md",
    }
    assert "/workspace/uploads/b.md" in keys


async def test_failed_build_is_not_cached(env, monkeypatch):
    async def boom(*_a):
        raise RuntimeError("nope")

    monkeypatch.setattr(doc_search, "_load_text", boom)
    with pytest.raises(RuntimeError):
        await doc_search.get_index("s", "/workspace/uploads/a.md")
    assert not doc_search._CACHE and not doc_search._INFLIGHT


def test_vector_norms_precomputed():
    idx = Bm25Index([Chunk(0, "a", 0), Chunk(1, "b", 0)])
    idx.vectors = [[3.0, 4.0], [0.0, 0.0]]
    assert idx._norms == [5.0, 0.0]
    sims = dict(idx.similarities([3.0, 4.0]))
    assert sims[0] == pytest.approx(1.0) and sims[1] == 0.0
