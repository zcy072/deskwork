"""按 API Key 隔离数据:会话、记忆、文件下载、事件流、状态、取消、启动时归属旧数据。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from app.api import security
from app.api.routes import stream
from app.config import settings
from app.db import close_db, init_db, repository
from app.owner import owner_of_key

A, B = "key-aaaaaaaa", "key-bbbbbbbb"
HA, HB = {"Authorization": f"Bearer {A}"}, {"Authorization": f"Bearer {B}"}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "API_KEYS", f"{A},{B}")

    async def one(key, ttl):
        return 1

    monkeypatch.setattr(security, "incr_with_ttl", one)
    await close_db()
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    await close_db()


async def _conv(owner_key: str, cid: str = "s1"):
    await repository.ensure_conversation(cid, "hello", owner_of_key(owner_key))
    await repository.add_user_message_with_files(
        cid, prompt_id="", content="hello", parts=[], files=[]
    )


async def test_conversations_are_isolated(client):
    await _conv(A)
    assert [c["id"] for c in (await client.get("/conversations", headers=HA)).json()] == ["s1"]
    assert (await client.get("/conversations", headers=HB)).json() == []
    for method, url in (
        ("get", "/conversations/s1/messages"),
        ("get", "/conversations/s1/usage"),
        ("delete", "/conversations/s1"),
    ):
        assert (await getattr(client, method)(url, headers=HB)).status_code == 404, url
    assert (
        await client.patch("/conversations/s1", json={"title": "x"}, headers=HB)
    ).status_code == 404
    assert (await client.get("/conversations/s1/messages", headers=HA)).status_code == 200
    assert await repository.get_messages("s1") is not None  # B 的删除没有生效


async def test_session_endpoints_hide_other_owners(client, monkeypatch):
    await _conv(A)
    called = []

    async def status(sid):
        called.append(sid)
        return "complete", None

    monkeypatch.setattr(stream, "get_run_status", status)
    assert (await client.get("/thread-status/s1", headers=HB)).status_code == 404
    assert (await client.post("/thread-cancel/s1", headers=HB)).status_code == 404
    assert (
        await client.post(
            "/stream-events", json={"session_id": "s1", "prompt_id": "p1"}, headers=HB
        )
    ).status_code == 404
    assert (
        await client.get(
            "/files/download",
            params={"session_id": "s1", "relative_path": "output/a.txt"},
            headers=HB,
        )
    ).status_code == 404
    assert (
        await client.get(
            "/files/download",
            params={"session_id": "s1", "relative_path": "output/a.txt", "fingerprint": "a" * 64},
            headers=HB,
        )
    ).status_code == 404
    assert (
        await client.post(
            "/send-message", data={"session_id": "s1", "user_query": "hi"}, headers=HB
        )
    ).status_code == 404
    assert called == []  # 没有越过归属检查
    assert (await client.get("/thread-status/s1", headers=HA)).status_code == 200


async def test_unknown_session_is_visible_to_create(client):
    assert await repository.conversation_visible("new", owner_of_key(A)) is True


async def test_ensure_conversation_rejects_other_owner(client):
    await _conv(A)
    with pytest.raises(repository.NotOwnerError):
        await repository.ensure_conversation("s1", "x", owner_of_key(B))


async def test_memories_are_isolated(client):
    r = await client.post("/memories", json={"content": "A 的事实"}, headers=HA)
    mid = r.json()["id"]
    assert (await client.get("/memories", headers=HB)).json() == []
    assert (await client.delete(f"/memories/{mid}", headers=HB)).status_code == 404
    # 同样内容各存各的,互不去重
    rb = await client.post("/memories", json={"content": "A 的事实"}, headers=HB)
    assert rb.json()["id"] != mid
    assert len((await client.get("/memories", headers=HA)).json()) == 1
    assert (await client.delete(f"/memories/{mid}", headers=HA)).status_code == 204


async def test_memory_capacity_is_per_owner(client):
    oa, ob = owner_of_key(A), owner_of_key(B)
    await repository.add_memory("b1", None, 2, ob)
    for t in ("a1", "a2", "a3"):
        await repository.add_memory(t, None, 2, oa)
    assert [m.content for m in await repository.list_memories(ob)] == ["b1"]
    assert len(await repository.list_memories(oa)) == 2


async def test_memory_tools_use_current_owner(client, monkeypatch):
    from app.owner import current_owner
    from app.tools.memory_tools import forget, recall, remember

    current_owner.set(owner_of_key(A))
    monkeypatch.setattr(settings, "MEMORY_CONFIRM_WRITES", False)
    out = await remember.ainvoke({"content": "喜欢简洁"})
    mid = int(out.split("id=")[1].rstrip("."))
    current_owner.set(owner_of_key(B))
    assert await recall.ainvoke({"query": "简洁"}) == "No memories saved."
    assert "Error" in await forget.ainvoke({"memory_id": mid})
    current_owner.set(owner_of_key(A))
    assert "喜欢简洁" in await recall.ainvoke({"query": "简洁"})


async def test_adopt_legacy_rows(client):
    await repository.ensure_conversation("old", "q", "")
    await repository.add_memory("旧记忆", None, 10, "")
    first = owner_of_key(A)
    assert await repository.adopt_legacy_rows(first) == 2
    assert [c.id for c, _ in await repository.list_conversations(owner=first)] == ["old"]
    assert [m.content for m in await repository.list_memories(first)] == ["旧记忆"]
    assert await repository.adopt_legacy_rows(first) == 0


async def test_auth_disabled_owner_is_empty(monkeypatch):
    monkeypatch.setattr(settings, "API_KEYS", "")

    class R:
        headers = {"authorization": "Bearer whatever"}

    assert security.get_owner(R()) == ""


async def test_read_endpoints_404_for_conversation_without_record(client, monkeypatch):
    """没有会话记录的 session_id 不能读沙箱文件/事件流/状态;只有 send-message 允许新建。"""
    for method, url, kw in (
        ("get", "/thread-status/ghost", {}),
        ("post", "/thread-cancel/ghost", {}),
        (
            "get",
            "/files/download",
            {"params": {"session_id": "ghost", "relative_path": "output/a"}},
        ),
        ("post", "/stream-events", {"json": {"session_id": "ghost", "prompt_id": "p"}}),
    ):
        r = await getattr(client, method)(url, headers=HA, **kw)
        assert r.status_code == 404, url
    assert await repository.conversation_visible("ghost", owner_of_key(A))
    assert not await repository.conversation_visible("ghost", owner_of_key(A), allow_missing=False)


async def test_download_whitelist_applies_to_resolved_path(client, tmp_path, monkeypatch):
    await _conv(A)
    root = tmp_path / "sessions" / "s1"
    for d in ("output", "scratchpad"):
        (root / d).mkdir(parents=True)
    (root / "scratchpad" / "x.txt").write_text("secret")
    (root / "output" / "ok.txt").write_text("ok")
    from app.sandbox import session_paths

    monkeypatch.setattr(session_paths, "SESSION_ROOT", tmp_path / "sessions")

    def get(rel):
        return client.get(
            "/files/download", params={"session_id": "s1", "relative_path": rel}, headers=HA
        )

    assert (await get("output/ok.txt")).status_code == 200
    assert (await get("output/../scratchpad/x.txt")).status_code == 400
    assert (await get("scratchpad/x.txt")).status_code == 400
