"""同一次模型回复里并行调用写同一状态键的工具,不能让整轮报 InvalidUpdateError。"""

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.state import DeepAgentState
from app.tools import file_tools, write_todos
from tests.test_ask_user import ScriptedModel


def _agent(tools, calls):
    model = ScriptedModel(
        script=[AIMessage(content="", tool_calls=calls), AIMessage(content="done")]
    )
    return create_agent(
        model=model,
        tools=tools,
        system_prompt="test",
        state_schema=DeepAgentState,
        checkpointer=InMemorySaver(),
    )


async def test_parallel_write_todos_keeps_last():
    first = [{"content": "a", "status": "pending"}]
    second = [{"content": "a", "status": "completed"}]
    ag = _agent(
        [write_todos],
        [
            {"id": "t1", "name": "write_todos", "args": {"todos": first}},
            {"id": "t2", "name": "write_todos", "args": {"todos": second}},
        ],
    )
    state = await ag.ainvoke({"messages": [("user", "go")]}, {"configurable": {"thread_id": "t"}})
    assert state["messages"][-1].content == "done"
    assert state["todos"] == second


async def test_parallel_present_files(monkeypatch):
    async def fake_present(session_id, paths):
        return "presented", [{"name": p} for p in paths]

    monkeypatch.setattr(file_tools, "present_container_files", fake_present)
    monkeypatch.setattr(file_tools, "get_stream_writer", lambda: lambda _e: None)
    args = {"description": "show"}
    ag = _agent(
        [file_tools.present_files],
        [
            {"id": "f1", "name": "present_files", "args": {**args, "paths": ["/a"]}},
            {"id": "f2", "name": "present_files", "args": {**args, "paths": ["/b"]}},
        ],
    )
    cfg = {"configurable": {"thread_id": "t", "session_id": "s"}}
    state = await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    assert state["messages"][-1].content == "done"
