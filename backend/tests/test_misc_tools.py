"""todo / display_widget / ask_user_input / 文件工具错误回传的边界路径。"""

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from pydantic import ValidationError

from app.sandbox.errors import ContainerFileSystemError
from app.tools import display_widget, file_tools, read_todos, write_todos


def _call(tool, args, state=None, call_id="c1"):
    return tool.ainvoke({"type": "tool_call", "id": call_id, "name": tool.name, "args": args})


async def test_write_todos_updates_state():
    todos = [{"content": "a", "status": "pending"}]
    cmd = write_todos.func(todos=todos, runtime=_rt())
    assert cmd.update["todos"] == todos
    assert cmd.update["messages"][0].status == "success"


async def test_write_todos_rejects_bad_status():
    with pytest.raises(ValidationError):
        write_todos.args_schema.model_validate(
            {"todos": [{"content": "a", "status": "bogus"}], "runtime": _rt()}
        )


def test_read_todos_empty_and_filled():
    cmd = read_todos.func(runtime=_rt({}))
    assert "No todos" in cmd.update["messages"][0].content
    todos = [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "weird"},
    ]
    text = read_todos.func(runtime=_rt({"todos": todos})).update["messages"][0].content
    assert "1. ✅ a (completed)" in text
    assert "2. ❓ b (weird)" in text


class _RT:
    tool_call_id = "c1"

    def __init__(self, state):
        self.state = state


def _rt(state=None):
    return _RT(state or {})


async def test_display_widget_emits_event_without_state_write(monkeypatch):
    events = []
    from app.tools import visualize_tools

    monkeypatch.setattr(visualize_tools, "get_stream_writer", lambda: events.append)
    cmd = await display_widget.coroutine(
        title="T", html_content="<p>x</p>", description="d", runtime=_rt(), height=300
    )
    assert isinstance(cmd, Command)
    assert events == [
        {
            "kind": "widget.presented",
            "widgets": [{"title": "T", "html_content": "<p>x</p>", "height": 300}],
        }
    ]
    assert set(cmd.update) == {"messages"}
    assert "displayed" in cmd.update["messages"][0].content


async def test_display_widget_default_height(monkeypatch):
    events = []
    from app.tools import visualize_tools

    monkeypatch.setattr(visualize_tools, "get_stream_writer", lambda: events.append)
    await display_widget.coroutine(title="T", html_content="h", description="d", runtime=_rt())
    assert events[0]["widgets"][0]["height"] == 500


async def test_file_tool_errors_become_error_messages(monkeypatch):
    class RT(_RT):
        config = {"configurable": {"thread_id": "t"}}

    async def boom_fs(*a, **k):
        raise ContainerFileSystemError("nope")

    async def boom_any(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(file_tools, "view_container_path_content", boom_fs)
    monkeypatch.setattr(file_tools, "create_container_file", boom_any)
    for tool, kwargs, text in [
        (file_tools.view_file, {"path": "p"}, "Error: nope"),
        (file_tools.create_file, {"path": "p", "file_text": "x"}, "工具执行失败(trace id: -)"),
    ]:
        cmd = await tool.coroutine(description="d", runtime=RT({}), **kwargs)
        msg = cmd.update["messages"][0]
        assert isinstance(msg, ToolMessage)
        assert msg.status == "error" and msg.content == text
