"""编辑重发 / 重新生成:checkpoint 回退、消息截断、旧库补列。

回退到某条消息走 start_chat(rewind_to_message_id=...) 的生产路径;并行回答、快照等
更复杂的回退场景在 test_run_recovery / test_rewind_parallel_answers。"""

import sqlite3

import pytest
from langchain_core.messages import AIMessage

from app.config import settings
from app.db import close_db, init_db, repository, runs
from app.runtime.sessions import sessions
from app.services import chat_operations as chat
from tests.history_seed import seed_legacy_turn, seed_user_turn
from tests.test_run_recovery import runtime as _runtime


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    await init_db()
    yield
    await close_db()


rewind_runtime = pytest.fixture(_runtime.__wrapped__, name="rewind_runtime")


async def _user_ids(sid):
    return [m.id for m in await repository.get_messages(sid) if m.role == "user"]


async def _texts(sid):
    """图里的消息正文;用户输入在图里带 <user_query> 包装,这里剥掉。"""
    state = await chat.get_agent().aget_state(await runs.graph_config(sid))
    return [
        m.content.removeprefix("<user_query>\n").removesuffix("\n</user_query>")
        for m in state.values.get("messages", [])
    ]


async def _turn(text, files=(), **kwargs):
    """经 start_chat 跑完整一轮(运行回执、checkpoint、快照都走生产路径)。"""
    pid = await chat.start_chat("s", text, [], **kwargs)
    await sessions.get("s").task
    return await runs.get(pid)


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
        await seed_legacy_turn("s", "p", "hi")
        msg = (await repository.get_messages("s"))[0]
        assert msg.checkpoint_id is None
    finally:
        await close_db()


# ---------- repository ----------


async def test_truncate_from_removes_tail_and_file_rows(db):
    await seed_legacy_turn("s", "p1", "a", files=[{"file_name": "x", "container_path": "/w/x"}])
    await seed_legacy_turn("s", "p2", "b", files=[{"file_name": "y", "container_path": "/w/y"}])
    first, second = await _user_ids("s")

    assert await repository.truncate_from("s", second) == 1
    assert [m.id for m in await repository.get_messages("s")] == [first]
    assert await repository.get_message_files(first) == [
        {"file_name": "x", "container_path": "/w/x"}
    ]
    assert await repository.get_message_files(second) == []


async def test_get_message_checks_conversation(db):
    await seed_user_turn("s", "p", "a", [])
    (mid,) = await _user_ids("s")
    assert await repository.get_message("s", mid) is not None
    assert await repository.get_message("other", mid) is None


# ---------- 编辑重发(start_chat 的 rewind_to_message_id) ----------


async def test_rewind_to_middle_message_keeps_earlier_context(rewind_runtime):
    rewind_runtime.graph([AIMessage(content=f"r{i}") for i in range(1, 4)])
    await _turn("a")
    await _turn("b")
    _, second = await _user_ids("s")
    assert await _texts("s") == ["a", "r1", "b", "r2"]

    run = await _turn("b2", rewind_to_message_id=second)

    assert run.status == "complete"
    assert await _texts("s") == ["a", "r1", "b2", "r3"]
    assert [m.content for m in await repository.get_messages("s")] == ["a", "r1", "b2", "r3"]


async def test_rewind_to_first_message_replaces_whole_thread(rewind_runtime):
    rewind_runtime.graph([AIMessage(content="r1"), AIMessage(content="r2")])
    await _turn("a")
    (first,) = await _user_ids("s")

    await _turn("a2", rewind_to_message_id=first)

    assert await _texts("s") == ["a2", "r2"]
    assert [m.content for m in await repository.get_messages("s")] == ["a2", "r2"]


async def test_rewind_rejects_assistant_unknown_and_foreign_messages(rewind_runtime):
    await _turn("a")
    assistant = next(m for m in await repository.get_messages("s") if m.role == "assistant")
    for target in (assistant.id, 9999):
        with pytest.raises(chat.RewindError):
            await chat.start_chat("s", "x", [], rewind_to_message_id=target)
    await chat.start_chat("other", "o", [])
    await sessions.get("other").task
    (foreign,) = await _user_ids("other")
    with pytest.raises(chat.RewindError):
        await chat.start_chat("s", "x", [], rewind_to_message_id=foreign)
    assert [m.content for m in await repository.get_messages("s")] == ["a", "done"]


async def test_rewind_rejects_legacy_message_without_checkpoint(rewind_runtime):
    await seed_legacy_turn("s", "legacy", "old")
    (legacy,) = await _user_ids("s")
    with pytest.raises(chat.RewindError, match="不能回退"):
        await chat.start_chat("s", "x", [], rewind_to_message_id=legacy)
