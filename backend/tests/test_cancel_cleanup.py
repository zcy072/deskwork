"""清理阶段被再次取消(用户连点停止)时,stream.end 和状态复位仍要完成。"""

import asyncio

from app.services import chat_operations as ops


class _Agent:
    async def aget_state(self, _config):
        class S:
            values = {"messages": []}
            interrupts = ()
            config = {"configurable": {"checkpoint_id": ""}}

        return S()

    def astream(self, *_a, **_k):
        async def gen():
            await asyncio.sleep(999)
            yield  # pragma: no cover

        return gen()


async def test_second_cancel_during_cleanup_does_not_lose_stream_end(monkeypatch):
    events, status = [], []
    in_cleanup = asyncio.Event()

    async def push(_pid, event, data):
        events.append(event)

    async def set_status(_sid, s):
        status.append(s)

    async def persist(*_a, **_k):
        in_cleanup.set()
        await asyncio.sleep(0.2)  # 落库较慢,给第二次 cancel 留窗口

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(ops, "get_agent", lambda: _Agent())
    monkeypatch.setattr(ops, "_push", push)
    monkeypatch.setattr(ops, "set_thread_status", set_status)
    monkeypatch.setattr(ops, "persist_assistant_turn", persist)
    monkeypatch.setattr(ops, "mark_done", noop)
    monkeypatch.setattr(ops, "refresh_running_ttl", noop)

    task = asyncio.create_task(ops._run_agent("p", "s", "q"))
    await asyncio.sleep(0.05)
    task.cancel()
    await in_cleanup.wait()
    task.cancel()  # 第二次取消打在清理阶段
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert "stream.end" in events and status == ["complete"]
    assert "p" not in ops._started_prompts
