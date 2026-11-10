"""会话列表的运行状态与未读:终态入库,运行中看 Redis,进入会话后清除。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from app.api import security
from app.api.routes import conversations as routes
from app.config import settings
from app.db import close_db, init_db, repository
from app.owner import owner_of_key
from app.services import chat_operations as ops
from app.services.history import persist_user_turn

A, B = "key-aaaaaaaa", "key-bbbbbbbb"
HA, HB = {"Authorization": f"Bearer {A}"}, {"Authorization": f"Bearer {B}"}
OA = owner_of_key(A)


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "API_KEYS", f"{A},{B}")

    async def one(key, ttl):
        return 1

    async def idle(ids):
        return {}

    monkeypatch.setattr(security, "incr_with_ttl", one)
    monkeypatch.setattr(routes.run_store, "latest_statuses", idle)
    await close_db()
    await init_db()
    await persist_user_turn("s1", "p1", "hello", [], OA)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    await close_db()


async def _item(client, headers=HA):
    return (await client.get("/conversations", headers=headers)).json()[0]


async def test_new_conversation_has_no_status(client):
    item = await _item(client)
    assert item["status"] is None and item["unread"] is False


@pytest.mark.parametrize("outcome", ["complete", "waiting", "error", "cancelled"])
async def test_outcome_is_listed_unread_until_marked_read(client, outcome):
    await repository.set_run_outcome("s1", outcome)
    item = await _item(client)
    assert (item["status"], item["unread"]) == (outcome, True)

    assert (await client.post("/conversations/s1/read", headers=HA)).status_code == 204
    item = await _item(client)
    assert (item["status"], item["unread"]) == (outcome, False)  # 状态保留,只清未读


async def test_cannot_mark_someone_elses_conversation(client):
    await repository.set_run_outcome("s1", "complete")
    assert (await client.post("/conversations/s1/read", headers=HB)).status_code == 404
    assert (await client.post("/conversations/nope/read", headers=HA)).status_code == 404
    assert (await _item(client))["unread"] is True


async def test_running_in_redis_overrides_stored_outcome(client, monkeypatch):
    await repository.set_run_outcome("s1", "complete")

    async def running(ids):
        return dict.fromkeys(ids, "running")

    monkeypatch.setattr(routes.run_store, "latest_statuses", running)
    item = await _item(client)
    assert (item["status"], item["unread"]) == ("running", False)


async def test_redis_failure_falls_back_to_stored_status(client, monkeypatch):
    await repository.set_run_outcome("s1", "error")

    async def down(_ids):
        raise ConnectionError("redis down")

    monkeypatch.setattr(routes.run_store, "latest_statuses", down)
    item = await _item(client)
    assert (item["status"], item["unread"]) == ("error", True)


async def test_new_turn_clears_previous_outcome(client):
    await repository.set_run_outcome("s1", "error")
    await persist_user_turn("s1", "p2", "again", [], OA)
    item = await _item(client)
    assert item["status"] is None and item["unread"] is False


async def test_interrupted_run_is_marked_error_on_restart(client):
    await repository.save_assistant_message("s1", "p1", content="x", parts=[], status="running")
    await repository.mark_interrupted_messages()
    item = await _item(client)
    assert (item["status"], item["unread"]) == ("error", True)


async def test_record_outcome_is_best_effort(client, monkeypatch):
    async def boom(*_a):
        raise RuntimeError("db down")

    monkeypatch.setattr(ops.repository, "set_run_outcome", boom)
    await ops._record_outcome("s1", "complete")  # 不抛异常


async def test_run_agent_records_outcomes(client, monkeypatch):
    seen = []

    async def record(_sid, status):
        seen.append(status)

    async def noop(*_a, **_k):
        return None

    class Boom:
        def __getattr__(self, _):
            raise RuntimeError("no agent")

    monkeypatch.setattr(ops, "_record_outcome", record)
    monkeypatch.setattr(ops, "get_agent", lambda: Boom())
    monkeypatch.setattr(ops, "set_thread_status", noop)
    monkeypatch.setattr(ops, "_push", noop)
    monkeypatch.setattr(ops, "mark_done", noop)
    await ops._run_agent("p9", "s1", "q")
    assert seen == ["error"]


def _fake_agent(stream):
    class Snapshot:
        interrupts = ()
        values = {"messages": []}
        config = {"configurable": {}}

    class FakeAgent:
        async def aget_state(self, _c):
            return Snapshot()

        astream = staticmethod(stream)

    return FakeAgent()


@pytest.fixture
def run_env(monkeypatch):
    seen = []

    async def record(_sid, status):
        seen.append(status)

    async def noop(*_a, **_k):
        return None

    async def saved(*_a, **_k):
        return True

    monkeypatch.setattr(ops, "_record_outcome", record)
    monkeypatch.setattr(ops, "set_thread_status", noop)
    monkeypatch.setattr(ops, "persist_assistant_turn", saved)
    monkeypatch.setattr(ops, "_push", noop)
    monkeypatch.setattr(ops, "mark_done", noop)
    monkeypatch.setattr(ops, "_record_checkpoint", noop)
    monkeypatch.setattr(ops, "_repair_cancelled_tool_calls", noop)
    return seen


async def test_run_agent_records_complete(run_env, monkeypatch):
    async def stream(*_a, **_k):
        if False:
            yield None

    monkeypatch.setattr(ops, "get_agent", lambda: _fake_agent(stream))
    await ops._run_agent("p1", "s1", "q")
    assert run_env == ["complete"]


async def test_run_agent_records_error_when_save_fails(run_env, monkeypatch):
    async def stream(*_a, **_k):
        if False:
            yield None

    async def not_saved(*_a, **_k):
        return False

    monkeypatch.setattr(ops, "get_agent", lambda: _fake_agent(stream))
    monkeypatch.setattr(ops, "persist_assistant_turn", not_saved)
    await ops._run_agent("p1", "s1", "q")
    assert run_env[0] == "error" and "complete" not in run_env


async def test_run_agent_records_cancelled(run_env, monkeypatch):
    import asyncio

    async def stream(*_a, **_k):
        raise asyncio.CancelledError
        yield None

    monkeypatch.setattr(ops, "get_agent", lambda: _fake_agent(stream))
    await ops._run_agent("p1", "s1", "q")
    assert run_env == ["cancelled"]
