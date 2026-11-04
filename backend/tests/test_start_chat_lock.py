"""start_chat:并发双击只放行一个;启动中途失败要把状态从 running 改回去。"""

import asyncio

import pytest

from app.services import chat_operations as ops

_real_launch = ops._launch_agent_task


@pytest.fixture
def stubbed(monkeypatch):
    calls = {"launched": 0, "status": []}

    async def get_status(_sid):
        await asyncio.sleep(0.01)  # 模拟 Redis 往返,给并发请求插队的机会
        return "complete"

    async def set_status(_sid, status):
        calls["status"].append(status)

    async def noop(*_a, **_k):
        return None

    async def upload(_sid, _uploads):
        return []

    monkeypatch.setattr(ops, "get_thread_status", get_status)
    monkeypatch.setattr(ops, "set_thread_status", set_status)
    monkeypatch.setattr(ops, "register_prompt", noop)
    monkeypatch.setattr(ops, "_push", noop)
    monkeypatch.setattr(ops, "persist_user_turn", noop)
    monkeypatch.setattr(ops, "upload_session_files", upload)

    async def done(pid):
        calls["done"].append(pid)

    calls["done"] = []
    monkeypatch.setattr(ops, "mark_done", done)
    monkeypatch.setattr(
        ops,
        "_launch_agent_task",
        lambda *a, **k: calls.__setitem__("launched", calls["launched"] + 1),
    )
    ops._starting_sessions.discard("s1")
    return calls


async def test_concurrent_start_only_one_wins(stubbed):
    results = await asyncio.gather(
        ops.start_chat("s1", "hi", []), ops.start_chat("s1", "hi", []), return_exceptions=True
    )
    assert sum(isinstance(r, ops.ActiveRunError) for r in results) == 1
    assert stubbed["launched"] == 1


async def test_failure_after_running_resets_status(stubbed, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(ops, "persist_user_turn", boom)
    with pytest.raises(RuntimeError):
        await ops.start_chat("s1", "hi", [])
    assert stubbed["status"] == ["running", "error"]
    assert "s1" not in ops._starting_sessions
    # 已推过事件的流要收尾(设过期),登记的 prompt_id 也要清掉
    assert len(stubbed["done"]) == 1
    assert ops.get_active_prompt_id("s1") is None


async def test_cancel_during_startup_waits_for_task(stubbed, monkeypatch):
    """取消落在快照/写库期间:不报 409,等任务建起来后取消它;期间 prompt_id 可查到。"""
    status = {"s1": "complete"}
    gate = asyncio.Event()
    started = asyncio.Event()

    async def get_status(sid):
        return status.get(sid)

    async def set_status(sid, s):
        status[sid] = s

    async def slow_persist(*_a, **_k):
        started.set()
        await gate.wait()

    async def run_forever(*_a, **_k):
        await asyncio.sleep(999)

    monkeypatch.setattr(ops, "get_thread_status", get_status)
    monkeypatch.setattr(ops, "set_thread_status", set_status)
    monkeypatch.setattr(ops, "persist_user_turn", slow_persist)
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
    assert prompt_id not in ops._active_prompt_ids.values()


async def test_upload_failure_does_not_rewind(stubbed, monkeypatch):
    order = []

    async def check(_sid, _mid):
        order.append("check")

    async def rewind(_sid, _mid):
        order.append("rewind")
        return []

    async def bad_upload(_sid, _uploads):
        order.append("upload")
        raise RuntimeError("manager down")

    monkeypatch.setattr(ops, "_check_rewind_target", check)
    monkeypatch.setattr(ops, "_rewind", rewind)
    monkeypatch.setattr(ops, "upload_session_files", bad_upload)
    with pytest.raises(RuntimeError):
        await ops.start_chat("s1", "hi", [], rewind_to_message_id=1)
    assert order == ["check", "upload"]  # 上传失败时历史和 checkpoint 原样保留


async def test_started_marker_is_per_prompt(monkeypatch):
    class Boom:
        def __getattr__(self, _):
            raise RuntimeError("no agent")

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(ops, "get_agent", lambda: Boom())
    monkeypatch.setattr(ops, "set_thread_status", noop)
    monkeypatch.setattr(ops, "_push", noop)
    monkeypatch.setattr(ops, "persist_assistant_turn", noop)
    monkeypatch.setattr(ops, "mark_done", noop)
    ops._started_prompts.add("other-prompt")
    await ops._run_agent("p-new", "s1", "q")
    assert ops._started_prompts == {"other-prompt"}
    ops._started_prompts.discard("other-prompt")


async def test_assistant_turn_persisted_before_status_complete(monkeypatch):
    order = []

    async def status(_sid, s):
        order.append(f"status:{s}")

    async def persist(*_a, **_k):
        order.append("persist")
        return True

    async def noop(*_a, **_k):
        return None

    class Snapshot:
        interrupts = ()
        values = {"messages": []}
        config = {"configurable": {}}

    class FakeAgent:
        async def aget_state(self, _c):
            return Snapshot()

        async def astream(self, *_a, **_k):
            if False:
                yield None

    monkeypatch.setattr(ops, "get_agent", lambda: FakeAgent())
    monkeypatch.setattr(ops, "set_thread_status", status)
    monkeypatch.setattr(ops, "persist_assistant_turn", persist)
    monkeypatch.setattr(ops, "_push", noop)
    monkeypatch.setattr(ops, "mark_done", noop)
    monkeypatch.setattr(ops, "_record_checkpoint", noop)
    await ops._run_agent("p1", "s1", "q")
    assert order == ["persist", "status:complete"]


async def test_tool_result_checkpoints_running_turn(monkeypatch):
    saved = []

    async def persist(_sid, _pid, recorder, status, *_a, **_k):
        saved.append((status, [p["type"] for p in recorder.parts]))

    async def noop(*_a, **_k):
        return None

    class Snapshot:
        interrupts = ()
        values = {"messages": []}
        config = {"configurable": {}}

    class FakeAgent:
        async def aget_state(self, _c):
            return Snapshot()

        async def astream(self, *_a, **_k):
            yield ("custom", {"event": "tool.result", "data": {"id": "c1", "content": "ok"}})

    monkeypatch.setattr(ops, "get_agent", lambda: FakeAgent())
    monkeypatch.setattr(ops, "set_thread_status", noop)
    monkeypatch.setattr(ops, "persist_assistant_turn", persist)
    monkeypatch.setattr(ops, "_push", noop)
    monkeypatch.setattr(ops, "mark_done", noop)
    monkeypatch.setattr(ops, "_record_checkpoint", noop)
    monkeypatch.setattr(ops, "_normalize_custom", lambda d: (d["event"], d["data"]))
    await ops._run_agent("p1", "s1", "q")
    assert saved[0] == ("running", ["tool_result"])
    assert saved[-1][0] == "complete"


async def test_terminal_save_failure_is_reported_not_complete(monkeypatch):
    """B2:终态写库失败时,不能报 complete,要有可见的错误事件。"""
    statuses, events = [], []

    async def status(_sid, s):
        statuses.append(s)

    async def persist(*_a, **_k):
        return False

    async def push(_pid, event, _data):
        events.append(event)

    async def noop(*_a, **_k):
        return None

    class Snapshot:
        interrupts = ()
        values = {"messages": []}
        config = {"configurable": {}}

    class FakeAgent:
        async def aget_state(self, _c):
            return Snapshot()

        async def astream(self, *_a, **_k):
            if False:
                yield None

    monkeypatch.setattr(ops, "get_agent", lambda: FakeAgent())
    monkeypatch.setattr(ops, "set_thread_status", status)
    monkeypatch.setattr(ops, "persist_assistant_turn", persist)
    monkeypatch.setattr(ops, "_push", push)
    monkeypatch.setattr(ops, "mark_done", noop)
    monkeypatch.setattr(ops, "_record_checkpoint", noop)
    await ops._run_agent("p1", "s1", "q")
    assert "complete" not in statuses and statuses[-1] == "error"
    assert "error" in events and events[-1] == "stream.end"


async def test_startup_failure_does_not_rewind(stubbed, monkeypatch):
    """B5:回退是破坏性的,启动中途失败时历史和 checkpoint 必须原样保留。"""
    rewound = []

    async def check(_sid, _mid):
        return None

    async def rewind(_sid, _mid):
        rewound.append(1)
        return []

    async def files(_mid):
        return []

    async def boom(*_a, **_k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(ops, "_check_rewind_target", check)
    monkeypatch.setattr(ops, "_rewind", rewind)
    monkeypatch.setattr(ops.repository, "get_message_files", files)
    monkeypatch.setattr(ops, "_push", boom)
    with pytest.raises(RuntimeError):
        await ops.start_chat("s1", "hi", [], rewind_to_message_id=1)
    assert rewound == []


async def test_delete_refused_while_starting(monkeypatch):
    """B6:启动中的会话不能被删除,删除期间也不能启动。"""
    from app.services import conversation_cleanup as cc

    ops._starting_sessions.add("s2")
    try:
        with pytest.raises(ops.ActiveRunError):
            await cc.delete_conversation_data("s2")
    finally:
        ops._starting_sessions.discard("s2")

    ops._deleting_sessions.add("s3")
    try:
        with pytest.raises(ops.ActiveRunError):
            await ops.start_chat("s3", "hi", [])
    finally:
        ops._deleting_sessions.discard("s3")


async def test_same_request_id_returns_original_prompt(stubbed):
    first = await ops.start_chat("s1", "hi", [], client_request_id="r1")
    again = await ops.start_chat("s1", "hi", [], client_request_id="r1")
    assert again == first
    assert stubbed["launched"] == 1


async def test_request_id_with_different_content_is_rejected(stubbed):
    await ops.start_chat("s1", "hi", [], client_request_id="r2")
    with pytest.raises(ops.RequestIdConflictError):
        await ops.start_chat("s1", "other", [], client_request_id="r2")
    assert stubbed["launched"] == 1


async def test_request_id_is_scoped_to_owner(stubbed):
    a = await ops.start_chat("s1", "hi", [], owner="a", client_request_id="r3")
    ops._active_agent_tasks.pop("s1", None)
    b = await ops.start_chat("s1", "hi", [], owner="b", client_request_id="r3")
    assert a != b
    assert stubbed["launched"] == 2


async def test_failed_start_is_not_remembered(stubbed, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(ops, "persist_user_turn", boom)
    with pytest.raises(RuntimeError):
        await ops.start_chat("s1", "hi", [], client_request_id="r4")
    assert ("", "s1", "r4") not in ops._accepted_requests
