"""错误信息不外泄、host_path 不外泄、终态写库失败如实报告。"""

import json

import pytest

from app.db import repository
from app.services import chat_operations as ops
from app.services.budget import BudgetExceeded
from tests.history_seed import run_turn


class _Agent:
    def __init__(self, exc):
        self.exc = exc

    async def aget_state(self, _config):
        class S:
            values = {"messages": []}
            interrupts = ()
            tasks = ()
            metadata = {}
            config = {"configurable": {"checkpoint_id": ""}}

        return S()

    def astream(self, *_a, **_k):
        exc = self.exc

        async def gen():
            raise exc
            yield  # pragma: no cover

        return gen()


@pytest.fixture
def pushed(run_db, monkeypatch):
    events: list[tuple[str, dict]] = []

    async def push(_pid, event, data):
        events.append((event, data))

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(ops, "_push", push)
    monkeypatch.setattr(ops, "_repair_cancelled_tool_calls", noop)
    monkeypatch.setattr(ops.CONTAINER_MANAGER, "snapshot_session", noop)
    return events


async def test_internal_error_is_not_leaked(pushed, monkeypatch):
    monkeypatch.setattr(
        ops, "get_agent", lambda: _Agent(RuntimeError("/srv/secret/path sqlite error"))
    )
    await run_turn("s", "p", "q")
    msg = next(d["message"] for e, d in pushed if e == "error")
    assert "secret" not in msg and "trace id" in msg


async def test_budget_error_is_passed_verbatim(pushed, monkeypatch):
    monkeypatch.setattr(ops, "get_agent", lambda: _Agent(BudgetExceeded("已达到上限,请拆小任务")))
    await run_turn("s", "p", "q")
    assert next(d["message"] for e, d in pushed if e == "error") == "已达到上限,请拆小任务"


async def test_save_failure_event_has_code(pushed, monkeypatch):
    async def broken(*_a, **_k):
        raise RuntimeError("disk full")

    async def stream(*_a, **_k):
        if False:
            yield None

    class _Ok(_Agent):
        def astream(self, *_a, **_k):
            return stream()

    monkeypatch.setattr(ops, "get_agent", lambda: _Ok(None))
    monkeypatch.setattr(ops.run_store, "save", broken)
    await run_turn("s", "p", "q")
    err = next(d for e, d in pushed if e == "error")
    assert err["code"] == "history_save_failed" and "历史记录" in err["message"]


def test_files_presented_strips_host_path():
    event, data = ops._normalize_custom(
        {
            "kind": "files.presented",
            "files": [{"file_name": "a", "host_path": "/srv/x/a", "container_path": "/w/a"}],
        }
    )
    assert event == "files.presented"
    assert data["files"] == [{"file_name": "a", "container_path": "/w/a"}]


async def test_start_chat_events_exclude_host_path(run_db, monkeypatch):
    sent = []

    async def push(_pid, event, data):
        sent.append(json.dumps(data))

    async def upload(_sid, _u):
        return [{"file_name": "a", "container_path": "/w/a", "host_path": "/srv/a"}]

    monkeypatch.setattr(ops, "_push", push)
    monkeypatch.setattr(ops, "upload_session_files", upload)
    monkeypatch.setattr(ops, "_launch_agent_task", lambda *a, **k: None)
    prompt_id = await ops.start_chat("s9", "hi", [])
    assert sent and all("host_path" not in s for s in sent)
    run = await ops.run_store.get(prompt_id)
    assert run.input_json["files"] == [{"file_name": "a", "container_path": "/w/a"}]
    assert await repository.get_conversation("s9") is not None
