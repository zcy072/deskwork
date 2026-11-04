"""工具权限分级:TOOL_POLICY 的 deny / confirm / allow。"""

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.middlewares import ToolGuardMiddleware
from app.agents.permissions import load_policy, parse_policy, policy_for, unknown_tools
from app.config import settings
from tests.test_ask_user import ScriptedModel

RAN: list[str] = []


@tool
def display_widget(title: str) -> str:
    """show"""
    RAN.append(title)
    return "ok"


def _agent():
    call = AIMessage(
        content="",
        tool_calls=[{"id": "c1", "name": "display_widget", "args": {"title": "x"}}],
    )
    return create_agent(
        model=ScriptedModel(script=[call, AIMessage(content="done")]),
        tools=[display_widget],
        system_prompt="test",
        checkpointer=InMemorySaver(),
        middleware=[ToolGuardMiddleware()],
    )


@pytest.fixture(autouse=True)
def _clear():
    RAN.clear()


@pytest.mark.parametrize("raw", ["not json", "[1]", '{"a": "deny", "b": "maybe"}'])
def test_parse_policy_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        parse_policy(raw)


@pytest.mark.parametrize("raw", ["not json", '{"a": "deny", "b": "maybe"}'])
def test_invalid_policy_denies_every_tool(monkeypatch, raw):
    """限制性配置写错时宁可全拦,不能静默变成不限制。"""
    monkeypatch.setattr(settings, "TOOL_POLICY", raw)
    assert load_policy() is None
    assert policy_for("view_file") == "deny"


def test_valid_policy_and_unknown_names(monkeypatch):
    monkeypatch.setattr(
        settings, "TOOL_POLICY", '{"a": "deny", "c": "allow", "bash_tol": "confirm"}'
    )
    assert load_policy() == {"a": "deny", "c": "allow", "bash_tol": "confirm"}
    assert policy_for("a") == "deny" and policy_for("zzz") is None
    assert unknown_tools(["a", "c", "bash_tool"]) == ["bash_tol"]
    monkeypatch.setattr(settings, "TOOL_POLICY", "")
    assert policy_for("a") is None


async def test_unconfigured_tool_runs_without_prompt(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_POLICY", "")
    cfg = {"configurable": {"thread_id": "t1"}}
    await _agent().ainvoke({"messages": [("user", "go")]}, cfg)
    assert RAN == ["x"]


async def test_deny_blocks_without_interrupt(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_POLICY", '{"display_widget": "deny"}')
    ag = _agent()
    cfg = {"configurable": {"thread_id": "t2"}}
    await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    assert not (await ag.aget_state(cfg)).interrupts
    assert RAN == []


@pytest.mark.parametrize("answer,ran", [("允许", ["x"]), ("拒绝", [])])
async def test_confirm_asks_then_obeys(monkeypatch, answer, ran):
    monkeypatch.setattr(settings, "TOOL_POLICY", '{"display_widget": "confirm"}')
    ag = _agent()
    cfg = {"configurable": {"thread_id": "t3"}}
    await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    snap = await ag.aget_state(cfg)
    assert snap.interrupts and "display_widget" in snap.interrupts[0].value["question"]
    await ag.ainvoke(Command(resume=answer), cfg)
    assert RAN == ran


async def test_policy_confirm_on_bash_asks_once_for_risky_command(monkeypatch):
    """bash_tool 设为 confirm 时,高风险命令只问一次(策略确认里已经展示了命令)。"""
    monkeypatch.setattr(settings, "TOOL_POLICY", '{"bash_tool": "confirm"}')
    monkeypatch.setattr(settings, "CONFIRM_RISKY_COMMANDS", True)

    @tool
    def bash_tool(command: str) -> str:
        """run"""
        RAN.append(command)
        return "ok"

    call = AIMessage(
        content="",
        tool_calls=[
            {"id": "c1", "name": "bash_tool", "args": {"command": "rm -rf /workspace/output"}}
        ],
    )
    ag = create_agent(
        model=ScriptedModel(script=[call, AIMessage(content="done")]),
        tools=[bash_tool],
        system_prompt="test",
        checkpointer=InMemorySaver(),
        middleware=[ToolGuardMiddleware()],
    )
    cfg = {"configurable": {"thread_id": "t4"}}
    await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    assert "bash_tool" in (await ag.aget_state(cfg)).interrupts[0].value["question"]
    await ag.ainvoke(Command(resume="允许"), cfg)
    assert not (await ag.aget_state(cfg)).interrupts
    assert RAN == ["rm -rf /workspace/output"]
