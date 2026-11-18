"""repository 的分页/计数/索引迁移/批量删除,以及 /conversations/{id}/messages 分页。"""

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app import app
from app.config import settings
from app.db import close_db, get_session, init_db, repository


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    await init_db()
    yield
    await close_db()


async def _seed(cid, n_messages, owner=""):
    await repository.ensure_conversation(cid, f"title {cid}", owner)
    for i in range(n_messages):
        await repository.add_user_message_with_files(
            cid, prompt_id="", content=f"m{i}", parts=[], files=[]
        )


async def test_list_conversations_counts_pagination_and_owner(db):
    await _seed("a", 3, "o1")
    await _seed("b", 0, "o1")
    await _seed("c", 2, "o1")
    await _seed("x", 5, "o2")
    rows = await repository.list_conversations(owner="o1")
    # 最近更新在前:c 最后写入,b 没有消息所以只在创建时有 updated_at
    counts = {c.id: n for c, n in rows}
    assert counts == {"a": 3, "b": 0, "c": 2}
    assert [c.id for c, _ in rows][0] == "c"
    page1 = await repository.list_conversations(limit=2, offset=0, owner="o1")
    page2 = await repository.list_conversations(limit=2, offset=2, owner="o1")
    assert len(page1) == 2 and len(page2) == 1
    assert {c.id for c, _ in page1 + page2} == {"a", "b", "c"}
    assert {c.id for c, _ in await repository.list_conversations(owner="o2")} == {"x"}


async def test_delete_conversation_removes_messages_and_files(db):
    await _seed("s", 3, "o")
    await repository.add_user_message_with_files(
        "s", prompt_id="", content="f", parts=[], files=[{"file_name": "a", "container_path": "/a"}]
    )
    assert await repository.delete_conversation("s") is True
    assert await repository.get_messages("s") is None
    async with get_session() as s:
        assert (await s.execute(text("SELECT count(*) FROM messages"))).scalar() == 0
        assert (await s.execute(text("SELECT count(*) FROM uploaded_files"))).scalar() == 0
    assert await repository.delete_conversation("s") is False


async def test_get_messages_limit_before_and_owner(db):
    await _seed("s", 5, "o")
    allm = await repository.get_messages("s")
    assert len(allm) == 5
    last2 = await repository.get_messages("s", limit=2)
    assert [m.id for m in last2] == [m.id for m in allm[-2:]]  # 升序
    older = await repository.get_messages("s", limit=2, before_id=last2[0].id)
    assert [m.id for m in older] == [m.id for m in allm[1:3]]
    assert await repository.get_messages("s", owner="other") is None
    assert await repository.get_messages("s", owner="o") is not None


async def test_messages_endpoint_paginates_and_keeps_default(db):
    await _seed("s", 4)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        full = (await c.get("/conversations/s/messages")).json()
        assert len(full) == 4
        page = (await c.get("/conversations/s/messages", params={"limit": 2})).json()
        assert [m["id"] for m in page] == [m["id"] for m in full[-2:]]
        before = (
            await c.get(
                "/conversations/s/messages", params={"limit": 5, "before_id": full[2]["id"]}
            )
        ).json()
        assert [m["id"] for m in before] == [m["id"] for m in full[:2]]
        assert (await c.get("/conversations/nope/messages")).status_code == 404
        assert (await c.get("/conversations/s/messages", params={"limit": 0})).status_code == 422


async def test_messages_endpoint_returns_attachments_for_user_messages_only(db):
    await repository.ensure_conversation("s", "q")
    f = [{"file_name": "x.docx", "container_path": "/workspace/uploads/x.docx"}]
    await repository.add_user_message_with_files(
        "s", prompt_id="p1", content="a", parts=[], files=f
    )
    await repository.save_assistant_message("s", "p1", content="r", parts=[], status="complete")
    await repository.add_user_message_with_files(
        "s", prompt_id="p2", content="b", parts=[], files=[]
    )
    assert await repository.get_files_for_messages([]) == {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        rows = (await c.get("/conversations/s/messages")).json()
    assert [r.get("attachments") for r in rows] == [f, None, []]


async def test_save_assistant_message_by_id_updates_in_place(db):
    await repository.ensure_conversation("s", "q")
    first = await repository.save_assistant_message(
        "s", "p", content="a", parts=[{"type": "text", "text": "a"}], status="running"
    )
    second = await repository.save_assistant_message(
        "s", "p", content="ab", parts=[], status="complete", usage={"t": 1}, message_id=first.id
    )
    assert second.id == first.id
    msgs = await repository.get_messages("s")
    assert len(msgs) == 1 and msgs[0].content == "ab" and msgs[0].usage == {"t": 1}
    # id 不存在时退回查找/创建,不会丢数据
    third = await repository.save_assistant_message(
        "s", "p2", content="x", parts=[], status="complete", message_id=99999
    )
    assert third.id != 99999 and len(await repository.get_messages("s")) == 2


def _old_schema_db(path):
    """没有 owner / usage 等新列,也没有任何新索引的旧库。"""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE conversations (id VARCHAR(64) PRIMARY KEY, title VARCHAR(200),
            created_at DATETIME, updated_at DATETIME);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id VARCHAR(64) REFERENCES conversations(id) ON DELETE CASCADE,
            prompt_id VARCHAR(64), role VARCHAR(16), content TEXT, parts JSON,
            status VARCHAR(16), created_at DATETIME);
        CREATE TABLE uploaded_files (id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id VARCHAR(64) REFERENCES conversations(id) ON DELETE CASCADE,
            message_id INTEGER REFERENCES messages(id), file_name VARCHAR(255),
            container_path VARCHAR(512), created_at DATETIME);
        CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, content VARCHAR(500),
            created_at DATETIME, last_used_at DATETIME);
        INSERT INTO conversations VALUES ('old', 't', '2024-01-01', '2024-01-01');
        INSERT INTO memories VALUES (1, 'm', '2024-01-01', '2024-01-01');
        """
    )
    con.commit()
    con.close()


async def test_init_db_adds_missing_columns_indexes_and_normalizes_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    _old_schema_db(tmp_path / "deskwork.sqlite")
    await close_db()
    await init_db()
    try:
        async with get_session() as s:
            idx = {
                r[0]
                for r in await s.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
            }
            assert "ix_conversations_owner_updated" in idx
            assert "ix_uploaded_files_message_id" in idx
            assert "ix_memories_owner" in idx
            # 旧数据的 NULL owner 被规范成 "",直接比较 owner == "" 就能查到
            assert (await s.execute(text("SELECT owner FROM conversations"))).scalar() == ""
        assert [c.id for c, _ in await repository.list_conversations(owner="")] == ["old"]
        assert len(await repository.list_memories("")) == 1
    finally:
        await close_db()


async def _seed_text(cid, owner, title, *texts):
    await repository.ensure_conversation(cid, title, owner)
    for t in texts:
        await repository.add_user_message_with_files(
            cid, prompt_id="", content=t, parts=[], files=[]
        )


async def test_search_matches_title_and_body_scoped_to_owner(db):
    await _seed_text("a", "o1", "季度报告", "请分析销售数据")
    await _seed_text("b", "o1", "闲聊", "今天的销售额是多少", "销售数据已更新")
    await _seed_text("c", "o2", "销售复盘", "别人的销售数据")
    rows = await repository.search_conversations("销售", "o1")
    by_id = {c.id: snip for c, snip in rows}
    assert set(by_id) == {"a", "b"}
    assert "销售数据已更新" in by_id["b"]  # 取最新命中的消息
    title_rows = await repository.search_conversations("报告", "o1")
    assert [c.id for c, _ in title_rows] == ["a"]
    assert title_rows[0][1] == ""
    assert {c.id for c, _ in await repository.search_conversations("销售", "o2")} == {"c"}


async def test_search_escapes_like_wildcards_and_ignores_case(db):
    await _seed_text("a", "", "t1", "100% done", "a_b")
    await _seed_text("b", "", "t2", "100 percent", "axb")
    assert [c.id for c, _ in await repository.search_conversations("%", "")] == ["a"]
    assert [c.id for c, _ in await repository.search_conversations("a_b", "")] == ["a"]
    assert [c.id for c, _ in await repository.search_conversations("DONE", "")] == ["a"]
    assert await repository.search_conversations("\\", "") == []


async def test_search_endpoint_validates_and_does_not_shadow_messages_route(db):
    await _seed_text("s", "", "t", "hello world")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/conversations/search", params={"q": "hello"})
        assert r.status_code == 200
        assert r.json()[0]["id"] == "s" and "hello" in r.json()[0]["snippet"]
        assert (await c.get("/conversations/search", params={"q": ""})).status_code == 422
        assert (await c.get("/conversations/search", params={"q": "  "})).status_code == 422
        assert (await c.get("/conversations/search", params={"q": "x" * 101})).status_code == 422
        assert (await c.get("/conversations/s/messages")).status_code == 200
