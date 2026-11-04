"""外部 MCP 接入:配置解析、真实 stdio 服务器的工具加载与调用、写类工具的人工确认。"""

import json
import sys
from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.middlewares import ToolGuardMiddleware
from app.config import settings
from app.services import mcp_client
from tests.test_ask_user import ScriptedModel

SERVER = str(Path(__file__).parent / "fixtures" / "echo_mcp_server.py")


def _config():
    return json.dumps({"echo": {"transport": "stdio", "command": sys.executable, "args": [SERVER]}})


@pytest.fixture
async def mcp_tools(monkeypatch):
    monkeypatch.setattr(settings, "MCP_SERVERS", _config())
    monkeypatch.setattr(settings, "MCP_CONFIRM_TOOLS", True)
    await mcp_client.init_mcp()
    yield {t.name: t for t in mcp_client.get_mcp_tools()}
    monkeypatch.setattr(settings, "MCP_SERVERS", "")
    await mcp_client.init_mcp()


def test_parse_empty_is_no_servers():
    assert mcp_client.parse_servers("") == {}


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"a": 1}',
        '{"a": {"transport": "stdio"}}',
        '{"a": {"transport": "http"}}',
        '{"a": {"transport": "carrier-pigeon", "url": "x"}}',
    ],
)
def test_parse_rejects_bad_config(raw):
    with pytest.raises(ValueError):
        mcp_client.parse_servers(raw)


async def test_tools_loaded_with_server_prefix(mcp_tools):
    assert set(mcp_tools) == {"echo_shout", "echo_fetch_page", "echo_append_log"}
    assert mcp_client.mcp_status()["echo"].startswith("ok")


async def test_only_readonly_closed_world_tool_skips_confirmation(mcp_tools):
    # 只读但 openWorldHint=true 的工具能把数据带出沙箱,同样要确认
    assert mcp_client.confirm_tool_names() == {"echo_fetch_page", "echo_append_log"}


async def test_real_tool_call_over_stdio(mcp_tools):
    assert "HELLO" in str(await mcp_tools["echo_shout"].ainvoke({"text": "hello"}))


async def test_unreachable_server_is_skipped_not_fatal(monkeypatch):
    bad = {"nope": {"transport": "stdio", "command": "/nonexistent/binary"}}
    monkeypatch.setattr(settings, "MCP_SERVERS", json.dumps(bad))
    await mcp_client.init_mcp()
    assert mcp_client.get_mcp_tools() == []
    assert mcp_client.mcp_status()["nope"].startswith("failed")
    monkeypatch.setattr(settings, "MCP_SERVERS", "")
    await mcp_client.init_mcp()


def _agent(tools, name, args):
    call = AIMessage(content="", tool_calls=[{"id": "c1", "name": name, "args": args}])
    return create_agent(
        model=ScriptedModel(script=[call, AIMessage(content="done")]),
        tools=list(tools.values()),
        system_prompt="t",
        checkpointer=InMemorySaver(),
        middleware=[ToolGuardMiddleware()],
    )


async def test_write_tool_asks_and_runs_when_allowed(mcp_tools):
    ag = _agent(mcp_tools, "echo_append_log", {"line": "x"})
    cfg = {"configurable": {"thread_id": "t"}}
    await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    snap = await ag.aget_state(cfg)
    assert snap.interrupts and "echo_append_log" in snap.interrupts[0].value["question"]
    out = await ag.ainvoke(Command(resume="允许"), cfg)
    assert "logged: x" in str(out["messages"][-2].content)


async def test_write_tool_denied_does_not_run(mcp_tools):
    ag = _agent(mcp_tools, "echo_append_log", {"line": "x"})
    cfg = {"configurable": {"thread_id": "t"}}
    await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    out = await ag.ainvoke(Command(resume="拒绝"), cfg)
    assert out["messages"][-2].status == "error"
    assert "logged" not in str(out["messages"][-2].content)


async def test_readonly_tool_runs_without_asking(mcp_tools):
    ag = _agent(mcp_tools, "echo_shout", {"text": "hi"})
    cfg = {"configurable": {"thread_id": "t"}}
    out = await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    assert not (await ag.aget_state(cfg)).interrupts
    assert "HI" in str(out["messages"][-2].content)


async def test_open_world_readonly_tool_asks(mcp_tools):
    ag = _agent(mcp_tools, "echo_fetch_page", {"url": "https://example.com/?d=secret"})
    cfg = {"configurable": {"thread_id": "t"}}
    await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    snap = await ag.aget_state(cfg)
    assert snap.interrupts and "echo_fetch_page" in snap.interrupts[0].value["question"]


async def test_policy_allow_skips_mcp_confirmation(mcp_tools, monkeypatch):
    monkeypatch.setattr(settings, "TOOL_POLICY", '{"echo_fetch_page": "allow"}')
    ag = _agent(mcp_tools, "echo_fetch_page", {"url": "https://example.com"})
    cfg = {"configurable": {"thread_id": "t"}}
    out = await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    assert not (await ag.aget_state(cfg)).interrupts
    assert "page: https://example.com" in str(out["messages"][-2].content)


async def test_policy_confirm_asks_mcp_tool_once(mcp_tools, monkeypatch):
    """TOOL_POLICY 设为 confirm 的 MCP 工具只问一次,不会再叠加一次外部工具确认。"""
    from langgraph.types import Command

    monkeypatch.setattr(settings, "TOOL_POLICY", '{"echo_fetch_page": "confirm"}')
    ag = _agent(mcp_tools, "echo_fetch_page", {"url": "https://example.com"})
    cfg = {"configurable": {"thread_id": "t"}}
    await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    assert (await ag.aget_state(cfg)).interrupts
    await ag.ainvoke(Command(resume="允许"), cfg)
    assert not (await ag.aget_state(cfg)).interrupts
