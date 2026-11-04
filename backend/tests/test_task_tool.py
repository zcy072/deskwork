"""task 工具:子 Agent 同样受工具防护约束,失败和列表型内容有正确的返回。"""

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.state import DeepAgentState
from app.services.image_view import message_text
from app.tools.task_tools import create_task_tool
from tests.test_ask_user import ScriptedModel

RAN: list[str] = []


@tool
def bash_tool(command: str) -> str:
    """run"""
    RAN.append(command)
    return "ok"


SUB = {
    "name": "helper",
    "description": "test helper",
    "system_prompt": "help",
    "tools": ["bash_tool"],
}


def _call(name, args, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": args}])


def _parent(sub_script):
    task = create_task_tool([bash_tool], [SUB], ScriptedModel(script=sub_script), DeepAgentState)
    parent = ScriptedModel(
        script=[
            _call("task", {"description": "do it", "subagent_type": "helper"}, "p1"),
            AIMessage(content="done"),
        ]
    )
    return create_agent(
        model=parent,
        tools=[task],
        system_prompt="test",
        state_schema=DeepAgentState,
        checkpointer=InMemorySaver(),
    )


async def _run(ag, payload):
    cfg = {"configurable": {"thread_id": "t"}}
    return await ag.ainvoke(payload, cfg), cfg


def _task_result(state):
    return next(m for m in state["messages"] if getattr(m, "tool_call_id", None) == "p1")


async def test_subagent_risky_command_interrupts_and_denial_blocks_it():
    RAN.clear()
    ag = _parent(
        [_call("bash_tool", {"command": "rm -f /workspace/uploads/a.docx"}), AIMessage(content="x")]
    )
    state, cfg = await _run(ag, {"messages": [("user", "go")]})
    assert "__interrupt__" in state
    assert RAN == []
    state = await ag.ainvoke(Command(resume="拒绝"), cfg)
    assert RAN == []
    assert _task_result(state).status != "error"  # 子 Agent 正常结束,命令没跑


async def test_subagent_safe_command_runs_without_prompt():
    RAN.clear()
    ag = _parent([_call("bash_tool", {"command": "ls /workspace"}), AIMessage(content="listed")])
    state, _ = await _run(ag, {"messages": [("user", "go")]})
    assert "__interrupt__" not in state
    assert RAN == ["ls /workspace"]
    assert _task_result(state).content == "listed"


async def test_list_content_blocks_are_flattened():
    ag = _parent([AIMessage(content=[{"type": "text", "text": "<p>hi</p>"}])])
    state, _ = await _run(ag, {"messages": [("user", "go")]})
    assert _task_result(state).content == "<p>hi</p>"


async def test_empty_subagent_reply_is_an_error():
    ag = _parent([AIMessage(content="")])
    state, _ = await _run(ag, {"messages": [("user", "go")]})
    result = _task_result(state)
    assert result.status == "error" and "no text" in result.content


async def test_unknown_subagent_type_is_an_error():
    task = create_task_tool([bash_tool], [SUB], ScriptedModel(script=[]), DeepAgentState)
    parent = ScriptedModel(
        script=[
            _call("task", {"description": "x", "subagent_type": "nope"}, "p1"),
            AIMessage(content="ok"),
        ]
    )
    ag = create_agent(model=parent, tools=[task], state_schema=DeepAgentState)
    state = await ag.ainvoke({"messages": [("user", "go")]})
    assert _task_result(state).status == "error"


def test_task_description_lists_agents_one_per_line():
    task = create_task_tool([bash_tool], [SUB], ScriptedModel(script=[]), DeepAgentState)
    assert "\n- helper: test helper" in task.description
    assert "['" not in task.description


def test_message_text_handles_strings_and_blocks():
    assert message_text("  a ") == "a"
    assert message_text([{"type": "text", "text": "a"}, {"type": "tool_use"}, "b"]) == "ab"
    assert message_text(None) == ""


async def test_runaway_subagent_is_stopped_with_an_error_result():
    """子 Agent 不进主 Agent 的预算统计,必须自己有步数上限,否则会无限循环。"""
    RAN.clear()
    loop = [_call("bash_tool", {"command": f"echo {i}"}, f"s{i}") for i in range(500)]
    ag = _parent(loop)
    state, _ = await _run(ag, {"messages": [("user", "go")]})
    result = _task_result(state)
    assert result.status == "error"
    assert 0 < len(RAN) < 100
