"""Exercise recovery against real SQLite checkpoints and filesystem directories."""

import asyncio
import shutil
import time
import uuid
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agents.context import RunContext
from app.agents.recovery import _unanswered_interrupts
from app.agents.state import DeepAgentState
from app.config import settings
from app.db import close_db, init_db, repository, runs
from app.runtime.sessions import sessions
from app.sandbox.session_paths import SessionPaths
from app.services import chat_operations as ops
from app.tools import ask_user_input
from tests.test_ask_user import ScriptedModel


def ask():
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": "question-call",
                "name": "ask_user_input",
                "args": {"question": "Choose", "type": "single_select", "options": ["yes", "no"]},
            }
        ],
    )


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    await init_db()
    original_create = SessionPaths.create
    monkeypatch.setattr(
        SessionPaths,
        "create",
        staticmethod(lambda sid: original_create(sid, tmp_path / "sessions")),
    )
    events = []
    active_graph = []

    async def noop(*args, **kwargs):
        pass

    async def push(pid, event, data):
        events.append((pid, event, data))

    class Manager:
        def __init__(self):
            self.sessions = {}

        async def get_or_create(self, sid):
            if sid not in self.sessions:
                paths = SessionPaths.create(sid)
                for name in ("uploads", "output", "scratchpad"):
                    (paths.session_root / name).mkdir(parents=True, exist_ok=True)
                self.sessions[sid] = SimpleNamespace(
                    paths=paths, workspace_lock=asyncio.Lock(), close=noop
                )
            return self.sessions[sid]

        def stop_unconfirmed(self, sid):
            stop_state = getattr(self.sessions.get(sid), "stop_state", None)
            return bool(stop_state and stop_state.unconfirmed)

        async def snapshot_session(self, sid, pid):
            session = await self.get_or_create(sid)
            root = session.paths.session_root / "snapshots" / f"{time.time_ns()}_{pid}"
            for name in ("output", "scratchpad"):
                shutil.copytree(session.paths.session_root / name, root / name)
            return True

    monkeypatch.setattr(ops, "CONTAINER_MANAGER", Manager())
    monkeypatch.setattr(ops, "_push", push)
    monkeypatch.setattr(ops, "finish_existing_stream", noop)
    monkeypatch.setattr(ops, "get_agent", lambda: active_graph[0])
    async with AsyncExitStack() as stack:
        saver = await stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite"))
        )

        def graph(script, checkpoint=saver):
            result = create_agent(
                model=ScriptedModel(script=script),
                tools=[ask_user_input],
                state_schema=DeepAgentState,
                context_schema=RunContext,
                checkpointer=checkpoint,
            )
            active_graph[:] = [result]
            return result

        graph([AIMessage(content="done")])
        yield SimpleNamespace(graph=graph, saver=saver, events=events, root=tmp_path, stack=stack)
        await ops.shutdown_runs()
    await close_db()


async def send(text, **kwargs):
    pid = await ops.start_chat("s", text, [], **kwargs)
    await sessions.get("s").task
    return await runs.get(pid)


@pytest.mark.parametrize("name", ["view_image", "review_document"])
async def test_internal_vision_budget_stops_graph_before_next_main_call(runtime, monkeypatch, name):
    from app.services import doc_review, image_view
    from app.tools import image_tools, review_tools

    main = ScriptedModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "inspect-call",
                        "name": name,
                        "args": {"path": "/workspace/output/result.png", "description": "inspect"},
                    }
                ],
            ),
            AIMessage(content="must not run after the budget exception"),
        ]
    )
    vision = ScriptedModel(script=[AIMessage(content="vision response")])
    monkeypatch.setattr(settings, "AGENT_MAX_LLM_CALLS", 1)

    async def image(*args):
        return b"png", "image/png"

    async def pages(*args):
        return [b"jpg"]

    if name == "view_image":
        monkeypatch.setattr(image_tools, "vision_enabled", lambda: True)
        monkeypatch.setattr(image_tools, "load_image", image)
        service, tool = image_view, image_tools.view_image
    else:
        monkeypatch.setattr(review_tools, "render_pages", pages)
        service, tool = doc_review, review_tools.review_document
    monkeypatch.setattr(
        service, "get_named_model", lambda *a: (SimpleNamespace(model="vision"), vision)
    )
    graph = create_agent(
        model=main,
        tools=[tool],
        state_schema=DeepAgentState,
        context_schema=RunContext,
        checkpointer=runtime.saver,
    )
    monkeypatch.setattr(ops, "get_agent", lambda: graph)
    result = await send("inspect the image")
    # Budget is checked when a response ends. The vision call trips it; the
    # graph must stop there instead of making another main-model request.
    assert main.i == 1 and vision.i == 1
    assert result.usage["llm_calls"] == 2
    assert result.status == "error" and "调用上限" in result.public_error
    assert sessions.get("s") is None


async def test_preparing_recovery_retains_input_and_visible_failure(runtime):
    pid = str(uuid.uuid4())
    await runs.accept(
        pid, "s", "", {"text": "uncommitted question", "files": []}, None, "", "trace"
    )
    assert await runs.recover_interrupted(ops.get_agent()) == 1
    messages = await repository.get_messages("s")
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "uncommitted question"
    assert messages[1].status == "error"
    assert (await repository.get_conversation("s")).unread


async def test_running_recovery_preserves_persisted_progress(runtime):
    pid = str(uuid.uuid4())
    await runs.accept(pid, "s", "", {"text": "question", "files": []}, None, "", "trace")
    await runs.start(pid, "question", [], "")
    await runs.save(pid, [{"type": "text", "text": "partial"}], "partial", 7, {"llm_calls": 1})
    old = await runs.message(pid)
    await runs.recover_interrupted(ops.get_agent())
    current = await runs.message(pid)
    assert (current.id, current.parts, current.last_event_seq) == (old.id, old.parts, 7)
    assert current.status == "error"


async def test_failed_answer_does_not_reset_the_pending_turn(runtime):
    graph = runtime.graph([ask(), AIMessage(content="done")])
    first = await send("question")
    assert first.status == "waiting"
    state = await graph.aget_state(await runs.graph_config("s"))
    interrupt_id = _unanswered_interrupts(state)[0].id
    invalid = await send("yes", mode="resume", interrupt_id="stale-question")
    assert invalid.status == "error" and not invalid.input_committed
    resumed = await send("yes", mode="resume", interrupt_id=interrupt_id)
    assert resumed.status == "complete"
    assert resumed.turn_id == first.turn_id
    assert resumed.resume_of_run_id == first.id


async def test_rewinding_before_question_starts_a_new_turn(runtime):
    runtime.graph([AIMessage(content="first"), ask(), AIMessage(content="replacement")])
    await send("q1")
    waiting = await send("q2")
    target = next(
        m for m in await repository.get_messages("s") if m.role == "user" and m.content == "q2"
    )
    replacement = await send("new question", rewind_to_message_id=target.id)
    assert replacement.status == "complete"
    assert replacement.turn_id == replacement.id != waiting.turn_id
    assert replacement.resume_of_run_id is None


async def test_unpublished_snapshot_cannot_be_restored(runtime):
    from app.services.rewind import files_available, snapshot_available

    pid = str(uuid.uuid4())
    await runs.accept(pid, "s", "", {"text": "q", "files": []}, None, "", "trace")
    await ops.CONTAINER_MANAGER.snapshot_session("s", pid)
    assert snapshot_available(SessionPaths.create("s"), pid)
    assert not await files_available("s", pid)
    await runs.snapshot_finished(pid, True)
    assert await files_available("s", pid)
    assert not await files_available("other", pid)


async def test_shutdown_waits_for_file_operations(runtime):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def mutation():
        with sessions.operation("s", "files"):
            entered.set()
            await finish.wait()

    task = asyncio.create_task(mutation())
    await entered.wait()
    shutdown = asyncio.create_task(ops.shutdown_runs())
    await asyncio.sleep(0)
    assert not shutdown.done()
    finish.set()
    await asyncio.wait_for(asyncio.gather(task, shutdown), 2)


async def test_shutdown_cancels_admission_before_closing_dependencies(runtime, monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def slow_upload(*args):
        entered.set()
        await finish.wait()
        return []

    monkeypatch.setattr(ops, "upload_session_files", slow_upload)
    submission = asyncio.create_task(ops.start_chat("s", "q", []))
    await entered.wait()
    shutdown = asyncio.create_task(ops.shutdown_runs())
    await asyncio.sleep(0)
    assert not shutdown.done()
    finish.set()
    pid, _ = await asyncio.wait_for(asyncio.gather(submission, shutdown), 3)
    assert (await runs.get(pid)).status == "cancelled"


async def test_snapshot_cancellation_drains_remote_writer(tmp_path, monkeypatch):
    from app.sandbox import session_paths
    from app.sandbox.bash_handler import BashSession
    from app.sandbox.manager_client import ExecResult

    monkeypatch.setattr(session_paths, "SANDBOX_DATA_ROOT", tmp_path)
    paths = SessionPaths.create("s", tmp_path / "sessions")
    for name in ("output", "scratchpad"):
        (paths.session_root / name).mkdir(parents=True)
    entered, finish = asyncio.Event(), asyncio.Event()

    class Manager:
        async def run_exec(self, *args, **kwargs):
            entered.set()
            await finish.wait()
            return ExecResult(0, "", "")

    session = BashSession(paths, manager_client=Manager())
    task = asyncio.create_task(session.snapshot(str(uuid.uuid4())))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert session.workspace_lock.locked()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not session.workspace_lock.locked()


async def test_admission_cancel_after_commit_does_not_leave_orphan(runtime, monkeypatch):
    real_accept = runs.accept
    accepted = []

    async def commit_then_cancel(*args, **kwargs):
        record = await real_accept(*args, **kwargs)
        accepted.append(record.id)
        raise asyncio.CancelledError

    monkeypatch.setattr(runs, "accept", commit_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        await ops.start_chat("s", "q", [], client_request_id="cancelled-request")
    run = await runs.get(accepted[0])
    assert run.status == "cancelled"
    assert sessions.get("s") is None
    assert await ops.start_chat("s", "q", [], client_request_id="cancelled-request") == run.id


async def test_migration_retains_legacy_orphans_without_claiming_them(runtime):
    import sqlite3

    from sqlalchemy import select

    from app.db import get_session
    from app.db.models import Message

    await close_db()
    with sqlite3.connect(runtime.root / "data" / "deskwork.sqlite") as db:
        db.execute("DELETE FROM schema_revisions")
        db.execute(
            "INSERT INTO messages (conversation_id,prompt_id,role,content,parts,status,created_at) "
            "VALUES ('deleted-session','orphan-prompt','user','retain me','[]','complete', '2026-01-01 00:00:00')"
        )
    await init_db()
    assert await runs.get("orphan-prompt") is None
    async with get_session() as db:
        orphan = await db.scalar(select(Message).where(Message.prompt_id == "orphan-prompt"))
        assert orphan.content == "retain me"


async def test_failed_answer_cannot_bypass_the_turn_budget(runtime, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MAX_LLM_CALLS", 1)
    graph = runtime.graph([ask(), AIMessage(content="done")])
    first = await send("question")
    state = await graph.aget_state(await runs.graph_config("s"))
    interrupt_id = _unanswered_interrupts(state)[0].id
    await send("yes", mode="resume", interrupt_id="stale-question")
    resumed = await send("yes", mode="resume", interrupt_id=interrupt_id)
    assert resumed.turn_id == first.turn_id
    assert resumed.status == "error" and "调用上限" in resumed.public_error


async def test_run_deadline_is_error_and_releases_a_quiescent_session(runtime, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_RUN_TIMEOUT_SECONDS", 0.1)
    original = ops.get_agent()

    class Agent:
        aget_state = original.aget_state
        aupdate_state = original.aupdate_state

        async def astream(self, *args, **kwargs):
            await asyncio.Event().wait()
            yield

    monkeypatch.setattr(ops, "get_agent", lambda: Agent())
    run = await send("q")
    assert run.status == "error" and run.error_code == "run_timeout"
    assert sessions.get("s") is None


async def test_legacy_prompt_collision_does_not_leak_another_conversations_snapshot(runtime):
    await repository.ensure_conversation("other", "other", "bob")
    await repository.save_assistant_message(
        "other", "shared", content="private-other", parts=[], status="complete"
    )
    await runs.accept("shared", "s", "alice", {"text": "own", "files": []}, None, "", "trace")
    await runs.start("shared", "own", [], "")
    await runs.save("shared", [{"type": "text", "text": "own"}], "own", 1, {}, "complete")
    assert (await runs.message("shared")).content == "own"
    assert await runs.for_messages(["shared"], "other") == {}


async def test_sdk_timeout_has_a_persistent_safe_error(runtime, monkeypatch):
    from httpx import Request
    from openai import APITimeoutError

    original = ops.get_agent()

    class Agent:
        aget_state = original.aget_state
        aupdate_state = original.aupdate_state

        async def astream(self, *args, **kwargs):
            raise APITimeoutError(request=Request("POST", "https://private.invalid"))
            yield

    monkeypatch.setattr(ops, "get_agent", lambda: Agent())
    run = await send("q")
    assert run.error_code == "model_timeout" and "private.invalid" not in run.public_error


async def test_legacy_checkpoint_lineage_excludes_abandoned_future_turns(runtime):
    import json

    graph = runtime.graph(
        [
            ask(),
            AIMessage(content="answer"),
            AIMessage(content="unrelated"),
            AIMessage(content="replacement"),
        ]
    )
    first = await send("question")
    state = await graph.aget_state(await runs.graph_config("s"))
    interrupt_id = _unanswered_interrupts(state)[0].id
    await send("yes", mode="resume", interrupt_id=interrupt_id)
    target = next(
        m for m in await repository.get_messages("s") if m.role == "user" and m.content == "yes"
    )
    unrelated = await send("unrelated question")
    assert unrelated.turn_id != first.turn_id
    async with runtime.saver.lock:
        rows = await (
            await runtime.saver.conn.execute(
                "SELECT checkpoint_id,metadata FROM checkpoints WHERE thread_id='s'"
            )
        ).fetchall()
        for cid, raw in rows:
            metadata = json.loads(raw)
            metadata.pop("run_id", None)
            await runtime.saver.conn.execute(
                "UPDATE checkpoints SET metadata=? WHERE thread_id='s' AND checkpoint_id=?",
                (json.dumps(metadata).encode(), cid),
            )
        await runtime.saver.conn.commit()
    replacement = await send(
        "no", mode="resume", interrupt_id=interrupt_id, rewind_to_message_id=target.id
    )
    assert replacement.status == "complete"
    assert replacement.turn_id == first.turn_id
    assert replacement.resume_of_run_id == first.id


async def test_shutdown_waits_for_delete_after_the_run_stopped(runtime):
    handle = sessions.reserve("s")
    handle.phase = "running"
    handle.ready.set()
    handle.task = asyncio.create_task(asyncio.sleep(0))
    await handle.task
    sessions.begin_delete("s")
    shutdown = asyncio.create_task(ops.shutdown_runs())
    await asyncio.sleep(0)
    assert not shutdown.done()
    sessions.release(handle)
    await asyncio.wait_for(shutdown, 2)


def test_delete_cannot_release_a_session_that_requires_recovery():
    from app.runtime.sessions import ActiveRunError, SessionRegistry

    registry = SessionRegistry()
    handle = registry.reserve("s")
    handle.phase = "recovery_required"
    with pytest.raises(ActiveRunError):
        registry.begin_delete("s")
    assert registry.get("s") is handle


async def test_answer_rewind_preserves_completed_parallel_tool_results(runtime):
    from langchain.tools import tool
    from langchain_core.messages import ToolMessage

    calls = []

    @tool
    async def record_effect() -> str:
        """Record an irreversible test side effect."""
        calls.append("called")
        return "effect completed"

    question = ask()
    question.tool_calls.append(
        {"id": "effect-call", "name": "record_effect", "args": {}, "type": "tool_call"}
    )
    graph = create_agent(
        model=ScriptedModel(
            script=[question, AIMessage(content="done"), AIMessage(content="changed")]
        ),
        tools=[ask_user_input, record_effect],
        state_schema=DeepAgentState,
        context_schema=RunContext,
        checkpointer=runtime.saver,
    )
    runtime.graph([AIMessage(content="unused")])
    # Use the same app accessor while retaining the fixture's owned checkpointer.
    from unittest.mock import patch

    with patch.object(ops, "get_agent", return_value=graph):
        first = await send("question")
        state = await graph.aget_state(await runs.graph_config("s"))
        iid = _unanswered_interrupts(state)[0].id
        await send("yes", mode="resume", interrupt_id=iid)
        target = next(
            m for m in await repository.get_messages("s") if m.role == "user" and m.content == "yes"
        )
        changed = await send("no", mode="resume", interrupt_id=iid, rewind_to_message_id=target.id)
        assert changed.status == "complete"
        assert calls == ["called"]
        state = await graph.aget_state(await runs.graph_config("s"))
        results = [m.content for m in state.values["messages"] if isinstance(m, ToolMessage)]
        assert "User answered: no" in results and "User answered: yes" not in results
        assert "effect completed" in results
        assert changed.turn_id == first.turn_id


async def test_rewinding_an_answer_can_refuse_previously_allowed_subagent_command(runtime):
    from unittest.mock import patch

    from langchain.tools import tool

    from app.tools.task_tools import create_task_tool

    calls = []

    @tool
    async def bash_tool(command: str) -> str:
        """Execute a test command."""
        calls.append(command)
        return "ok"

    from langchain_core.messages import ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class HelperModel(ScriptedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            results = [m for m in messages if isinstance(m, ToolMessage)]
            reply = (
                AIMessage(content=results[-1].content)
                if results
                else AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "shell-call",
                            "name": "bash_tool",
                            "args": {"command": "rm -f /workspace/output/a.txt"},
                        }
                    ],
                )
            )
            return ChatResult(generations=[ChatGeneration(message=reply)])

    delegated = create_task_tool(
        [bash_tool],
        [
            {
                "name": "helper",
                "description": "test",
                "system_prompt": "test",
                "tools": ["bash_tool"],
            }
        ],
        HelperModel(script=[]),
        DeepAgentState,
    )
    model = ScriptedModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "delegate",
                        "name": "task",
                        "args": {"description": "test", "subagent_type": "helper"},
                    }
                ],
            ),
            AIMessage(content="done"),
            AIMessage(content="changed"),
        ]
    )
    graph = create_agent(
        model=model,
        tools=[delegated],
        context_schema=RunContext,
        state_schema=DeepAgentState,
        checkpointer=runtime.saver,
    )
    with patch.object(ops, "get_agent", return_value=graph):
        first = await send("question")
        assert first.status == "waiting"
        state = await graph.aget_state(await runs.graph_config("s"))
        iid = _unanswered_interrupts(state)[0].id
        await send("允许", mode="resume", interrupt_id=iid)
        assert len(calls) == 1
        target = next(
            m
            for m in await repository.get_messages("s")
            if m.role == "user" and m.content == "允许"
        )
        changed = await send(
            "拒绝", mode="resume", interrupt_id=iid, rewind_to_message_id=target.id
        )
        assert changed.status == "complete"
        assert len(calls) == 1
        latest_state = await graph.aget_state(await runs.graph_config("s"))
        delegation = next(
            m
            for m in latest_state.values["messages"]
            if isinstance(m, ToolMessage) and m.tool_call_id == "delegate"
        )
        assert "拒绝" in delegation.content


async def test_failed_confirmation_fork_is_removed_before_releasing_workspace(runtime, monkeypatch):
    graph = runtime.graph([ask(), AIMessage(content="done")])
    first = await send("question")
    state = await graph.aget_state(await runs.graph_config("s"))
    iid = _unanswered_interrupts(state)[0].id
    await send("yes", mode="resume", interrupt_id=iid)
    before = await runs.graph_config("s")
    target = next(
        m for m in await repository.get_messages("s") if m.role == "user" and m.content == "yes"
    )
    real_fork = ops.fork_answered_interrupt
    destination = []

    async def fork_then_fail(*args):
        destination.append(args[3])
        await real_fork(*args)
        raise OSError("copy interrupted")

    monkeypatch.setattr(ops, "fork_answered_interrupt", fork_then_fail)
    failed = await send("no", mode="resume", interrupt_id=iid, rewind_to_message_id=target.id)
    assert failed.status == "error" and not failed.input_committed
    assert await runs.graph_config("s") == before
    assert [
        row async for row in runtime.saver.alist({"configurable": {"thread_id": destination[0]}})
    ] == []
    assert sessions.get("s") is None
    assert not list(SessionPaths.create("s").session_root.glob("operations/*/journal.json"))
    assert (await runs.get(first.id)).status == "waiting"


async def test_missing_upload_rejects_rewind_before_truncating_history(runtime):
    from app.services.history import persist_user_turn
    from app.services.rewind import RewindError

    await persist_user_turn(
        "s",
        "old",
        "q",
        [{"file_name": "gone.txt", "container_path": "/workspace/uploads/gone.txt"}],
    )
    target = (await repository.get_messages("s"))[0]
    await repository.set_user_checkpoint("s", "old", "")
    with pytest.raises(RewindError, match="gone.txt"):
        await ops.start_chat(
            "s", "replacement", [], rewind_to_message_id=target.id, rewind_mode="conversation_only"
        )
    assert [m.content for m in await repository.get_messages("s")] == ["q"]


async def test_restart_recovers_a_checkpoint_question_before_ui_snapshot(runtime):
    graph = runtime.graph([ask(), AIMessage(content="done")])
    pid = str(uuid.uuid4())
    await runs.accept(pid, "s", "", {"text": "q", "files": []}, None, "", "trace")
    await runs.start(pid, "q", [], "")
    await graph.ainvoke(
        {"messages": [{"role": "user", "content": "q"}]},
        {"configurable": {"thread_id": "s"}, "metadata": {"run_id": pid}},
        context=RunContext("s", "", pid, pid, "trace"),
    )
    assert await runs.message(pid) is None
    await runs.recover_interrupted(graph)
    recovered = await runs.get(pid)
    assert recovered.status == "waiting"
    message = await runs.message(pid)
    question = next(p for p in message.parts if p["type"] == "ask")
    assert question["question"] == "Choose"
    resumed = await send("yes", mode="resume", interrupt_id=question["interrupt_id"])
    assert resumed.status == "complete" and resumed.turn_id == pid
