"""会话备份包:zip 内容、清单、符号链接与上限、路由。"""

import hashlib
import io
import json
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from app.api.routes import stream
from app.config import settings
from app.db import close_db, init_db, repository
from app.sandbox.session_paths import SessionPaths
from app.services import session_export
from app.services.history import TurnRecorder, persist_assistant_turn, persist_user_turn


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    await init_db()
    yield
    await close_db()


@pytest.fixture
def paths(tmp_path):
    p = SessionPaths.create("s", tmp_path / "sessions")
    for d in ("uploads", "output", "scratchpad"):
        (p.session_root / d).mkdir(parents=True)
    return p


def _doc(refs: list[str] = ()):
    return {
        "id": "s",
        "title": "t",
        "messages": [
            {
                "id": 1,
                "role": "user",
                "attachments": [
                    {"file_name": "x", "container_path": f"/workspace/{r}"} for r in refs
                ],
            }
        ],
    }


def _open(dest):
    zf = zipfile.ZipFile(dest)
    return zf, json.loads(zf.read("manifest.json"))


def test_archive_contains_files_with_matching_digest(paths, tmp_path):
    root = paths.session_root
    (root / "uploads" / "a.txt").write_bytes(b"hello")
    (root / "output" / "sub").mkdir()
    (root / "output" / "sub" / "b.bin").write_bytes(b"\x00\x01")
    (root / "scratchpad" / "tmp").write_bytes(b"no")
    dest = tmp_path / "out.zip"
    manifest = session_export.build_session_archive(paths, _doc(), dest)
    zf, on_disk = _open(dest)
    assert manifest == on_disk
    assert sorted(zf.namelist()) == sorted(
        ["conversation.json", "manifest.json", "files/uploads/a.txt", "files/output/sub/b.bin"]
    )
    entry = next(f for f in manifest["files"] if f["path"] == "uploads/a.txt")
    assert entry == {
        "path": "uploads/a.txt",
        "size": 5,
        "sha256": hashlib.sha256(b"hello").hexdigest(),
    }
    assert zf.read("files/uploads/a.txt") == b"hello"
    assert manifest["format_version"] == session_export.FORMAT_VERSION


def test_missing_references_are_listed(paths, tmp_path):
    (paths.session_root / "uploads" / "a.txt").write_bytes(b"1")
    doc = _doc(["uploads/a.txt", "output/gone.txt", "scratchpad/x"])
    manifest = session_export.build_session_archive(paths, doc, tmp_path / "o.zip")
    assert manifest["missing"] == ["output/gone.txt"]


def test_symlinks_are_not_followed(paths, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top secret")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "f").write_bytes(b"leak")
    (paths.session_root / "output" / "link").symlink_to(secret)
    (paths.session_root / "output" / "dirlink").symlink_to(outside, target_is_directory=True)
    dest = tmp_path / "o.zip"
    manifest = session_export.build_session_archive(paths, _doc(), dest)
    assert manifest["files"] == []
    assert not any(n.startswith("files/") for n in zipfile.ZipFile(dest).namelist())


def test_size_limits_are_recorded_as_skipped(paths, tmp_path, monkeypatch):
    monkeypatch.setattr(session_export, "MAX_FILE_BYTES", 4)
    monkeypatch.setattr(session_export, "MAX_TOTAL_BYTES", 6)
    root = paths.session_root
    (root / "output" / "a").write_bytes(b"123")
    (root / "output" / "b").write_bytes(b"456")
    (root / "output" / "c").write_bytes(b"789")
    (root / "output" / "huge").write_bytes(b"12345")
    manifest = session_export.build_session_archive(paths, _doc(), tmp_path / "o.zip")
    assert [f["path"] for f in manifest["files"]] == ["output/a", "output/b"]
    reasons = {s["path"]: s["reason"] for s in manifest["skipped"]}
    assert set(reasons) == {"output/c", "output/huge"}
    assert "200 MB" in reasons["output/huge"] and "1 GB" in reasons["output/c"]
    # 被跳过的文件不应再被当成"缺失"
    manifest = session_export.build_session_archive(paths, _doc(["output/c"]), tmp_path / "o2.zip")
    assert manifest["missing"] == []


def test_file_count_limit(paths, tmp_path, monkeypatch):
    monkeypatch.setattr(session_export, "MAX_FILES", 2)
    for name in "abc":
        (paths.session_root / "output" / name).write_bytes(b"1")
    manifest = session_export.build_session_archive(paths, _doc(), tmp_path / "o.zip")
    assert len(manifest["files"]) == 2
    assert [s["path"] for s in manifest["skipped"]] == ["output/c"]


async def _seed(owner: str = ""):
    await repository.ensure_conversation("s", "你好", owner=owner)
    await persist_user_turn(
        "s",
        "p1",
        "你好",
        [{"file_name": "a.txt", "container_path": "/workspace/uploads/a.txt"}],
        owner,
    )
    r = TurnRecorder()
    r.record("ai.token", {"text": "嗨"})
    await persist_assistant_turn("s", "p1", r, "complete")


@pytest.fixture
def client_paths(paths, monkeypatch):
    monkeypatch.setattr(stream.SessionPaths, "create", staticmethod(lambda _sid: paths))
    return paths


async def test_export_endpoint_returns_zip(db, client_paths):
    (client_paths.session_root / "uploads" / "a.txt").write_bytes(b"hello")
    await _seed()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/files/export", params={"session_id": "s"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    conv = json.loads(zf.read("conversation.json"))
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert conv["messages"][0]["attachments"][0]["file_name"] == "a.txt"
    assert json.loads(zf.read("manifest.json"))["missing"] == []
    assert zf.read("files/uploads/a.txt") == b"hello"


async def test_export_endpoint_unknown_session_404(db, client_paths):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/files/export", params={"session_id": "nope"})
    assert r.status_code == 404


async def test_export_endpoint_blocked_while_running(db, client_paths, monkeypatch):
    await _seed()
    monkeypatch.setattr(stream, "get_active_prompt_id", lambda _sid: "p1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/files/export", params={"session_id": "s"})
    assert r.status_code == 409


async def test_export_endpoint_hides_other_owners_conversation(db, client_paths):
    await _seed(owner="someone-else")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/files/export", params={"session_id": "s"})
    assert r.status_code == 404


def test_precompressed_files_are_stored_not_deflated(tmp_path):
    import zipfile

    from app.services.session_export import _copy_into_zip

    (tmp_path / "a.docx").write_bytes(b"PK" + b"x" * 4096)
    (tmp_path / "a.txt").write_text("y" * 4096)
    dest = tmp_path / "out.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("a.docx", "a.txt"):
            _copy_into_zip(zf, f"files/{name}", tmp_path / name, 1 << 20)
    with zipfile.ZipFile(dest) as zf:
        types = {i.filename: i.compress_type for i in zf.infolist()}
        assert zf.read("files/a.docx") == b"PK" + b"x" * 4096
    assert types == {"files/a.docx": zipfile.ZIP_STORED, "files/a.txt": zipfile.ZIP_DEFLATED}


def test_sweep_removes_only_stale_archives(tmp_path, monkeypatch):
    import os
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    old = session_export.new_archive_path()
    fresh = session_export.new_archive_path()
    other = tmp_path / "unrelated.zip"
    other.write_bytes(b"")
    os.utime(old, (0, 0))
    os.utime(other, (0, 0))
    assert session_export.sweep_stale_archives() == 1
    assert not old.exists() and fresh.exists() and other.exists()
