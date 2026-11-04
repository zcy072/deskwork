"""Office 预览:路径白名单、缓存、并发合并、失败映射(沙箱用假会话代替)。"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import app
from app.sandbox import session_paths
from app.services import file_preview

SID = "preview-sess"


class FakeSession:
    def __init__(self, paths, pdf: bytes | None = b"%PDF-fake", error: bool = False):
        self.paths = paths
        self.pdf = pdf
        self.error = error
        self.calls = 0

    async def run_isolated(self, cmd, timeout=None):
        self.calls += 1
        await asyncio.sleep(0.05)
        if self.error:
            return "boom", True
        if self.pdf is not None:
            tag_dir = self.paths.scratchpad / "_preview" / cmd.split("_preview/")[1].split()[0]
            tag_dir.mkdir(parents=True, exist_ok=True)
            (tag_dir / "a.pdf").write_bytes(self.pdf)
        return "", False


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(session_paths, "SESSION_ROOT", tmp_path)
    paths = session_paths.SessionPaths.create(SID, tmp_path)
    for d in paths.subdirs:
        d.mkdir(parents=True, exist_ok=True)
    (paths.uploads / "a.docx").write_bytes(b"docx")
    (paths.uploads / "n.txt").write_text("x")
    (paths.scratchpad / "s.docx").write_bytes(b"docx")
    fake = FakeSession(paths)

    class Mgr:
        async def get_or_create(self, sid):
            return fake

    monkeypatch.setattr(file_preview, "CONTAINER_MANAGER", Mgr())
    monkeypatch.setattr(file_preview, "SessionPaths", session_paths.SessionPaths)
    file_preview._CACHE.clear()
    file_preview._INFLIGHT.clear()
    return paths, fake


def _client():
    return TestClient(app)


def _get(path):
    return _client().get("/files/preview", params={"session_id": SID, "relative_path": path})


def test_rejects_unsupported_type_and_non_whitelisted_dirs(env):
    assert _get("uploads/n.txt").status_code == 400
    assert _get("scratchpad/s.docx").status_code == 400
    assert _get("output/../scratchpad/s.docx").status_code == 400
    assert _get("uploads/missing.docx").status_code == 404


def test_converts_and_returns_pdf_with_security_headers(env):
    r = _get("uploads/a.docx")
    assert r.status_code == 200
    assert r.content == b"%PDF-fake"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "sandbox"


def test_second_request_hits_cache_until_file_changes(env):
    paths, fake = env
    assert _get("uploads/a.docx").status_code == 200
    assert _get("uploads/a.docx").status_code == 200
    assert fake.calls == 1
    f = paths.uploads / "a.docx"
    f.write_bytes(b"changed")
    import os

    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + 10))
    assert _get("uploads/a.docx").status_code == 200
    assert fake.calls == 2


async def test_concurrent_requests_share_one_conversion(env):
    _, fake = env
    results = await asyncio.gather(
        *(file_preview.office_to_pdf(SID, "uploads/a.docx") for _ in range(4))
    )
    assert set(results) == {b"%PDF-fake"}
    assert fake.calls == 1


def test_conversion_failure_is_422_and_not_cached(env):
    _, fake = env
    fake.error = True
    assert _get("uploads/a.docx").status_code == 422
    fake.error = False
    assert _get("uploads/a.docx").status_code == 200


def test_missing_output_is_422(env):
    _, fake = env
    fake.pdf = None
    assert _get("uploads/a.docx").status_code == 422


def test_symlinked_result_is_refused(env, tmp_path):
    paths, fake = env
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"SECRET")

    async def evil(cmd, timeout=None):
        d = paths.scratchpad / "_preview" / cmd.split("_preview/")[1].split()[0]
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.pdf").symlink_to(secret)
        return "", False

    fake.run_isolated = evil
    r = _get("uploads/a.docx")
    assert r.status_code == 422
    assert b"SECRET" not in r.content


async def test_prewarm_converts_office_only_and_populates_cache(env):
    _, fake = env
    await file_preview.prewarm(SID, ["uploads/a.docx", "uploads/n.txt", "uploads/missing.docx"])
    assert fake.calls == 1
    assert _get("uploads/a.docx").status_code == 200
    assert fake.calls == 1  # 命中预热的缓存


async def test_prewarm_swallows_failures(env):
    paths, fake = env
    fake.error = True
    await file_preview.prewarm(SID, ["uploads/a.docx"])  # 不抛异常


async def test_prewarm_limits_file_count(env):
    paths, fake = env
    names = []
    for i in range(file_preview.PREWARM_MAX_FILES + 2):
        (paths.uploads / f"f{i}.docx").write_bytes(b"x")
        names.append(f"uploads/f{i}.docx")
    await file_preview.prewarm(SID, names)
    assert fake.calls == file_preview.PREWARM_MAX_FILES


async def test_same_mtime_different_content_is_reconverted(env):
    import os

    paths, fake = env
    f = paths.uploads / "a.docx"
    st = f.stat()
    await file_preview.office_to_pdf(SID, "uploads/a.docx")
    f.write_bytes(b"other")  # 同长度之外的内容变化,mtime 还原
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    await file_preview.office_to_pdf(SID, "uploads/a.docx")
    assert fake.calls == 2


async def test_file_changed_during_conversion_is_not_cached(env):
    paths, fake = env
    f = paths.uploads / "a.docx"
    orig = fake.run_isolated

    async def modify_midway(cmd, timeout=None):
        out = await orig(cmd, timeout)
        f.write_bytes(b"edited while converting")
        return out

    fake.run_isolated = modify_midway
    await file_preview.office_to_pdf(SID, "uploads/a.docx")
    assert not file_preview._CACHE
