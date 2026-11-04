"""作为客户端接入外部 MCP 服务器,把它们的工具并入 Agent。

配置见 settings.MCP_SERVERS。启动时(lifespan)加载一次工具列表;某个服务器连不上只记日志并跳过,
不影响后端启动。注意 langchain-mcp-adapters 每次工具调用都会新建一个会话,stdio 服务器因此
每次调用都会重新启动进程;适合无状态的工具,不适合依赖进程内状态的服务器。
"""

import asyncio
import json
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.config import settings
from app.utils import logger

_tools: list[BaseTool] = []
_tool_names: set[str] = set()
_confirm_names: set[str] = set()
_server_status: dict[str, str] = {}


def parse_servers(raw: str) -> dict[str, dict[str, Any]]:
    """解析 MCP_SERVERS;格式不对或某项缺少必要字段时抛 ValueError,避免静默忽略配置错误。"""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"MCP_SERVERS 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("MCP_SERVERS 必须是 JSON 对象:服务名 -> 连接配置")
    for name, conn in data.items():
        if not isinstance(conn, dict):
            raise ValueError(f"MCP 服务 {name!r} 的配置必须是对象")
        transport = conn.get("transport")
        if transport == "stdio":
            if not conn.get("command"):
                raise ValueError(f"MCP 服务 {name!r}(stdio)缺少 command")
        elif transport in ("http", "streamable_http", "sse"):
            if not conn.get("url"):
                raise ValueError(f"MCP 服务 {name!r}({transport})缺少 url")
        else:
            raise ValueError(f"MCP 服务 {name!r} 的 transport 必须是 stdio / http / sse")
    return data


def needs_confirmation(tool: BaseTool) -> bool:
    """只有明确声明 readOnlyHint=true 且 openWorldHint=false 的工具才免确认,没声明就按有副作用处理。

    只读但面向开放世界的工具(如抓取任意 URL)也要确认:沙箱断网,它是 agent 唯一能访问外部的通道,
    文档里的注入指令可以让它把数据拼进 URL 发出去。MCP 规范中 openWorldHint 缺省为 true。
    """
    meta = tool.metadata or {}
    return not (meta.get("readOnlyHint") is True and meta.get("openWorldHint") is False)


async def init_mcp() -> None:
    _tools.clear()
    _tool_names.clear()
    _confirm_names.clear()
    _server_status.clear()
    try:
        servers = parse_servers(settings.MCP_SERVERS)
    except ValueError as exc:
        logger.error("MCP 配置无效,已忽略: %s", exc)
        return
    if not servers:
        return
    client = MultiServerMCPClient(servers, tool_name_prefix=True)
    for name in servers:
        try:
            tools = await asyncio.wait_for(
                client.get_tools(server_name=name), timeout=settings.MCP_CONNECT_TIMEOUT
            )
        except Exception as exc:
            _server_status[name] = f"failed: {type(exc).__name__}: {exc}"
            logger.error("MCP 服务 %s 加载失败: %s: %s", name, type(exc).__name__, exc)
            continue
        _tools.extend(tools)
        _tool_names.update(t.name for t in tools)
        if settings.MCP_CONFIRM_TOOLS:
            _confirm_names.update(t.name for t in tools if needs_confirmation(t))
        _server_status[name] = f"ok: {len(tools)} tools"
        logger.info("MCP 服务 %s 已接入 %d 个工具", name, len(tools))


def get_mcp_tools() -> list[BaseTool]:
    return list(_tools)


def mcp_tool_names() -> frozenset[str]:
    """所有外部 MCP 工具名(含只读的):它们的返回值都来自外部系统。"""
    return frozenset(_tool_names)


def confirm_tool_names() -> set[str]:
    return set(_confirm_names)


def mcp_status() -> dict[str, str]:
    return dict(_server_status)
