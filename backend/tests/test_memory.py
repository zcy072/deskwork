"""长期记忆:存取、过期、容量淘汰、召回排序、工具确认、提示注入、HTTP 接口。"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app import app
from app.agents.middlewares import MemoryMiddleware
from app.config import settings
from app.db import close_db, init_db, repository
from app.services.memory import format_for_prompt, select_memories
from app.tools.memory_tools import forget, recall, remember
from tests.test_ask_user import ScriptedModel


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    await init_db()
    yield
    await close_db()


async def test_add_dedups_and_refreshes(db):
    a = await repository.add_memory("用户喜欢简洁的回答", 30, 100)
    b = await repository.add_memory("用户喜欢简洁的回答", 30, 100)
    assert a.id == b.id and len(await repository.list_memories()) == 1


async def test_expired_hidden_and_purged_on_write(db):
    await repository.add_memory("旧事实", 1, 100)
    future = datetime.now(UTC) + timedelta(days=2)
    assert await repository.list_memories(now=future) == []
    assert len(await repository.list_memories()) == 1
    await repository.add_memory("别的事实", None, 100)  # 写入时顺带清理真正过期的(此刻未过期)
    assert len(await repository.list_memories()) == 2


async def test_zero_ttl_never_expires(db):
    await repository.add_memory("永久", None, 100)
    far = datetime.now(UTC) + timedelta(days=36500)
    assert len(await repository.list_memories(now=far)) == 1


async def test_capacity_evicts_least_recently_used(db):
    a = await repository.add_memory("甲", None, 2)
    await repository.add_memory("乙", None, 2)
    await repository.touch_memories([a.id])  # 甲最近被用过,乙最久未用
    await repository.add_memory("丙", None, 2)
    assert {m.content for m in await repository.list_memories()} == {"甲", "丙"}


async def _age(memory_id, days):
    """把记忆的写入/使用时间和过期时间一起往过去推 days 天。"""
    from app.db import get_session
    from app.db.models import Memory

    async with get_session() as s:
        mem = await s.get(Memory, memory_id)
        mem.last_used_at -= timedelta(days=days)
        mem.expires_at -= timedelta(days=days)
        await s.commit()


async def test_touch_renews_with_own_ttl(db):
    short = await repository.add_memory("短期", 1, 100)
    long = await repository.add_memory("长期", 30, 100)
    await _age(long.id, 20)  # 长期还剩 10 天
    await repository.touch_memories([short.id, long.id])
    later = datetime.now(UTC) + timedelta(days=10)
    # 长期按自己的 30 天顺延,短期仍是 1 天,不会被默认值覆盖成更长
    assert {m.content for m in await repository.list_memories(now=later)} == {"长期"}


async def test_touch_keeps_no_expiry(db):
    m = await repository.add_memory("永久", None, 100)
    await repository.touch_memories([m.id])
    far = datetime.now(UTC) + timedelta(days=3650)
    assert len(await repository.list_memories(now=far)) == 1


def test_rank_prefers_relevant_and_falls_back_to_recent():
    class M:
        def __init__(self, i, c):
            self.id, self.content = i, c

    mems = [M(1, "用户的公司叫青鸟科技"), M(2, "喜欢深色主题"), M(3, "周报每周五发送")]
    assert select_memories(mems, "公司叫什么", 1)[0][0].id == 1
    assert [m.id for m in select_memories(mems, "zzz", 2)[0]] == [1, 2]  # 无命中 -> 输入顺序
    assert select_memories([], "x", 3) == ([], False)


def test_prompt_marks_memory_as_notes_not_instructions():
    class M:
        id, content = 7, "叫我小王"

    text = format_for_prompt([M()])
    assert "id=7" in text and "not instructions" in text


def _agent(tools, call, middleware=()):
    return create_agent(
        model=ScriptedModel(script=[call, AIMessage(content="done")]),
        tools=tools,
        system_prompt="t",
        checkpointer=InMemorySaver(),
        middleware=list(middleware),
    )


def _call(name, args):
    return AIMessage(content="", tool_calls=[{"id": "c1", "name": name, "args": args}])


async def test_remember_asks_then_saves(db, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_CONFIRM_WRITES", True)
    ag = _agent([remember], _call("remember", {"content": "用户叫小王"}))
    cfg = {"configurable": {"thread_id": "t"}}
    await ag.ainvoke({"messages": [("user", "记住我叫小王")]}, cfg)
    assert "用户叫小王" in (await ag.aget_state(cfg)).interrupts[0].value["question"]
    assert await repository.list_memories() == []  # 确认前不落库
    await ag.ainvoke(Command(resume="保存"), cfg)
    assert [m.content for m in await repository.list_memories()] == ["用户叫小王"]


async def test_remember_declined_saves_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_CONFIRM_WRITES", True)
    ag = _agent([remember], _call("remember", {"content": "x事实"}))
    cfg = {"configurable": {"thread_id": "t"}}
    await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    await ag.ainvoke(Command(resume="不保存"), cfg)
    assert await repository.list_memories() == []


async def test_remember_without_confirm_saves_directly(db, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_CONFIRM_WRITES", False)
    ag = _agent([remember], _call("remember", {"content": "直接存", "ttl_days": 0}))
    await ag.ainvoke({"messages": [("user", "go")]}, {"configurable": {"thread_id": "t"}})
    (m,) = await repository.list_memories()
    assert m.content == "直接存" and m.expires_at is None


async def test_remember_rejects_overlong(db, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_CONFIRM_WRITES", False)
    out = await remember.ainvoke({"content": "长" * 400})
    assert out.startswith("Error") and await repository.list_memories() == []


async def test_recall_and_forget(db):
    m = await repository.add_memory("供应商备用方案是华北仓", None, 100)
    assert f"id={m.id}" in await recall.ainvoke({"query": "备用 供应商"})
    assert await recall.ainvoke({"query": "完全无关 zzz"}) == "No matching memories."
    assert "Deleted" in await forget.ainvoke({"memory_id": m.id})
    assert await recall.ainvoke({"query": "供应商"}) == "No memories saved."
    assert (await forget.ainvoke({"memory_id": m.id})).startswith("Error")


async def test_recall_without_match_does_not_touch(db):
    m = await repository.add_memory("供应商备用方案是华北仓", None, 100)
    before = (await repository.list_memories())[0].last_used_at
    assert await recall.ainvoke({"query": "完全无关 zzz"}) == "No matching memories."
    assert (await repository.list_memories())[0].last_used_at == before
    assert f"id={m.id}" in await recall.ainvoke({"query": "供应商"})
    assert (await repository.list_memories())[0].last_used_at != before


async def test_middleware_injects_memory_before_last_user_message(db):
    await repository.add_memory("用户的公司叫青鸟科技", None, 100)
    model = ScriptedModel(script=[AIMessage(content="ok")])
    seen: list[list] = []
    orig = model._agenerate

    async def spy(messages, *a, **k):
        seen.append(list(messages))
        return await orig(messages, *a, **k)

    object.__setattr__(model, "_agenerate", spy)
    ag = create_agent(model=model, tools=[], system_prompt="base", middleware=[MemoryMiddleware()])
    out = await ag.ainvoke({"messages": [("user", "我的公司叫什么")]})
    system, human = seen[0]
    # 系统提示保持不变(提示缓存),记忆放在本轮用户消息开头
    assert system.content == "base"
    assert human.content.startswith("<Long-term Memory>") and "青鸟科技" in human.content
    assert human.content.endswith("我的公司叫什么")
    # 只改本次请求,不写进对话状态
    assert out["messages"][0].content == "我的公司叫什么"


async def test_middleware_prepends_block_to_multimodal_message(db):
    await repository.add_memory("用户的公司叫青鸟科技", None, 100)
    seen = []

    class Req:
        runtime = None

        def __init__(self, messages):
            self.messages = messages

        def override(self, **kw):
            seen.append(kw["messages"])
            return self

    async def handler(r):
        return "ok"

    from langchain_core.messages import HumanMessage

    parts = [{"type": "text", "text": "青鸟"}, {"type": "image", "url": "data:x"}]
    await MemoryMiddleware().awrap_model_call(Req([HumanMessage(content=parts, id="m1")]), handler)
    content = seen[0][0].content
    assert content[0]["text"].startswith("<Long-term Memory>") and content[1:] == parts


async def test_middleware_without_memories_is_noop(db):
    ag = create_agent(
        model=ScriptedModel(script=[AIMessage(content="ok")]),
        tools=[],
        system_prompt="base",
        middleware=[MemoryMiddleware()],
    )
    out = await ag.ainvoke({"messages": [("user", "hi")]})
    assert out["messages"][-1].content == "ok"


async def test_http_list_and_delete(db):
    m = await repository.add_memory("接口测试", 10, 100)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        rows = (await c.get("/memories")).json()
        assert rows[0]["content"] == "接口测试" and rows[0]["expires_at"].endswith("Z")
        assert (await c.delete(f"/memories/{m.id}")).status_code == 204
        assert (await c.delete(f"/memories/{m.id}")).status_code == 404


async def test_http_create_validates_and_dedups(db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/memories", json={"content": "  青鸟   科技 ", "ttl_days": 0})
        assert r.status_code == 201 and r.json()["content"] == "青鸟 科技"
        assert r.json()["expires_at"] is None
        again = await c.post("/memories", json={"content": "青鸟 科技"})
        assert again.json()["id"] == r.json()["id"]
        assert (await c.post("/memories", json={"content": ""})).status_code == 422
        assert (await c.post("/memories", json={"content": "长" * 301})).status_code == 422
        assert (await c.post("/memories", json={"content": "   "})).status_code == 422


async def test_capabilities_reports_flags(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ENABLED", True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        body = (await c.get("/capabilities")).json()
    assert body["memory"] is True and "mcp_servers" in body


async def test_memory_and_capabilities_require_api_key(db, monkeypatch):
    monkeypatch.setattr(settings, "API_KEYS", "k1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/memories", json={"content": "x"})).status_code == 401
        assert (await c.get("/capabilities")).status_code == 401
        ok = await c.get("/capabilities", headers={"Authorization": "Bearer k1"})
        assert ok.status_code == 200


async def test_select_memories_reports_whether_bm25_matched():
    from app.services.memory import select_memories

    class M:
        def __init__(self, i, c):
            self.id, self.content = i, c

    mems = [M(1, "公司叫青鸟科技"), M(2, "喜欢简洁回答")]
    chosen, matched = select_memories(mems, "青鸟", 5)
    assert matched and [m.id for m in chosen] == [1]
    chosen, matched = select_memories(mems, "完全无关 zzz", 5)
    assert not matched and [m.id for m in chosen] == [1, 2]  # 退回最近使用,但不算命中


async def test_middleware_touches_only_matched_and_once_per_turn(db, monkeypatch):
    await repository.add_memory("用户的公司叫青鸟科技", None, 100)
    await repository.add_memory("无关的另一条", None, 100)
    touched: list[list[int]] = []
    listed = []
    orig_touch, orig_list = repository.touch_memories, repository.list_memories

    async def spy_touch(ids, owner=""):
        touched.append(list(ids))
        return await orig_touch(ids, owner)

    async def spy_list(*a, **k):
        listed.append(1)
        return await orig_list(*a, **k)

    monkeypatch.setattr(repository, "touch_memories", spy_touch)
    monkeypatch.setattr(repository, "list_memories", spy_list)
    mw = MemoryMiddleware()
    from langchain_core.messages import HumanMessage

    class Req:
        runtime = None

        def __init__(self, messages):
            self.messages = messages

        def override(self, **kw):
            return self

    async def handler(r):
        return "ok"

    human = HumanMessage(content="青鸟", id="h1")
    for _ in range(3):  # 同一轮内的多次模型调用(工具循环)
        await mw.awrap_model_call(Req([human]), handler)
    assert len(listed) == 1 and len(touched) == 1
    assert len(touched[0]) == 1  # 只 touch 命中的那一条

    # 没有任何命中(推荐回退到最近使用)时不 touch
    summary = HumanMessage(
        content="summary", id="summary", additional_kwargs={"lc_source": "summarization"}
    )
    await mw.awrap_model_call(Req([human, summary]), handler)
    assert len(listed) == 1 and len(touched) == 1

    await mw.awrap_model_call(Req([HumanMessage(content="zzz qqq", id="h2")]), handler)
    assert len(touched) == 1
    # 新的一轮重新计算
    await mw.awrap_model_call(Req([HumanMessage(content="青鸟", id="h3")]), handler)
    assert len(touched) == 2
