"""start_chat:并发双击只放行一个;启动中途失败要把运行回执收尾;_run_agent 的落库顺序。"""

import asyncio

import pytest

from app.db import repository
from app.services import chat_operations as ops
from tests.history_seed import run_turn

_real_launch = ops._launch_agent_task


@pytest.fixture
async def stubbed(run_db, monkeypatch):
    calls = {"launched": 0, "finished": []}

    async def noop(*_a, **_k):
        return None

    async def upload(_sid, _uploads):
        await asyncio.sleep(0)
        return []

    async def finished(pid):
        calls["finished"].append(pid)

    monkeypatch.setattr(ops, "_push", noop)
    monkeypatch.setattr(ops, "upload_session_files", upload)
    monkeypatch.setattr(ops, "finish_existing_stream", finished)
    monkeypatch.setattr(
        ops,
        "_launch_agent_task",
        lambda *a, **k: calls.__setitem__("launched", calls["launched"] + 1),
    )
    return calls


async def test_concurrent_start_only_one_wins(stubbed):
    results = await asyncio.gather(
        ops.start_chat("s1", "hi", []), ops.start_chat("s1", "hi", []), return_exceptions=True
    )
    assert sum(isinstance(r, ops.ActiveRunError) for r in results) == 1
    assert stubbed["launched"] == 1


async def test_failure_after_accept_records_error_and_closes_stream(stubbed, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("launch failed")

    monkeypatch.setattr(ops, "_launch_agent_task", boom)
    with pytest.raises(RuntimeError):
        await ops.start_chat("s1", "hi", [])
    (prompt_id,) = stubbed["finished"]
    run = await ops.run_store.get(prompt_id)
    assert (run.status, run.error_code) == ("error", "admission_interrupted")
    assert ops.sessions.get("s1") is None
    assert ops.get_active_prompt_id("s1") is None


async def test_cancel_during_startup_waits_for_task(stubbed, monkeypatch):
    """取消落在快照/写库期间:不报 409,等任务建起来后取消它;期间 prompt_id 可查到。"""
    gate = asyncio.Event()
    started = asyncio.Event()
    real_accept = ops.run_store.accept

    async def slow_accept(*args, **kwargs):
        started.set()
        await gate.wait()
        return await real_accept(*args, **kwargs)

    async def run_forever(*_a, **_k):
        await asyncio.sleep(999)

    monkeypatch.setattr(ops.run_store, "accept", slow_accept)
    monkeypatch.setattr(ops, "_run_agent", run_forever)
    monkeypatch.setattr(ops, "_launch_agent_task", _real_launch)

    start = asyncio.create_task(ops.start_chat("s1", "hi", []))
    await started.wait()
    assert ops.get_active_prompt_id("s1") is not None  # 刷新页面能接回
    cancel = asyncio.create_task(ops.cancel_chat("s1"))
    await asyncio.sleep(0.1)
    assert not cancel.done()  # 在等任务建起来
    gate.set()
    prompt_id = await start
    _, cancelled = await cancel
    assert cancelled
    assert ops.get_active_prompt_id("s1") is None
    assert (await ops.run_store.get(prompt_id)).status == "cancelled"


async def test_upload_failure_leaves_history_untouched(stubbed, monkeypatch):
    order = []

    async def check(_sid, _mid):
        order.append("check")

    async def bad_upload(_sid, _uploads):
        order.append("upload")
        raise RuntimeError("manager down")

    async def reused(*_a):
        order.append("reused")
        return []

    monkeypatch.setattr(ops, "_check_rewind_target", check)
    monkeypatch.setattr(ops.rewind_service, "reused_files", reused)
    monkeypatch.setattr(ops, "upload_session_files", bad_upload)
    with pytest.raises(RuntimeError):
        await ops.start_chat(
            "s1", "hi", [], rewind_to_message_id=1, rewind_mode="conversation_only"
        )
    assert order == ["check", "upload"]  # 上传失败时不会读取/截断历史
    assert stubbed["launched"] == 0


async def test_started_marker_is_per_prompt(run_db, monkeypatch):
    class Boom:
        def __getattr__(self, _):
            raise RuntimeError("no agent")

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(ops, "get_agent", lambda: Boom())
    monkeypatch.setattr(ops, "_push", noop)
    other = ops.sessions.reserve("other")
    other.prompt_id = "other-prompt"
    await run_turn("s1", "p-new", "q")
    assert ops.sessions.get("other") is other
    ops.sessions.release(other)


class _Snapshot:
    interrupts = ()
    tasks = ()
    metadata = {}
    values = {"messages": []}
    config = {"configurable": {}}


def _agent(*chunks):
    class FakeAgent:
        async def aget_state(self, _c):
            return _Snapshot()

        async def astream(self, *_a, **_k):
            for chunk in chunks:
                yield chunk

    return FakeAgent()


@pytest.fixture
def runner(run_db, monkeypatch):
    events = []

    async def push(_pid, event, _data):
        events.append(event)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(ops, "_push", push)
    monkeypatch.setattr(ops, "_repair_cancelled_tool_calls", noop)
    monkeypatch.setattr(ops.CONTAINER_MANAGER, "snapshot_session", noop)
    return events


async def test_terminal_receipt_saved_before_stream_end(runner, monkeypatch):
    order = []
    real_save = ops.run_store.save

    async def save(*args, **kwargs):
        await real_save(*args, **kwargs)
        order.append(f"save:{args[5]}")

    async def finished(_pid):
        order.append("stream.end")

    monkeypatch.setattr(ops, "get_agent", lambda: _agent())
    monkeypatch.setattr(ops.run_store, "save", save)
    monkeypatch.setattr(ops, "finish_existing_stream", finished)
    await run_turn("s1", "p1", "q")
    assert order == ["save:complete", "stream.end"]


async def test_tool_result_checkpoints_running_turn(runner, monkeypatch):
    saved = []
    real_save = ops.run_store.save

    async def save(run_id, parts, *rest, **kwargs):
        saved.append((rest[3] if len(rest) > 3 else "running", [p["type"] for p in parts]))
        return await real_save(run_id, parts, *rest, **kwargs)

    chunk = ("custom", {"event": "tool.result", "data": {"id": "c1", "content": "ok"}})
    monkeypatch.setattr(ops, "get_agent", lambda: _agent(chunk))
    monkeypatch.setattr(ops.run_store, "save", save)
    monkeypatch.setattr(ops, "_normalize_custom", lambda d: (d["event"], d["data"]))
    run = await run_turn("s1", "p1", "q")
    assert saved[0] == ("running", ["tool_result"])
    assert saved[-1][0] == "complete" and run.status == "complete"
    message = await ops.run_store.message("p1")
    assert [p["type"] for p in message.parts] == ["tool_result"]


async def test_terminal_save_failure_is_reported_not_complete(runner, monkeypatch):
    """B2:终态写库失败时,不能报 complete,要有可见的错误事件。"""

    async def broken(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ops, "get_agent", lambda: _agent())
    monkeypatch.setattr(ops.run_store, "save", broken)
    run = await run_turn("s1", "p1", "q")
    assert run.status != "complete"
    assert (await repository.get_conversation("s1")).last_run_status != "complete"
    assert "error" in runner and runner[-1] == "stream.end"


async def test_startup_failure_does_not_rewind(stubbed, monkeypatch):
    """B5:回退是破坏性的,启动中途失败时历史和 checkpoint 必须原样保留。"""
    touched = []

    async def check(_sid, _mid):
        return None

    async def reused(*_a):
        return []

    async def prepare(*_a, **_k):
        touched.append("prepare")

    async def boom(*_a, **_k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(ops, "_check_rewind_target", check)
    monkeypatch.setattr(ops.rewind_service, "reused_files", reused)
    monkeypatch.setattr(ops.rewind_service, "prepare", prepare)
    monkeypatch.setattr(ops, "_push", boom)
    with pytest.raises(RuntimeError):
        await ops.start_chat(
            "s1", "hi", [], rewind_to_message_id=1, rewind_mode="conversation_only"
        )
    assert touched == [] and stubbed["launched"] == 0


async def test_delete_refused_while_starting(run_db):
    """B6:启动中的会话不能被删除,删除期间也不能启动。"""
    from app.services import conversation_cleanup as cc

    starting = ops.sessions.reserve("s2")
    try:
        with pytest.raises(ops.ActiveRunError):
            await cc.delete_conversation_data("s2")
    finally:
        ops.sessions.release(starting)

    deleting = ops.sessions.reserve("s3", "delete")
    try:
        with pytest.raises(ops.ActiveRunError):
            await ops.start_chat("s3", "hi", [])
    finally:
        ops.sessions.release(deleting)


async def test_same_request_id_returns_original_prompt(stubbed):
    first = await ops.start_chat("s1", "hi", [], client_request_id="r1")
    again = await ops.start_chat("s1", "hi", [], client_request_id="r1")
    assert again == first
    assert stubbed["launched"] == 1


async def test_request_id_with_different_content_is_rejected(stubbed):
    await ops.start_chat("s1", "hi", [], client_request_id="r2")
    with pytest.raises(ops.run_store.RequestIdConflictError):
        await ops.start_chat("s1", "other", [], client_request_id="r2")
    assert stubbed["launched"] == 1


async def test_request_id_is_scoped_to_session(stubbed):
    a = await ops.start_chat("s1", "hi", [], owner="a", client_request_id="r3")
    b = await ops.start_chat("s2", "hi", [], owner="b", client_request_id="r3")
    assert a != b
    assert stubbed["launched"] == 2


async def test_failed_start_is_not_remembered(stubbed, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(ops.run_store, "accept", boom)
    with pytest.raises(RuntimeError):
        await ops.start_chat("s1", "hi", [], client_request_id="r4")
    assert await ops.run_store.accepted("s1", "", "r4", "") is None


async def test_file_only_message_titles_conversation_with_file_names(stubbed):
    from app.services.file_operations import InMemoryUpload

    uploads = [InMemoryUpload("季度报表.xlsx", b"x"), InMemoryUpload("说明.docx", b"y")]
    await ops.start_chat("s-files", "   ", uploads)
    conv = await repository.get_conversation("s-files")
    assert conv.title == "季度报表.xlsx、说明.docx"
