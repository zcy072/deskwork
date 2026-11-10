"""备份包的 checkpoint 往返、导入校验(路径、sha256、白名单)与 /conversations/import。"""

import asyncio
import io
import json
import sqlite3
import threading
import zipfile

import aiosqlite
import ormsgpack
import pytest
from fastapi import HTTPException, UploadFile
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from starlette.requests import Request

from app import app
from app.agents import checkpointers
from app.api.routes import conversations
from app.config import settings
from app.db import close_db, init_db, repository
from app.runtime.sessions import ActiveRunError, sessions
from app.sandbox.session_paths import SessionPaths
from app.services import chat_operations as ops
from app.services import checkpoint_transfer as ct
from app.services import conversation_cleanup as cleanup
from app.services import session_export, session_import
from app.services.history import TurnRecorder, persist_assistant_turn, persist_user_turn


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    await init_db()
    yield
    await close_db()


@pytest.fixture
async def saver(tmp_path):
    conn = await aiosqlite.connect(tmp_path / "cp.sqlite")
    s = AsyncSqliteSaver(conn)
    await s.setup()
    yield s
    await conn.close()


async def _put(saver, thread, text="hi"):
    cfg = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    cp = empty_checkpoint()
    cp["channel_values"] = {"messages": [HumanMessage(text), AIMessage("yo")]}
    cfg = await saver.aput(cfg, cp, {"source": "input", "step": 0}, {})
    await saver.aput_writes(cfg, [("messages", [HumanMessage("w")])], "task1")
    return cfg["configurable"]["checkpoint_id"]


async def test_checkpoint_roundtrip(saver):
    cid = await _put(saver, "old")
    payload = await ct.export_thread(saver, "old")
    assert payload["payload_version"] == ct.PAYLOAD_VERSION
    parsed = ct.parse_payload(json.loads(json.dumps(payload)))
    await ct.import_thread(saver, "new", parsed)
    got = await saver.aget_tuple({"configurable": {"thread_id": "new", "checkpoint_ns": ""}})
    assert got.config["configurable"]["checkpoint_id"] == cid
    assert [m.content for m in got.checkpoint["channel_values"]["messages"]] == ["hi", "yo"]
    assert got.pending_writes and got.pending_writes[0][1] == "messages"
    # 原线程不受影响
    assert await saver.aget_tuple({"configurable": {"thread_id": "old", "checkpoint_ns": ""}})


async def test_export_is_none_for_non_sqlite_saver():
    from langgraph.checkpoint.memory import InMemorySaver

    assert await ct.export_thread(InMemorySaver(), "x") is None


def _ext_payload(module: str, name: str) -> bytes:
    inner = ormsgpack.packb([module, name, {"a": 1}])
    return ormsgpack.packb(ormsgpack.Ext(ct.EXT_CONSTRUCTOR_KW_ARGS, inner))


def _forged(raw: bytes, typ: str = "msgpack") -> dict:
    import base64

    return {
        "payload_version": 1,
        "checkpoints": [
            {
                "ns": "",
                "id": "c1",
                "parent_id": None,
                "type": typ,
                "checkpoint": base64.b64encode(raw).decode(),
                "metadata": None,
            }
        ],
        "writes": [],
    }


def test_parse_rejects_unlisted_types():
    with pytest.raises(ct.CheckpointRejected, match="白名单"):
        ct.parse_payload(_forged(_ext_payload("os", "system")))


def test_parse_accepts_safe_types():
    parsed = ct.parse_payload(
        _forged(_ext_payload("langchain_core.messages.human", "HumanMessage"))
    )
    assert parsed.ids == {"c1"}


@pytest.mark.parametrize("typ", ["pickle", "json"])
def test_parse_rejects_pickle_and_json(typ):
    with pytest.raises(ct.CheckpointRejected, match="序列化类型"):
        ct.parse_payload(_forged(b"x", typ))


def test_parse_rejects_bad_structure():
    for bad in (
        None,
        {},
        {"payload_version": 2},
        {"payload_version": 1, "checkpoints": [], "writes": []},
    ):
        with pytest.raises(ct.CheckpointRejected):
            ct.parse_payload(bad)
    forged = _forged(ormsgpack.packb({"ok": 1}))
    forged["writes"] = [
        {"ns": "", "checkpoint_id": "nope", "task_id": "t", "task_path": "", "idx": 0,
         "channel": "c", "type": "msgpack", "value": None}
    ]  # fmt: skip
    with pytest.raises(ct.CheckpointRejected, match="不存在的 checkpoint"):
        ct.parse_payload(forged)


async def test_import_failure_rolls_back(saver):
    await _put(saver, "old")
    parsed = ct.parse_payload(await ct.export_thread(saver, "old"))
    await ct.import_thread(saver, "new", parsed)
    with pytest.raises(sqlite3.IntegrityError):  # 主键冲突,整体回滚
        await ct.import_thread(saver, "new", parsed)
    n = await (
        await saver.conn.execute("SELECT count(*) FROM checkpoints WHERE thread_id='new'")
    ).fetchone()
    assert n[0] == 1


# ---------- 备份包校验 ----------


def _archive(tmp_path, *, files=None, manifest=None, document=None, checkpoint=None, extra=None):
    files = {"uploads/a.txt": b"hello"} if files is None else files
    import hashlib

    man = {
        "format_version": session_export.FORMAT_VERSION,
        "checkpoint": {
            "graph_version": session_export.GRAPH_VERSION,
            "versions": session_export.checkpoint_versions(),
        },
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
            {"role": "user", "content": "q", "parts": [], "checkpoint_id": "",
             "attachments": [{"file_name": "a.txt", "container_path": "/workspace/uploads/a.txt"}]},
            {"role": "assistant", "content": "a", "parts": [{"type": "text", "text": "a"}]},
        ],
    }  # fmt: skip
    dest = tmp_path / "in.zip"
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(man))
        zf.writestr("conversation.json", json.dumps(doc))
        if checkpoint is not None:
            zf.writestr("checkpoint.json", json.dumps(checkpoint))
        for p, b in files.items():
            zf.writestr(f"files/{p}", b)
        for n, b in (extra or {}).items():
            zf.writestr(n, b)
    return dest


def test_inspect_ok_without_checkpoint(tmp_path):
    plan = session_import.inspect_archive(_archive(tmp_path))
    assert plan.title == "标题" and len(plan.messages) == 2
    assert plan.checkpoint is None and plan.warnings
    assert all(m["checkpoint_id"] is None for m in plan.messages)


async def test_inspect_warns_when_message_checkpoint_missing(tmp_path, saver):
    cid = await _put(saver, "old")
    payload = await ct.export_thread(saver, "old")
    doc = {
        "id": "old",
        "title": "t",
        "messages": [
            {"role": "user", "content": "q1", "parts": [], "checkpoint_id": cid},
            {"role": "assistant", "content": "a", "parts": []},
            {"role": "user", "content": "q2", "parts": [], "checkpoint_id": "gone"},
        ],
    }
    plan = session_import.inspect_archive(_archive(tmp_path, document=doc, checkpoint=payload))
    assert [m["checkpoint_id"] for m in plan.messages if m["role"] == "user"] == [cid, None]
    assert len(plan.warnings) == 1 and "1 条消息" in plan.warnings[0]


@pytest.mark.parametrize("kind", ["legacy", "graph", "dependency", "missing_dependency"])
async def test_incompatible_checkpoint_preserves_history_and_files(tmp_path, saver, kind):
    await _put(saver, "old")
    info = {
        "graph_version": session_export.GRAPH_VERSION,
        "versions": session_export.checkpoint_versions(),
    }
    if kind == "legacy":
        info.pop("graph_version")
    elif kind == "graph":
        info["graph_version"] += 1
    elif kind == "dependency":
        info["versions"]["langgraph"] = "0.0.0"
    else:
        info["versions"].pop("langchain-core")
    archive = _archive(
        tmp_path,
        checkpoint=await ct.export_thread(saver, "old"),
        manifest={"format_version": 2 if kind == "legacy" else 3, "checkpoint": info},
    )
    plan = session_import.inspect_archive(archive)
    assert plan.checkpoint is None
    assert len(plan.messages) == 2 and len(plan.files) == 1
    assert all(m["checkpoint_id"] is None for m in plan.messages)
    assert len(plan.warnings) == 1 and "未恢复执行上下文" in plan.warnings[0]


async def test_current_export_can_restore_checkpoint(tmp_path, saver):
    await _put(saver, "old")
    paths = SessionPaths.create("old", tmp_path / "sessions")
    paths.uploads.mkdir(parents=True)
    paths.output.mkdir()
    doc = {"id": "old", "title": "roundtrip", "messages": []}
    target = tmp_path / "export.zip"
    manifest = session_export.build_session_archive(
        paths, doc, target, checkpoint=await ct.export_thread(saver, "old")
    )
    assert manifest["checkpoint"]["graph_version"] == session_export.GRAPH_VERSION
    plan = session_import.inspect_archive(target)
    assert plan.checkpoint is not None and not plan.warnings


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


def test_inspect_rejects_unsupported_version(tmp_path):
    with pytest.raises(session_import.ImportRejected, match="版本"):
        session_import.inspect_archive(_archive(tmp_path, manifest={"format_version": 99}))


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
async def env(db, tmp_path, saver, monkeypatch):
    root = tmp_path / "sessions"
    mgr = FakeManager(root)
    monkeypatch.setattr(conversations, "CONTAINER_MANAGER", mgr)
    monkeypatch.setattr(cleanup, "CONTAINER_MANAGER", mgr)
    monkeypatch.setattr(cleanup, "PENDING_PURGE_DIR", tmp_path / "pending_purge")
    original = SessionPaths.create.__func__
    monkeypatch.setattr(
        SessionPaths, "create", classmethod(lambda cls, sid, *a, **k: original(cls, sid, root))
    )
    monkeypatch.setattr(checkpointers, "_saver", saver)
    return mgr, root, saver


async def _seed(saver, root):
    await repository.ensure_conversation("src", "你好")
    cid = await _put(saver, "src")
    await persist_user_turn(
        "src", "p1", "你好", [{"file_name": "a.txt", "container_path": "/workspace/uploads/a.txt"}]
    )
    await repository.set_user_checkpoint("src", "p1", cid)
    r = TurnRecorder()
    r.record("ai.token", {"text": "嗨"})
    await persist_assistant_turn("src", "p1", r, "complete")
    d = root / "src"
    (d / "uploads").mkdir(parents=True)
    (d / "output").mkdir()
    (d / "uploads" / "a.txt").write_bytes(b"hello")
    (d / "output" / "r.txt").write_bytes(b"result")
    return cid


async def test_export_then_import_resumes(env):
    mgr, root, saver = env
    cid = await _seed(saver, root)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        exp = await c.get("/files/export", params={"session_id": "src"})
        assert exp.status_code == 200
        names = zipfile.ZipFile(io.BytesIO(exp.content)).namelist()
        assert "checkpoint.json" in names
        r = await c.post("/conversations/import", files={"file": ("b.zip", exp.content)})
        assert r.status_code == 201, r.text
        body = r.json()
        new = body["id"]
        assert new != "src" and body["resumable"] is True and body["file_count"] == 2
        msgs = (await c.get(f"/conversations/{new}/messages")).json()
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[0]["rewindable"] is True
        assert msgs[0]["attachments"][0]["file_name"] == "a.txt"
        # 原会话仍在,没被覆盖
        assert (await c.get("/conversations/src/messages")).status_code == 200
    assert (root / new / "output" / "r.txt").read_bytes() == b"result"
    got = await saver.aget_tuple({"configurable": {"thread_id": new, "checkpoint_ns": ""}})
    assert got.config["configurable"]["checkpoint_id"] == cid


async def test_import_bad_archive_leaves_nothing(env, tmp_path):
    mgr, root, saver = env
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


async def test_import_malicious_checkpoint_rejected(env, tmp_path):
    mgr, root, saver = env
    arc = _archive(tmp_path, checkpoint=_forged(_ext_payload("os", "system")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/conversations/import", files={"file": ("b.zip", arc.read_bytes())})
    assert r.status_code == 400 and "白名单" in r.json()["detail"]
    n = await (await saver.conn.execute("SELECT count(*) FROM checkpoints")).fetchone()
    assert n[0] == 0


async def test_import_without_persistent_saver_keeps_messages(env, tmp_path, monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver

    monkeypatch.setattr(checkpointers, "_saver", InMemorySaver())
    mgr, root, saver = env
    arc = _archive(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/conversations/import", files={"file": ("b.zip", arc.read_bytes())})
    assert r.status_code == 201 and r.json()["resumable"] is False


async def _import_archive(archive):
    upload = UploadFile(file=io.BytesIO(archive.read_bytes()), filename="backup.zip")
    try:
        return await conversations.import_conversation(
            Request({"type": "http", "state": {}}), upload
        )
    finally:
        await upload.close()


async def _with_checkpoint(tmp_path, saver):
    await _put(saver, "original")
    return _archive(tmp_path, checkpoint=await ct.export_thread(saver, "original"))


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_import_failure_reconciles_durable_commit(
    env, tmp_path, monkeypatch, committed, failure
):
    mgr, root, saver = env
    archive = await _with_checkpoint(tmp_path, saver)
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
    checkpoint = await saver.aget_tuple({"configurable": {"thread_id": sid}})
    assert (checkpoint is not None) == committed
    assert (root / sid / "uploads" / "a.txt").exists() == committed
    assert (sid in mgr.purged) != committed
    assert not list(cleanup.PENDING_PURGE_DIR.iterdir())
    assert sessions.get(sid) is None


@pytest.mark.parametrize("after_commit", [False, True])
async def test_cancelled_commit_waiter_settles_before_releasing_gate(
    env, tmp_path, monkeypatch, after_commit
):
    mgr, root, saver = env
    archive = await _with_checkpoint(tmp_path, saver)
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
    assert await saver.aget_tuple({"configurable": {"thread_id": sid}})
    assert not mgr.purged and sessions.get(sid) is None


async def test_cancelled_extraction_drains_thread_and_shutdown_waits(env, tmp_path, monkeypatch):
    mgr, root, _ = env
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
    mgr, root, saver = env
    archive = await _with_checkpoint(tmp_path, saver)
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
    assert (await saver.aget_tuple({"configurable": {"thread_id": sid}}) is not None) == committed


async def test_failed_import_cleanup_is_retried(env, tmp_path, monkeypatch):
    mgr, root, saver = env
    archive = await _with_checkpoint(tmp_path, saver)
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
    assert await saver.aget_tuple({"configurable": {"thread_id": sid}}) is None
    monkeypatch.setattr(mgr, "purge_session", purge)
    assert await cleanup.retry_pending_purges() == 1
    assert not (root / sid).exists()


async def test_cancel_during_checkpoint_import_drains_then_removes_it(env, tmp_path, monkeypatch):
    mgr, root, saver = env
    archive = await _with_checkpoint(tmp_path, saver)
    original = conversations.import_thread
    entered, finish = asyncio.Event(), asyncio.Event()
    imported = []

    async def delayed_checkpoint(checkpointer, sid, parsed):
        imported.append(sid)
        entered.set()
        await finish.wait()
        await original(checkpointer, sid, parsed)

    monkeypatch.setattr(conversations, "import_thread", delayed_checkpoint)
    task = asyncio.create_task(_import_archive(archive))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not mgr.purged
        assert sessions.get(imported[0]) is not None
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    sid = imported[0]
    assert await repository.get_conversation(sid) is None
    assert await saver.aget_tuple({"configurable": {"thread_id": sid}}) is None
    assert not (root / sid).exists() and sessions.get(sid) is None


async def test_repeated_cancellation_during_rollback_keeps_gate(env, tmp_path, monkeypatch):
    mgr, root, saver = env
    archive = await _with_checkpoint(tmp_path, saver)
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
    mgr, root, _ = env
    await repository.ensure_conversation("empty", "failed before graph")
    await persist_user_turn("empty", "unstarted", "question", [])
    await mgr.get_or_create("empty")
    (root / "empty" / "output").mkdir()
    (root / "empty" / "output" / "file.txt").write_text("kept")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        exported = await client.get("/files/export", params={"session_id": "empty"})
        assert exported.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert "checkpoint.json" not in archive.namelist()
            assert not json.loads(archive.read("manifest.json"))["checkpoint"]["included"]
        imported = await client.post(
            "/conversations/import", files={"file": ("backup.zip", exported.content)}
        )
        assert imported.status_code == 201, imported.text
    result = imported.json()
    assert result["resumable"] is False
    assert (root / result["id"] / "output" / "file.txt").read_text() == "kept"
    messages = await repository.get_messages(result["id"])
    assert messages[0].content == "question" and messages[0].checkpoint_id is None


def test_legacy_empty_checkpoint_keeps_messages_and_files(tmp_path):
    archive = _archive(
        tmp_path,
        checkpoint={"payload_version": ct.PAYLOAD_VERSION, "checkpoints": [], "writes": []},
    )
    plan = session_import.inspect_archive(archive)
    assert plan.checkpoint is None and plan.graph_checkpoint_id is None
    assert len(plan.messages) == 2 and len(plan.files) == 1 and plan.warnings
    assert all(m["checkpoint_id"] is None for m in plan.messages)


@pytest.mark.parametrize(
    "change",
    [
        {"writes": [{}]},
        {"writes": None},
        {"checkpoints": None},
        {"payload_version": 2},
        {"unexpected": "field"},
    ],
)
def test_empty_checkpoint_compatibility_does_not_accept_malformed_payload(tmp_path, change):
    checkpoint = {"payload_version": ct.PAYLOAD_VERSION, "checkpoints": [], "writes": [], **change}
    with pytest.raises(session_import.ImportRejected):
        session_import.inspect_archive(_archive(tmp_path, checkpoint=checkpoint))


def test_empty_checkpoint_cannot_claim_a_selected_recovery_point(tmp_path):
    archive = _archive(
        tmp_path,
        checkpoint={"payload_version": ct.PAYLOAD_VERSION, "checkpoints": [], "writes": []},
        document={"messages": [], "graph_checkpoint_id": "missing"},
    )
    with pytest.raises(session_import.ImportRejected, match="恢复点"):
        session_import.inspect_archive(archive)
