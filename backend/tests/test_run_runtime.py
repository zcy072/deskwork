"""Durable acceptance and failure recovery through the real application runner."""

import asyncio

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.context import RunContext
from app.config import settings
from app.db import close_db, init_db, repository, runs
from app.runtime.sessions import ActiveRunError, SessionRegistry, file_io, sessions
from app.services import chat_operations as ops
from app.services import rewind
from tests.test_ask_user import ScriptedModel


@pytest.fixture
async def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "db"))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    await init_db()
    graph = create_agent(
        model=ScriptedModel(script=[AIMessage(content="answer")]),
        tools=[],
        checkpointer=InMemorySaver(),
        context_schema=RunContext,
    )
    events = []

    async def push(pid, event, data):
        events.append((pid, event, data))

    async def noop(*args, **kwargs):
        pass

    monkeypatch.setattr(ops, "get_agent", lambda: graph)
    monkeypatch.setattr(ops, "_push", push)
    monkeypatch.setattr(ops, "mark_done", noop)
    monkeypatch.setattr(ops.CONTAINER_MANAGER, "snapshot_session", noop)
    yield graph, events
    await ops.shutdown_runs()
    await close_db()


async def test_accepted_request_survives_loss_of_process_idempotency_cache(environment):
    pid = await ops.start_chat("s", "question", [], owner="alice", client_request_id="request")
    handle = sessions.get("s")
    await handle.task
    await close_db()
    await init_db()
    assert (
        await ops.start_chat("s", "question", [], owner="alice", client_request_id="request") == pid
    )
    assert (await runs.get(pid)).status == "complete"
    assert (await runs.get(pid)).usage["llm_calls"] == 1
    assert [m.role for m in await repository.get_messages("s")] == ["user", "assistant"]
    with pytest.raises(runs.RequestIdConflictError):
        await ops.start_chat("s", "changed", [], owner="alice", client_request_id="request")
    with pytest.raises(repository.NotOwnerError):
        await ops.start_chat("s", "question", [], owner="bob", client_request_id="request")


async def test_stream_failure_does_not_skip_durable_terminal_save(environment, monkeypatch):
    async def broken(pid, event, data):
        if event == "ai.token":
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(ops, "_push", broken)
    pid = await ops.start_chat("s", "question", [], client_request_id="r")
    await sessions.get("s").task
    run = await runs.get(pid)
    assert (run.status, run.error_code) == ("error", "stream_unavailable")
    assert (await runs.message(pid)).content == "answer"
    assert sessions.get("s") is None


async def test_cancel_before_coroutine_entry_is_persisted(environment):
    pid = await ops.start_chat("s", "q", [])
    status, cancelled = await ops.cancel_chat("s")
    assert cancelled and status == "cancelled"
    assert (await runs.get(pid)).status == "cancelled"
    assert sessions.get("s") is None


async def test_empty_failure_has_persistent_error(environment, monkeypatch):
    def unavailable():
        raise RuntimeError("private database path")

    monkeypatch.setattr(ops, "get_agent", unavailable)
    pid = await ops.start_chat("s", "q", [])
    await sessions.get("s").task
    run = await runs.get(pid)
    assert run.status == "error" and "private" not in run.public_error
    assert (await runs.message(pid)).status == "error"


def test_session_operations_share_one_gate():
    registry = SessionRegistry()
    handle = registry.reserve("s")
    for operation in ("run", "files", "export"):
        with pytest.raises(ActiveRunError):
            registry.reserve("s", operation)
    registry.release(handle)
    with registry.operation("s", "export"):
        with pytest.raises(ActiveRunError):
            registry.reserve("s")


async def test_cancelled_file_waiter_keeps_gate_until_thread_stops():
    import threading

    began, finish = threading.Event(), threading.Event()
    registry = SessionRegistry()

    def slow_write():
        began.set()
        finish.wait(5)

    async def mutation():
        with registry.operation("s", "files"):
            await file_io(slow_write)

    task = asyncio.create_task(mutation())
    await asyncio.to_thread(began.wait, 5)
    task.cancel()
    with pytest.raises(ActiveRunError):
        registry.reserve("s")
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registry.get("s") is None


async def test_rewind_recovery_rolls_back_uncommitted_directory_swaps(environment, tmp_path):
    root = tmp_path / "workspace"
    for name in ("output", "scratchpad"):
        (root / name).mkdir(parents=True)
        (root / name / "a.txt").write_text("current")
    from app.sandbox.session_paths import SessionPaths

    paths = SessionPaths("s", root, "/workspace")
    snapshot = tmp_path / "snapshot"
    for name in ("output", "scratchpad"):
        (snapshot / name).mkdir(parents=True)
        (snapshot / name / "a.txt").write_text("old")
    op = rewind.RewindOperation(paths, "operation", 1, {"thread_id": "s"}, True)
    op.stage(snapshot)
    # A crash between the two directory replacements must also be recoverable.
    (op.root / "backup").mkdir()
    (root / "output").rename(op.root / "backup" / "output")
    (op.root / "stage" / "output").rename(root / "output")
    op.recover(False)
    assert (root / "output" / "a.txt").read_text() == "current"
    assert (root / "scratchpad" / "a.txt").read_text() == "current"
    assert not op.root.exists()


async def test_rewind_commit_keeps_new_directories(environment, tmp_path):
    from app.sandbox.session_paths import SessionPaths

    root, snapshot = tmp_path / "workspace", tmp_path / "snapshot"
    for name in ("output", "scratchpad"):
        (root / name).mkdir(parents=True)
        (root / name / "a").write_text("current")
        (snapshot / name).mkdir(parents=True)
        (snapshot / name / "a").write_text("old")
    op = rewind.RewindOperation(
        SessionPaths("s", root, "/workspace"), "op", 1, {"thread_id": "s"}, True
    )
    op.stage(snapshot)
    op.apply()
    op.recover(True)
    assert (root / "output" / "a").read_text() == "old"
    assert not op.root.exists()


@pytest.mark.parametrize("first", [False, True])
async def test_runner_rewinds_checkpoint_and_history_together(
    environment, tmp_path, monkeypatch, first
):
    from types import SimpleNamespace

    from app.sandbox.session_paths import SessionPaths

    graph = create_agent(
        model=ScriptedModel(script=[AIMessage(content=f"reply{i}") for i in range(4)]),
        tools=[],
        checkpointer=InMemorySaver(),
        context_schema=RunContext,
    )
    monkeypatch.setattr(ops, "get_agent", lambda: graph)
    paths = SessionPaths.create("s", tmp_path / "sessions")
    paths.session_root.mkdir(parents=True)
    monkeypatch.setattr(rewind.SessionPaths, "create", staticmethod(lambda sid: paths))

    async def noop():
        pass

    async def session(sid):
        return SimpleNamespace(paths=paths, workspace_lock=asyncio.Lock(), close=noop)

    monkeypatch.setattr(ops.CONTAINER_MANAGER, "get_or_create", session)
    for query in ("q1", "q2"):
        await ops.start_chat("s", query, [])
        await sessions.get("s").task
    users = [m for m in await repository.get_messages("s") if m.role == "user"]
    target = users[0 if first else 1]
    pid = await ops.start_chat(
        "s", "replacement", [], rewind_to_message_id=target.id, rewind_mode="conversation_only"
    )
    await sessions.get("s").task
    assert (await runs.get(pid)).status == "complete"
    cfg = await runs.graph_config("s")
    state = await graph.aget_state(cfg)
    texts = "\n".join(m.content for m in state.values["messages"])
    assert "q2" not in texts and "replacement" in texts
    assert ("q1" not in texts) == first
    assert (cfg["configurable"]["thread_id"] != "s") == first
    assert all(m.content != "q2" for m in await repository.get_messages("s"))


async def test_numbered_migration_backfills_legacy_runs(environment):
    from sqlalchemy import delete

    from app.db import get_session
    from app.db.models import SchemaRevision
    from app.services.history import TurnRecorder, persist_assistant_turn, persist_user_turn

    await persist_user_turn("legacy", "legacy-prompt", "old question", [])
    recorder = TurnRecorder()
    recorder.record("ai.token", {"text": "old answer"})
    await persist_assistant_turn("legacy", "legacy-prompt", recorder, "complete")
    await repository.set_run_outcome("legacy", "waiting")
    async with get_session() as session:
        await session.execute(delete(SchemaRevision))
        await session.commit()
    await close_db()
    await init_db()
    run = await runs.get("legacy-prompt")
    assert run.status == "waiting" and run.input_committed
    await close_db()
    await init_db()
    assert (await runs.get("legacy-prompt")).id == run.id


async def test_failed_graph_cleanup_keeps_session_closed_to_new_runs(environment, monkeypatch):
    graph, _ = environment

    class BrokenStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            raise RuntimeError("cannot confirm cleanup")

    class Agent:
        aget_state = graph.aget_state
        aupdate_state = graph.aupdate_state

        def astream(self, *args, **kwargs):
            return BrokenStream()

    monkeypatch.setattr(ops, "get_agent", lambda: Agent())
    pid = await ops.start_chat("s", "q", [])
    handle = sessions.get("s")
    await handle.task
    assert (await runs.get(pid)).error_code == "cleanup_failed"
    assert handle.phase == "recovery_required"
    with pytest.raises(ActiveRunError):
        await ops.start_chat("s", "new", [])
