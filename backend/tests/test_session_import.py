"""备份包的 checkpoint 往返、导入校验(路径、sha256、白名单)与 /conversations/import。"""

import io
import json
import sqlite3
import zipfile

import aiosqlite
import ormsgpack
import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app import app
from app.agents import checkpointers
from app.api.routes import conversations
from app.config import settings
from app.db import close_db, init_db, repository
from app.sandbox.session_paths import SessionPaths
from app.services import checkpoint_transfer as ct
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
