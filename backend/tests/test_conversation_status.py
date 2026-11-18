"""会话列表的运行状态与未读:终态随运行回执入库,运行中看 runs 表,进入会话后清除。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from app.api import security
from app.api.routes import conversations as routes
from app.config import settings
from app.db import close_db, init_db, repository
from app.db import runs as run_store
from app.owner import owner_of_key
from app.services import chat_operations as ops
from tests.history_seed import run_turn, seed_assistant_turn, seed_user_turn

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
    await seed_user_turn("s1", "p1", "hello", owner=OA)
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
    await seed_assistant_turn("p1", [], outcome)
    item = await _item(client)
    assert (item["status"], item["unread"]) == (outcome, True)

    assert (await client.post("/conversations/s1/read", headers=HA)).status_code == 204
    item = await _item(client)
    assert (item["status"], item["unread"]) == (outcome, False)  # 状态保留,只清未读


async def test_cannot_mark_someone_elses_conversation(client):
    await seed_assistant_turn("p1", [], "complete")
    assert (await client.post("/conversations/s1/read", headers=HB)).status_code == 404
    assert (await client.post("/conversations/nope/read", headers=HA)).status_code == 404
    assert (await _item(client))["unread"] is True


async def test_running_in_redis_overrides_stored_outcome(client, monkeypatch):
    await seed_assistant_turn("p1", [], "complete")

    async def running(ids):
        return dict.fromkeys(ids, "running")

    monkeypatch.setattr(routes.run_store, "latest_statuses", running)
    item = await _item(client)
    assert (item["status"], item["unread"]) == ("running", False)


async def test_redis_failure_falls_back_to_stored_status(client, monkeypatch):
    await seed_assistant_turn("p1", [], "error")

    async def down(_ids):
        raise ConnectionError("redis down")

    monkeypatch.setattr(routes.run_store, "latest_statuses", down)
    item = await _item(client)
    assert (item["status"], item["unread"]) == ("error", True)


async def test_new_turn_clears_previous_outcome(client):
    await seed_assistant_turn("p1", [], "error")
    await seed_user_turn("s1", "p2", "again", owner=OA)
    item = await _item(client)
    assert item["status"] is None and item["unread"] is False


async def test_interrupted_run_is_marked_error_on_restart(client):
    assert await run_store.recover_interrupted() == 1
    item = await _item(client)
    assert (item["status"], item["unread"]) == ("error", True)


def _fake_agent(stream):
    class Snapshot:
        interrupts = ()
        tasks = ()
        values = {"messages": []}
        metadata = {}
        config = {"configurable": {}}

    class FakeAgent:
        async def aget_state(self, _c):
            return Snapshot()

        astream = staticmethod(stream)

    return FakeAgent()


@pytest.fixture
def run_env(client, monkeypatch):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(ops, "_push", noop)
    monkeypatch.setattr(ops, "_repair_cancelled_tool_calls", noop)
    monkeypatch.setattr(ops.CONTAINER_MANAGER, "snapshot_session", noop)


async def _outcome(prompt_id):
    run = await run_store.get(prompt_id)
    return run.status, (await repository.get_conversation("s1")).last_run_status


async def test_run_agent_records_error_when_agent_unavailable(run_env, monkeypatch):
    class Boom:
        def __getattr__(self, _):
            raise RuntimeError("no agent")

    monkeypatch.setattr(ops, "get_agent", lambda: Boom())
    await run_turn("s1", "p9", "q", owner=OA)
    assert await _outcome("p9") == ("error", "error")


async def test_run_agent_records_complete(run_env, monkeypatch):
    async def stream(*_a, **_k):
        if False:
            yield None

    monkeypatch.setattr(ops, "get_agent", lambda: _fake_agent(stream))
    await run_turn("s1", "p9", "q", owner=OA)
    assert await _outcome("p9") == ("complete", "complete")


async def test_run_agent_never_reports_complete_when_save_fails(run_env, monkeypatch):
    async def stream(*_a, **_k):
        if False:
            yield None

    async def broken(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ops, "get_agent", lambda: _fake_agent(stream))
    monkeypatch.setattr(ops.run_store, "save", broken)
    run = await run_turn("s1", "p9", "q", owner=OA)
    assert run.status != "complete"
    assert (await repository.get_conversation("s1")).last_run_status != "complete"


async def test_run_agent_records_cancelled(run_env, monkeypatch):
    import asyncio

    async def stream(*_a, **_k):
        raise asyncio.CancelledError
        yield None

    monkeypatch.setattr(ops, "get_agent", lambda: _fake_agent(stream))
    await run_turn("s1", "p9", "q", owner=OA)
    assert await _outcome("p9") == ("cancelled", "cancelled")
