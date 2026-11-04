"""chat_operations 里不依赖 LLM/Redis 的纯逻辑。"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage

from app.services import chat_operations as chat
from app.services.chat_operations import (
    _ai_tool_call_event,
    _normalize_custom,
    _TokenBatcher,
    build_agent_query,
)


def test_build_agent_query_without_files():
    assert build_agent_query("hi", []) == "<user_query>\nhi\n</user_query>"


def test_build_agent_query_with_files():
    q = build_agent_query(
        "analyze",
        [{"file_name": "a.csv", "container_path": "/workspace/uploads/a.csv"}],
    )
    assert "<uploaded_files>" in q
    assert "<name>a.csv</name>" in q
    assert "<path>/workspace/uploads/a.csv</path>" in q
    assert q.endswith("</user_query>")


def test_token_batcher_flushes_at_batch_size():
    b = _TokenBatcher(3)
    assert b.add("a") is None
    assert b.add("b") is None
    assert b.add("c") == "abc"
    assert b.flush() is None


def test_token_batcher_manual_flush():
    b = _TokenBatcher(10)
    b.add("x")
    b.add("y")
    assert b.flush() == "xy"
    assert b.flush() is None


def test_normalize_custom_tool_result():
    event, data = _normalize_custom(
        {"kind": "tool.result", "tool_call_id": "t1", "name": "bash_tool", "content": "ok"}
    )
    assert event == "tool.result"
    assert data == {"id": "t1", "name": "bash_tool", "content": "ok", "status": "success"}


def test_normalize_custom_files_and_widgets():
    assert _normalize_custom({"kind": "files.presented", "files": [1]}) == (
        "files.presented",
        {"files": [1]},
    )
    assert _normalize_custom({"kind": "widget.presented", "widgets": [2]}) == (
        "widget.presented",
        {"widgets": [2]},
    )


def test_normalize_custom_ignores_unknown():
    assert _normalize_custom({"kind": "other"}) is None
    assert _normalize_custom("not a dict") is None


def test_ai_tool_call_event():
    msg = AIMessage(
        content="thinking",
        tool_calls=[{"id": "1", "name": "bash_tool", "args": {"command": "ls"}}],
    )
    assert _ai_tool_call_event(msg) == {
        "text": "thinking",
        "tool_calls": [{"id": "1", "name": "bash_tool", "args": {"command": "ls"}}],
    }


async def test_repair_cancelled_tool_calls_adds_missing_tool_messages():
    ai = AIMessage(
        content="",
        tool_calls=[{"id": "call-1", "name": "bash_tool", "args": {}}],
    )
    updates = []

    class FakeAgent:
        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": [ai]})

        async def aupdate_state(self, config, values, as_node=None):
            updates.append((values, as_node))

    config = {"configurable": {"thread_id": "s1"}}
    await chat._repair_cancelled_tool_calls(FakeAgent(), config, chat.USER_CANCELLED_TOOL_MESSAGE)
    # 不是用户取消(出错、超预算后的下一轮兜底)时不能说成用户取消
    await chat._repair_cancelled_tool_calls(FakeAgent(), config)

    assert len(updates) == 2
    values, as_node = updates[0]
    assert as_node == "tools"
    (tool_msg,) = values["messages"]
    assert tool_msg.tool_call_id == "call-1"
    assert tool_msg.content == chat.USER_CANCELLED_TOOL_MESSAGE
    assert updates[1][0]["messages"][0].content == chat.INTERRUPTED_TOOL_MESSAGE


async def test_repair_cancelled_tool_calls_noop_when_answered():
    from langchain_core.messages import ToolMessage

    ai = AIMessage(content="", tool_calls=[{"id": "c", "name": "t", "args": {}}])
    answered = ToolMessage("done", tool_call_id="c")
    called = []

    class FakeAgent:
        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": [ai, answered]})

        async def aupdate_state(self, *a, **k):
            called.append(1)

    await chat._repair_cancelled_tool_calls(FakeAgent(), {"configurable": {"thread_id": "s"}})
    assert called == []
