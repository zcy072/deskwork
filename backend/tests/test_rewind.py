"""编辑重发 / 重新生成:checkpoint 回退、消息截断、旧库补列。"""

import sqlite3

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.config import settings
from app.db import close_db, init_db, repository, runs
from app.services import chat_operations as chat
from app.services.history import TurnRecorder, persist_assistant_turn, persist_user_turn


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    await init_db()
    yield
    await close_db()


def _replies():
    i = 0
    while True:
        i += 1
        yield AIMessage(content=f"r{i}")


@pytest.fixture
def agent(monkeypatch):
    saver = InMemorySaver()
    ag = create_agent(model=GenericFakeChatModel(messages=_replies()), tools=[], checkpointer=saver)
    monkeypatch.setattr(chat, "get_agent", lambda: ag)
    return ag


async def _texts(agent, sid):
    state = await agent.aget_state(await runs.graph_config(sid))
    return [m.content for m in state.values.get("messages", [])]


async def _turn(agent, sid, text, files=()):
    """模拟 start_chat + _run_agent 的落库与 checkpoint 记录,不经过 Redis。"""
    config = await runs.graph_config(sid)
    prompt_id = f"p-{text}"
    await persist_user_turn(sid, prompt_id, text, list(files))
    await chat._record_checkpoint(agent, config, sid, prompt_id)
    await agent.ainvoke({"messages": [HumanMessage(text)]}, config)
    from app.db import get_session
    from app.db.models import Conversation

    async with get_session() as db_session:
        conversation = await db_session.get(Conversation, sid)
        conversation.graph_checkpoint_id = None
        await db_session.commit()
    r = TurnRecorder()
    r.record("ai.token", {"text": f"reply-{text}"})
    await persist_assistant_turn(sid, prompt_id, r, "complete")


async def _user_ids(sid):
    return [m.id for m in await repository.get_messages(sid) if m.role == "user"]


# ---------- 旧库补列 ----------


async def test_init_db_adds_missing_checkpoint_column(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    await init_db()
    await close_db()

    db_file = next(tmp_path.glob("*.sqlite*"), None) or next(tmp_path.glob("*.db"))
    con = sqlite3.connect(db_file)
    con.executescript(
        "ALTER TABLE messages DROP COLUMN checkpoint_id;"  # 还原成改动前的表结构
    )
    con.commit()
    con.close()

    await init_db()  # 应自动补回
    try:
        await persist_user_turn("s", "p", "hi", [])
        msg = (await repository.get_messages("s"))[0]
        assert msg.checkpoint_id is None
    finally:
        await close_db()


# ---------- repository ----------


async def test_truncate_from_removes_tail_and_file_rows(db):
    await persist_user_turn("s", "p1", "a", [{"file_name": "x", "container_path": "/w/x"}])
    await persist_user_turn("s", "p2", "b", [{"file_name": "y", "container_path": "/w/y"}])
    first, second = await _user_ids("s")

    assert await repository.truncate_from("s", second) == 1
    assert [m.id for m in await repository.get_messages("s")] == [first]
    assert await repository.get_message_files(first) == [
        {"file_name": "x", "container_path": "/w/x"}
    ]
    assert await repository.get_message_files(second) == []


async def test_get_message_checks_conversation(db):
    await persist_user_turn("s", "p", "a", [])
    (mid,) = await _user_ids("s")
    assert await repository.get_message("s", mid) is not None
    assert await repository.get_message("other", mid) is None


# ---------- _rewind ----------


async def test_rewind_to_middle_message_keeps_earlier_context(db, agent):
    await _turn(agent, "s", "a")
    await _turn(agent, "s", "b")
    _, second = await _user_ids("s")
    assert await _texts(agent, "s") == ["a", "r1", "b", "r2"]

    await chat._rewind("s", second)

    assert await _texts(agent, "s") == ["a", "r1"]
    remaining = await repository.get_messages("s")
    assert [m.content for m in remaining] == ["a", "reply-a"]


async def test_rewind_to_first_message_clears_thread(db, agent):
    await _turn(agent, "s", "a")
    (first,) = await _user_ids("s")

    await chat._rewind("s", first)

    assert await _texts(agent, "s") == []
    assert await repository.get_messages("s") == []


async def test_rewind_then_continue_replaces_branch(db, agent):
    await _turn(agent, "s", "a")
    await _turn(agent, "s", "b")
    _, second = await _user_ids("s")

    await chat._rewind("s", second)
    await _turn(agent, "s", "b2")

    assert await _texts(agent, "s") == ["a", "r1", "b2", "r3"]


async def test_rewind_returns_original_attachments(db, agent):
    await _turn(agent, "s", "a", [{"file_name": "f.csv", "container_path": "/w/f.csv"}])
    (first,) = await _user_ids("s")

    assert await chat._rewind("s", first) == [{"file_name": "f.csv", "container_path": "/w/f.csv"}]


async def test_rewind_rejects_assistant_unknown_and_untracked(db, agent):
    await _turn(agent, "s", "a")
    assistant = next(m for m in await repository.get_messages("s") if m.role == "assistant")
    with pytest.raises(chat.RewindError):
        await chat._rewind("s", assistant.id)
    with pytest.raises(chat.RewindError):
        await chat._rewind("s", 9999)

    # 旧数据:没有记录过 checkpoint
    await persist_user_turn("old", "p", "legacy", [])
    (legacy,) = await _user_ids("old")
    with pytest.raises(chat.RewindError, match="cannot be rewound"):
        await chat._rewind("old", legacy)


async def test_rewind_rejects_message_of_another_conversation(db, agent):
    await _turn(agent, "s", "a")
    (mid,) = await _user_ids("s")
    with pytest.raises(chat.RewindError):
        await chat._rewind("other", mid)
