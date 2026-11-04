"""工具抛出的异常交给模型作为错误结果,不中断整轮,并推送 tool.result 事件。"""

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from app.agents import middlewares
from app.agents.middlewares import ToolGuardMiddleware
from tests.test_ask_user import ScriptedModel


@tool
def flaky(x: str) -> str:
    """fails"""
    raise ConnectionError("MCP session closed")


async def test_tool_exception_becomes_error_result(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(middlewares, "get_stream_writer", lambda: events.append)
    call = AIMessage(content="", tool_calls=[{"id": "c1", "name": "flaky", "args": {"x": "1"}}])
    ag = create_agent(
        model=ScriptedModel(script=[call, AIMessage(content="recovered")]),
        tools=[flaky],
        system_prompt="test",
        checkpointer=InMemorySaver(),
        middleware=[ToolGuardMiddleware()],
    )
    state = await ag.ainvoke({"messages": [("user", "go")]}, {"configurable": {"thread_id": "t"}})
    result = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    assert result.status == "error"
    assert "ConnectionError: MCP session closed" in result.content
    assert state["messages"][-1].content == "recovered"
    assert events[0]["kind"] == "tool.result" and events[0]["status"] == "error"
