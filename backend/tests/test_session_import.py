"""备份包导入校验(路径、sha256、版本)、导出→导入往返与 /conversations/import 的取消/回滚。"""

import asyncio
import io
import json
import threading
import zipfile

import pytest
from fastapi import HTTPException, UploadFile
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app import app
from app.api.routes import conversations
from app.config import settings
from app.db import close_db, init_db, repository
from app.runtime.sessions import ActiveRunError, sessions
from app.sandbox.session_paths import SessionPaths
from app.services import chat_operations as ops
from app.services import conversation_cleanup as cleanup
from app.services import session_export, session_import
from tests.history_seed import seed_legacy_turn, seed_turn


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    await init_db()
    yield
    await close_db()


# ---------- 备份包校验 ----------


def _archive(tmp_path, *, files=None, manifest=None, document=None, extra=None):
    files = {"uploads/a.txt": b"hello"} if files is None else files
    import hashlib

    man = {
        "format_version": session_export.FORMAT_VERSION,
        "files": [
            {"path": p, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
            for p, b in files.items()
        ],
        "missing": [],
    }
    man.update(manifest or {})
    doc = document or {
        "id": "old",
        "title": "标题",
        "messages": [
            {"role": "user", "content": "q", "parts": [],
             "attachments": [{"file_name": "a.txt", "container_path": "/workspace/uploads/a.txt"}]},
            {"role": "assistant", "content": "a", "parts": [{"type": "text", "text": "a"}]},
        ],
    }  # fmt: skip
    dest = tmp_path / "in.zip"
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(man))
        zf.writestr("conversation.json", json.dumps(doc))
        for p, b in files.items():
            zf.writestr(f"files/{p}", b)
        for n, b in (extra or {}).items():
            zf.writestr(n, b)
    return dest


def test_inspect_ok(tmp_path):
    plan = session_import.inspect_archive(_archive(tmp_path))
    assert plan.title == "标题" and len(plan.messages) == 2 and len(plan.files) == 1
    assert all("checkpoint_id" not in m for m in plan.messages)


def test_inspect_rejects_checkpoint_entry(tmp_path):
    # 本版本备份不含 checkpoint;带 checkpoint.json 的包按非本版本处理,不静默丢弃
    arc = _archive(tmp_path, extra={"checkpoint.json": b"{}"})
    with pytest.raises(session_import.ImportRejected, match="checkpoint"):
        session_import.inspect_archive(arc)


@pytest.mark.parametrize(
    "bad",
    [
        "../x",
        "/etc/passwd",
        "output/../uploads/x",
        "scratchpad/x",
        "output",
        "output//x",
        "output/./x",
        "a\\b",
    ],
)
def test_inspect_rejects_bad_paths(tmp_path, bad):
    arc = _archive(
        tmp_path, files={}, manifest={"files": [{"path": bad, "size": 1, "sha256": "0" * 64}]}
    )
    with pytest.raises(session_import.ImportRejected):
        session_import.inspect_archive(arc)


@pytest.mark.parametrize("version", [1, 4, 99])
def test_inspect_rejects_unsupported_version(tmp_path, version):
    with pytest.raises(session_import.ImportRejected, match="版本"):
        session_import.inspect_archive(_archive(tmp_path, manifest={"format_version": version}))


def test_inspect_rejects_not_a_backup(tmp_path):
    p = tmp_path / "x.zip"
    p.write_bytes(b"not a zip")
    with pytest.raises(session_import.ImportRejected):
        session_import.inspect_archive(p)
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("hello.txt", "x")
    with pytest.raises(session_import.ImportRejected, match="manifest"):
        session_import.inspect_archive(p)


def test_inspect_rejects_listed_file_absent(tmp_path):
    arc = _archive(tmp_path, files={})
    man = json.loads(zipfile.ZipFile(arc).read("manifest.json"))
    man["files"] = [{"path": "output/x", "size": 1, "sha256": "0" * 64}]
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("manifest.json", json.dumps(man))
        zf.writestr("conversation.json", json.dumps({"messages": []}))
    with pytest.raises(session_import.ImportRejected, match="缺少"):
        session_import.inspect_archive(arc)


def test_inspect_rejects_bad_role_and_size_limit(tmp_path, monkeypatch):
    doc = {"messages": [{"role": "system", "content": "x"}]}
    with pytest.raises(session_import.ImportRejected, match="角色"):
        session_import.inspect_archive(_archive(tmp_path, document=doc))
    monkeypatch.setattr(settings, "SESSION_MAX_STORAGE_MB", 0)  # 0 = 不限
    session_import.inspect_archive(_archive(tmp_path))
    monkeypatch.setattr(settings, "SESSION_MAX_STORAGE_MB", 1)
    big = _archive(tmp_path, files={"output/big": b"x" * (2 * 1024 * 1024)})
    with pytest.raises(session_import.ImportRejected, match="存储上限"):
        session_import.inspect_archive(big)


@pytest.mark.parametrize(
    "msg",
    [
        {"role": "assistant", "parts": ["x"]},
        {"role": "assistant", "parts": [{"text": "no type"}]},
        {"role": "assistant", "parts": [{"type": "files", "files": 5}]},
        {"role": "assistant", "parts": [{"type": "files", "files": ["a"]}]},
        {
            "role": "assistant",
            "parts": [{"type": "text", "text": "a"}],
            "usage": {"input_tokens": "abc"},
        },
        {"role": "assistant", "parts": [], "usage": {"by_model": {"m": {"input_tokens": -1}}}},
    ],
)
def test_inspect_rejects_malformed_parts_and_usage(tmp_path, msg):
    # 这些结构导入后会让读消息、汇总文件或用量的接口 500,整个会话打不开
    with pytest.raises(session_import.ImportRejected, match="parts / usage"):
        session_import.inspect_archive(_archive(tmp_path, document={"messages": [msg]}))


def test_extract_detects_tampered_content(tmp_path):
    arc = _archive(tmp_path)
    plan = session_import.inspect_archive(arc)
    plan.files[0]["sha256"] = "0" * 64
    paths = SessionPaths.create("n", tmp_path / "sessions")
    paths.session_root.mkdir(parents=True)
    with pytest.raises(session_import.ImportRejected, match="sha256"):
        session_import.extract_files(arc, plan, paths)


def test_extract_writes_files(tmp_path):
    arc = _archive(tmp_path, files={"uploads/a.txt": b"hello", "output/d/b.bin": b"\x00\x01"})
    plan = session_import.inspect_archive(arc)
    paths = SessionPaths.create("n", tmp_path / "sessions")
    paths.session_root.mkdir(parents=True)
    session_import.extract_files(arc, plan, paths)
    assert (paths.session_root / "uploads" / "a.txt").read_bytes() == b"hello"
    assert (paths.session_root / "output" / "d" / "b.bin").read_bytes() == b"\x00\x01"


def test_extract_declared_size_smaller_than_actual(tmp_path):
    arc = _archive(tmp_path)
    plan = session_import.inspect_archive(arc)
    plan.files[0]["size"] = 2
    paths = SessionPaths.create("n", tmp_path / "sessions")
    paths.session_root.mkdir(parents=True)
    with pytest.raises(session_import.ImportRejected, match="更大"):
        session_import.extract_files(arc, plan, paths)


# ---------- 端到端:导出 -> 导入 ----------


class FakeManager:
    def __init__(self, root):
        self.root = root
        self.purged = []

    async def get_or_create(self, sid):
        SessionPaths.create(sid, self.root).session_root.mkdir(parents=True, exist_ok=True)

    async def purge_session(self, sid):
        import shutil

        self.purged.append(sid)
        shutil.rmtree(SessionPaths.create(sid, self.root).session_root, ignore_errors=True)


@pytest.fixture
async def env(db, tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    mgr = FakeManager(root)
    monkeypatch.setattr(conversations, "CONTAINER_MANAGER", mgr)
    monkeypatch.setattr(cleanup, "CONTAINER_MANAGER", mgr)
    monkeypatch.setattr(cleanup, "PENDING_PURGE_DIR", tmp_path / "pending_purge")
    original = SessionPaths.create.__func__
    monkeypatch.setattr(
        SessionPaths, "create", classmethod(lambda cls, sid, *a, **k: original(cls, sid, root))
    )
    return mgr, root


async def _seed(root):
    await seed_turn(
        "src",
        "p1",
        "你好",
        "嗨",
        files=[{"file_name": "a.txt", "container_path": "/workspace/uploads/a.txt"}],
    )
    await repository.set_user_checkpoint("src", "p1", "cp-src")
    d = root / "src"
    (d / "uploads").mkdir(parents=True)
    (d / "output").mkdir()
    (d / "uploads" / "a.txt").write_bytes(b"hello")
    (d / "output" / "r.txt").write_bytes(b"result")


async def test_export_then_import_roundtrip(env):
    mgr, root = env
    await _seed(root)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        exp = await c.get("/files/export", params={"session_id": "src"})
        assert exp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exp.content)) as zf:
            assert "checkpoint.json" not in zf.namelist()
            manifest = json.loads(zf.read("manifest.json"))
            document = json.loads(zf.read("conversation.json"))
        assert "checkpoint" not in manifest
        assert "graph_checkpoint_id" not in document
        assert all("checkpoint_id" not in m for m in document["messages"])
        r = await c.post("/conversations/import", files={"file": ("b.zip", exp.content)})
        assert r.status_code == 201, r.text
        body = r.json()
        new = body["id"]
        assert new != "src" and body["file_count"] == 2 and "resumable" not in body
        msgs = (await c.get(f"/conversations/{new}/messages")).json()
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[0]["rewindable"] is False
        assert msgs[0]["attachments"][0]["file_name"] == "a.txt"
        # 原会话仍在,没被覆盖
        assert (await c.get("/conversations/src/messages")).status_code == 200
    assert (root / new / "output" / "r.txt").read_bytes() == b"result"
    conv = await repository.get_conversation(new)
    assert conv.graph_checkpoint_id is None
    assert all(m.checkpoint_id is None for m in await repository.get_messages(new))


async def test_import_bad_archive_leaves_nothing(env, tmp_path):
    mgr, root = env
    arc = _archive(tmp_path)
    data = bytearray(arc.read_bytes())
    # 破坏文件内容使 sha256 对不上
    i = data.rfind(b"hello")
    data[i : i + 5] = b"HELLO"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/conversations/import", files={"file": ("b.zip", bytes(data))})
        assert r.status_code == 400
        assert (await c.get("/conversations")).json() == []
    assert mgr.purged and not any(p.name == mgr.purged[0] for p in root.iterdir())


async def _import_archive(archive):
    upload = UploadFile(file=io.BytesIO(archive.read_bytes()), filename="backup.zip")
    try:
        return await conversations.import_conversation(
            Request({"type": "http", "state": {}}), upload
        )
    finally:
        await upload.close()


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_import_failure_reconciles_durable_commit(
    env, tmp_path, monkeypatch, committed, failure
):
    mgr, root = env
    archive = _archive(tmp_path)
    commit = repository.import_conversation
    imported = []

    async def failing_commit(sid, *args, **kwargs):
        imported.append(sid)
        if committed:
            await commit(sid, *args, **kwargs)
        raise failure("interrupted commit response")

    monkeypatch.setattr(repository, "import_conversation", failing_commit)
    with pytest.raises(failure):
        await _import_archive(archive)
    sid = imported[0]
    assert (await repository.get_conversation(sid) is not None) == committed
    assert (root / sid / "uploads" / "a.txt").exists() == committed
    assert (sid in mgr.purged) != committed
    assert not list(cleanup.PENDING_PURGE_DIR.iterdir())
    assert sessions.get(sid) is None


@pytest.mark.parametrize("after_commit", [False, True])
async def test_cancelled_commit_waiter_settles_before_releasing_gate(
    env, tmp_path, monkeypatch, after_commit
):
    mgr, root = env
    archive = _archive(tmp_path)
    commit = repository.import_conversation
    entered, finish = asyncio.Event(), asyncio.Event()
    imported = []

    async def slow_commit(sid, *args, **kwargs):
        imported.append(sid)
        if after_commit:
            await commit(sid, *args, **kwargs)
        entered.set()
        await finish.wait()
        if not after_commit:
            await commit(sid, *args, **kwargs)

    monkeypatch.setattr(repository, "import_conversation", slow_commit)
    task = asyncio.create_task(_import_archive(archive))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not mgr.purged
            with pytest.raises(ActiveRunError):
                sessions.reserve(imported[0])
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    sid = imported[0]
    assert await repository.get_conversation(sid) is not None
    assert (root / sid / "uploads" / "a.txt").read_bytes() == b"hello"
    assert not mgr.purged and sessions.get(sid) is None


async def test_cancelled_extraction_drains_thread_and_shutdown_waits(env, tmp_path, monkeypatch):
    mgr, root = env
    archive = _archive(
        tmp_path, files={"output/first.txt": b"first", "output/nested/late.txt": b"late"}
    )
    entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
    make_dir, extract = session_import._make_dir, session_import.extract_files

    def delayed_dir(path, mode):
        if path.name == "nested":
            entered.set()
            assert resume.wait(5)
        make_dir(path, mode)

    def watched_extract(*args):
        try:
            extract(*args)
        finally:
            finished.set()

    monkeypatch.setattr(session_import, "_make_dir", delayed_dir)
    monkeypatch.setattr(session_import, "extract_files", watched_extract)
    task = asyncio.create_task(_import_archive(archive))
    shutdown = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not mgr.purged and not finished.is_set()
        shutdown = asyncio.create_task(ops.shutdown_runs())
        await asyncio.sleep(0)
        assert not shutdown.done()
    finally:
        resume.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    if shutdown:
        await asyncio.wait_for(shutdown, 2)
    assert finished.is_set() and mgr.purged
    assert not list(root.iterdir()) and not sessions.active()
    assert await repository.list_conversations() == []


async def test_shutdown_tracks_import_from_upload_spooling(env, tmp_path, monkeypatch):
    archive = _archive(tmp_path)
    entered, finish = asyncio.Event(), asyncio.Event()
    spool = conversations._spool_upload

    async def slow_spool(file):
        entered.set()
        await finish.wait()
        return await spool(file)

    monkeypatch.setattr(conversations, "_spool_upload", slow_spool)
    task = asyncio.create_task(_import_archive(archive))
    await asyncio.wait_for(entered.wait(), 2)
    shutdown = asyncio.create_task(ops.shutdown_runs())
    await asyncio.sleep(0)
    try:
        assert not shutdown.done()
        with pytest.raises(HTTPException) as exc:
            await _import_archive(archive)
        assert exc.value.status_code == 409
    finally:
        finish.set()
    result = await asyncio.wait_for(task, 2)
    await asyncio.wait_for(shutdown, 2)
    assert await repository.get_conversation(result["id"]) is not None
    assert not sessions.active()


@pytest.mark.parametrize("committed", [False, True])
async def test_uncertain_import_preserves_resources_until_startup_reconciliation(
    env, tmp_path, monkeypatch, committed
):
    mgr, root = env
    archive = _archive(tmp_path)
    commit, get = repository.import_conversation, repository.get_conversation
    imported = []

    async def uncertain_commit(sid, *args, **kwargs):
        imported.append(sid)
        if committed:
            await commit(sid, *args, **kwargs)
        raise RuntimeError("commit response lost")

    async def unavailable(*args, **kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr(repository, "import_conversation", uncertain_commit)
    monkeypatch.setattr(repository, "get_conversation", unavailable)
    with pytest.raises(RuntimeError, match="commit response lost"):
        await _import_archive(archive)
    sid = imported[0]
    assert not mgr.purged
    assert (root / sid / "uploads" / "a.txt").exists()
    assert (cleanup.PENDING_PURGE_DIR / sid).exists()
    monkeypatch.setattr(repository, "get_conversation", get)
    await cleanup.retry_pending_purges()
    assert not (cleanup.PENDING_PURGE_DIR / sid).exists()
    assert (root / sid).exists() == committed


async def test_failed_import_cleanup_is_retried(env, tmp_path, monkeypatch):
    mgr, root = env
    archive = _archive(tmp_path)
    purge = mgr.purge_session

    async def fail(*args, **kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(repository, "import_conversation", fail)
    monkeypatch.setattr(mgr, "purge_session", fail)
    with pytest.raises(RuntimeError):
        await _import_archive(archive)
    markers = list(cleanup.PENDING_PURGE_DIR.iterdir())
    assert len(markers) == 1
    sid = markers[0].name
    assert (root / sid).exists()
    monkeypatch.setattr(mgr, "purge_session", purge)
    assert await cleanup.retry_pending_purges() == 1
    assert not (root / sid).exists()


async def test_repeated_cancellation_during_rollback_keeps_gate(env, tmp_path, monkeypatch):
    mgr, root = env
    archive = _archive(tmp_path)
    purge = mgr.purge_session
    entered, finish = asyncio.Event(), asyncio.Event()
    imported = []

    async def fail_commit(sid, *args, **kwargs):
        imported.append(sid)
        raise asyncio.CancelledError

    async def slow_purge(sid):
        entered.set()
        await finish.wait()
        await purge(sid)

    monkeypatch.setattr(repository, "import_conversation", fail_commit)
    monkeypatch.setattr(mgr, "purge_session", slow_purge)
    task = asyncio.create_task(_import_archive(archive))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            with pytest.raises(ActiveRunError):
                sessions.reserve(imported[0])
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert not (root / imported[0]).exists()
    assert not list(cleanup.PENDING_PURGE_DIR.iterdir())
    assert not sessions.active()


async def test_conversation_without_graph_survives_export_import(env):
    mgr, root = env
    await seed_legacy_turn("empty", "unstarted", "question")
    await mgr.get_or_create("empty")
    (root / "empty" / "output").mkdir()
    (root / "empty" / "output" / "file.txt").write_text("kept")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        exported = await client.get("/files/export", params={"session_id": "empty"})
        assert exported.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert "checkpoint.json" not in archive.namelist()
        imported = await client.post(
            "/conversations/import", files={"file": ("backup.zip", exported.content)}
        )
        assert imported.status_code == 201, imported.text
    result = imported.json()
    assert (root / result["id"] / "output" / "file.txt").read_text() == "kept"
    messages = await repository.get_messages(result["id"])
    assert messages[0].content == "question" and messages[0].checkpoint_id is None
