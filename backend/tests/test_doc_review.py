"""产物自检:默认关闭;配置后才启用;不支持的类型给出明确错误。"""

import pytest

from app.config import settings
from app.services import doc_review


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_LLM_PROFILE", "")
    assert not doc_review.review_enabled()


def test_enabled_when_profile_set(monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_LLM_PROFILE", "anthropic")
    assert doc_review.review_enabled()


async def test_unsupported_suffix_rejected(monkeypatch):
    class FakeSession:
        paths = type("P", (), {"virtual_root": "/workspace"})()

        def resolve_virtual_path(self, raw):
            return raw

    class FakeManager:
        async def get_or_create(self, _sid):
            return FakeSession()

    monkeypatch.setattr(doc_review, "CONTAINER_MANAGER", FakeManager())
    with pytest.raises(RuntimeError, match="不支持审阅"):
        await doc_review.render_pages("s", "/workspace/output/a.csv")


async def test_parallel_renders_are_isolated_and_cleaned(tmp_path, monkeypatch):
    """并行审阅各用各的目录,读到的是自己的页面;读完目录被删掉。"""
    import asyncio
    import re

    from app.sandbox import session_paths

    monkeypatch.setattr(session_paths, "SANDBOX_DATA_ROOT", tmp_path)
    paths = session_paths.SessionPaths.create("rev-sess", tmp_path)
    for d in paths.subdirs:
        d.mkdir(parents=True)

    class FakeSession:
        def __init__(self):
            self.paths = paths

        def resolve_virtual_path(self, raw):
            return raw

        async def is_file(self, _p):
            return True

        async def execute(self, cmd, timeout=None):
            run_id = re.search(r"_review/([0-9a-f]+)", cmd).group(1)
            name = re.search(r"/output/(\w+)\.pdf", cmd).group(1)
            work = paths.scratchpad / "_review" / run_id
            work.mkdir(parents=True, exist_ok=True)
            await asyncio.sleep(0.02)
            (work / "page-1.jpg").write_bytes(name.encode())
            await asyncio.sleep(0.02)
            return "", False

    class FakeManager:
        async def get_or_create(self, _sid):
            return FakeSession()

    monkeypatch.setattr(doc_review, "CONTAINER_MANAGER", FakeManager())
    a, b = await asyncio.gather(
        doc_review.render_pages("rev-sess", "/workspace/output/a.pdf"),
        doc_review.render_pages("rev-sess", "/workspace/output/b.pdf"),
    )
    assert (a, b) == ([b"a"], [b"b"])
    assert list((paths.scratchpad / "_review").iterdir()) == []
