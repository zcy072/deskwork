import asyncio

import pytest

from app.runtime.sessions import ActiveRunError, sessions
from app.services import conversation_cleanup as cc


class Spy:
    def __init__(self):
        self.calls = []

    def make(self, name, fail=False):
        async def fn(*args):
            self.calls.append(name)
            if fail:
                raise RuntimeError("boom")

        return fn


def _patch(monkeypatch, spy, *, exists=True, status=None, fail=None):
    async def get_status(_id):
        return status

    class FakeSaver:
        adelete_thread = staticmethod(spy.make("checkpoints", fail == "checkpoints"))

    class FakeManager:
        purge_session = staticmethod(spy.make("sandbox", fail == "sandbox"))

    async def delete_row(_id):
        spy.calls.append("db")
        return exists

    monkeypatch.setattr(cc, "get_thread_status", get_status)
    monkeypatch.setattr(cc.chat_operations, "get_thread_status", get_status)
    monkeypatch.setattr(cc.repository, "delete_conversation", delete_row)
    monkeypatch.setattr(cc, "get_checkpointer", lambda: FakeSaver)
    monkeypatch.setattr(cc, "CONTAINER_MANAGER", FakeManager)
    monkeypatch.setattr(cc, "purge_session", spy.make("redis"))


async def test_deletes_everything(monkeypatch):
    spy = Spy()
    _patch(monkeypatch, spy)
    assert await cc.delete_conversation_data("abc") is True
    assert spy.calls == ["db", "checkpoints", "sandbox", "redis"]


async def test_unknown_conversation_touches_nothing_else(monkeypatch):
    spy = Spy()
    _patch(monkeypatch, spy, exists=False)
    assert await cc.delete_conversation_data("abc") is False
    assert spy.calls == ["db"]


async def test_one_failed_cleanup_does_not_block_the_rest(monkeypatch):
    spy = Spy()
    _patch(monkeypatch, spy, fail="sandbox")
    assert await cc.delete_conversation_data("abc") is True
    assert spy.calls == ["db", "checkpoints", "sandbox", "redis"]


async def test_running_chat_is_cancelled_first(monkeypatch):
    spy = Spy()
    _patch(monkeypatch, spy, status="running")

    async def cancel(_id):
        spy.calls.append("cancel")

    monkeypatch.setattr(cc.chat_operations, "cancel_chat", cancel)
    await cc.delete_conversation_data("abc")
    assert spy.calls[0] == "cancel"


async def test_failed_sandbox_purge_leaves_marker_and_retry_clears_it(monkeypatch, tmp_path):
    spy = Spy()
    _patch(monkeypatch, spy, fail="sandbox")
    monkeypatch.setattr(cc, "PENDING_PURGE_DIR", tmp_path / "pending")
    assert await cc.delete_conversation_data("abc") is True
    assert (tmp_path / "pending" / "abc").exists()

    # manager 恢复后重试成功并清掉标记
    _patch(monkeypatch, Spy())
    assert await cc.retry_pending_purges(tmp_path / "pending") == 1
    assert not (tmp_path / "pending" / "abc").exists()


async def test_retry_keeps_marker_while_still_failing(monkeypatch, tmp_path):
    root = tmp_path / "pending"
    root.mkdir()
    (root / "abc").touch()
    _patch(monkeypatch, Spy(), fail="sandbox")
    assert await cc.retry_pending_purges(root) == 0
    assert (root / "abc").exists()


@pytest.fixture(autouse=True)
def _isolated_pending_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "PENDING_PURGE_DIR", tmp_path / "pending-default")


@pytest.mark.parametrize("stage", ["run", "commit", "purge"])
async def test_cancelled_delete_drains_owned_work_before_releasing_gate(monkeypatch, stage):
    spy = Spy()
    _patch(monkeypatch, spy)
    entered, finish = asyncio.Event(), asyncio.Event()
    handle = sessions.reserve("abc")
    handle.phase = "running"
    handle.started = True
    handle.ready.set()

    async def block():
        entered.set()
        await finish.wait()

    async def run():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handle.phase = "finalizing"
            if stage == "run":
                await block()

    async def commit(_id):
        spy.calls.append("db")
        await block()
        return True

    async def purge(_id):
        spy.calls.append("sandbox")
        await block()

    if stage == "commit":
        monkeypatch.setattr(cc.repository, "delete_conversation", commit)
    elif stage == "purge":
        monkeypatch.setattr(cc.CONTAINER_MANAGER, "purge_session", purge)
    handle.task = asyncio.create_task(run())
    await asyncio.sleep(0)
    task = asyncio.create_task(cc.delete_conversation_data("abc"))
    await asyncio.wait_for(entered.wait(), 2)
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert sessions.get("abc") is handle
        with pytest.raises(ActiveRunError):
            sessions.reserve("abc")
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert handle.task.done()
    assert spy.calls == ["db", "checkpoints", "sandbox", "redis"]
    assert sessions.get("abc") is None
    assert not (cc.PENDING_PURGE_DIR / "abc").exists()


async def test_unconfirmed_run_cleanup_keeps_recovery_gate(monkeypatch):
    spy = Spy()
    _patch(monkeypatch, spy)
    handle = sessions.reserve("abc")
    handle.phase = "running"
    handle.task = asyncio.create_task(asyncio.sleep(0))
    await handle.task

    async def failed_cancel(_id):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(cc.chat_operations, "cancel_chat", failed_cancel)
    with pytest.raises(ActiveRunError):
        await cc.delete_conversation_data("abc")
    assert sessions.get("abc") is handle
    assert (handle.operation, handle.phase) == ("run", "recovery_required")
    assert not spy.calls
