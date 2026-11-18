"""清理阶段被再次取消(用户连点停止)时,stream.end 和状态复位仍要完成。"""

import asyncio

from app.services import chat_operations as ops
from tests.history_seed import run_turn


class _Agent:
    async def aget_state(self, _config):
        class S:
            values = {"messages": []}
            interrupts = ()
            tasks = ()
            metadata = {}
            config = {"configurable": {"checkpoint_id": ""}}

        return S()

    def astream(self, *_a, **_k):
        async def gen():
            await asyncio.sleep(999)
            yield  # pragma: no cover

        return gen()


async def test_second_cancel_during_cleanup_does_not_lose_stream_end(run_db, monkeypatch):
    events = []
    in_cleanup = asyncio.Event()
    real_save = ops.run_store.save

    async def push(_pid, event, data):
        events.append(event)

    async def slow_save(*args, **kwargs):
        if args[5] == "cancelled":
            in_cleanup.set()
            await asyncio.sleep(0.2)  # 落库较慢,给第二次 cancel 留窗口
        return await real_save(*args, **kwargs)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(ops, "get_agent", lambda: _Agent())
    monkeypatch.setattr(ops, "_push", push)
    monkeypatch.setattr(ops.run_store, "save", slow_save)
    monkeypatch.setattr(ops, "finish_existing_stream", noop)
    monkeypatch.setattr(ops.CONTAINER_MANAGER, "snapshot_session", noop)

    task = asyncio.create_task(run_turn("s", "p", "q"))
    await asyncio.sleep(0.05)
    task.cancel()
    await in_cleanup.wait()
    task.cancel()  # 第二次取消打在清理阶段
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert "stream.end" in events
    assert (await ops.run_store.get("p")).status == "cancelled"
    assert ops.sessions.get("s") is None
