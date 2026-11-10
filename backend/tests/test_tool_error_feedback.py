"""R16 回归:未知异常的原文不进工具结果;写给模型看的错误仍原样透出。"""

from types import SimpleNamespace

import pytest

from app.services.tool_feedback import ToolFeedbackError
from app.tools import image_tools, review_tools, search_tools

MARKER = "PRIVATE_INTERNAL_SENTINEL"


def _runtime():
    return SimpleNamespace(
        config={"configurable": {"thread_id": "s"}}, tool_call_id="c", state={"messages": []}
    )


async def _call(name, monkeypatch, error):
    async def fail(*args, **kwargs):
        raise error

    if name == "view_image":
        monkeypatch.setattr(image_tools, "vision_enabled", lambda: True)
        monkeypatch.setattr(image_tools, "call_index_this_turn", lambda *a: 1)
        monkeypatch.setattr(image_tools, "load_image", fail)
        result = await image_tools.view_image.coroutine(
            "/workspace/uploads/a.png", "inspect", _runtime()
        )
    elif name == "review_document":
        monkeypatch.setattr(review_tools, "render_pages", fail)
        result = await review_tools.review_document.coroutine(
            "/workspace/output/a.pdf", "inspect", _runtime()
        )
    else:
        monkeypatch.setattr(search_tools, "get_index", fail)
        result = await search_tools.search_document.coroutine(
            "/workspace/uploads/a.txt", "q", "inspect", _runtime()
        )
    return result.update["messages"][0]


TOOLS = ["view_image", "review_document", "search_document"]


@pytest.mark.parametrize("name", TOOLS)
async def test_unknown_tool_errors_do_not_expose_internal_details(name, monkeypatch):
    message = await _call(name, monkeypatch, RuntimeError(MARKER))
    assert MARKER not in message.content
    assert "trace id" in message.content
    assert message.status == "error"


@pytest.mark.parametrize("name", TOOLS)
async def test_intended_tool_errors_reach_the_model(name, monkeypatch):
    message = await _call(name, monkeypatch, ToolFeedbackError("File not found: /workspace/x"))
    assert "File not found: /workspace/x" in message.content
