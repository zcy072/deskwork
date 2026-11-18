"""工具抛出的异常交给模型作为错误结果,不中断整轮,并推送 tool.result 事件。"""

from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp

from app.agents import middlewares
from app.agents.middlewares import ToolGuardMiddleware
from app.services.budget import BudgetExceeded
from app.tools import image_tools, review_tools
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
    assert "工具执行失败" in result.content and "MCP session closed" not in result.content
    assert state["messages"][-1].content == "recovered"
    assert events[0]["kind"] == "tool.result" and events[0]["status"] == "error"


@pytest.mark.parametrize("name", ["view_image", "review_document"])
@pytest.mark.parametrize("signal", [BudgetExceeded, GraphBubbleUp])
async def test_internal_model_control_signals_escape_tool(name, signal, monkeypatch):
    runtime = SimpleNamespace(
        config={"configurable": {"thread_id": "s"}}, tool_call_id="c", state={"messages": []}
    )

    async def stop(*args, **kwargs):
        raise signal("stop this run")

    async def image(*args):
        return b"png", "image/png"

    async def pages(*args):
        return [b"jpg"]

    if name == "view_image":
        monkeypatch.setattr(image_tools, "vision_enabled", lambda: True)
        monkeypatch.setattr(image_tools, "call_index_this_turn", lambda *a: 1)
        monkeypatch.setattr(image_tools, "load_image", image)
        monkeypatch.setattr(image_tools, "describe_image", stop)
        invoke = image_tools.view_image.coroutine
    else:
        monkeypatch.setattr(review_tools, "render_pages", pages)
        monkeypatch.setattr(review_tools, "review_pages", stop)
        invoke = review_tools.review_document.coroutine
    with pytest.raises(signal, match="stop this run"):
        await invoke("/workspace/output/result.png", "inspect", runtime)
