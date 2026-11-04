"""测试用的最小 MCP 服务器(stdio):只读封闭、只读但开放世界、有副作用各一个工具。"""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("echo")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def shout(text: str) -> str:
    """Uppercase the text."""
    return text.upper()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def fetch_page(url: str) -> str:
    """Pretend to fetch a URL (read-only, but reaches the outside world)."""
    return f"page: {url}"


@mcp.tool()
def append_log(line: str) -> str:
    """Pretend to write a line somewhere."""
    return f"logged: {line}"


if __name__ == "__main__":
    mcp.run("stdio")
