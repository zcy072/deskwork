from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from app.agents.state import Todo
from app.tools.tool_descriptions import (
    READ_TODOS_DESCRIPTION,
    WRITE_TODOS_DESCRIPTION,
)


def _todo_tool_response(
    runtime: ToolRuntime,
    feedback: str,
    *,
    is_error: bool = False,
    todos: list[Todo] | None = None,
) -> Command:
    update: dict = {
        "messages": [
            ToolMessage(
                feedback,
                tool_call_id=runtime.tool_call_id,
                status="error" if is_error else "success",
            )
        ],
    }
    if todos is not None:
        update["todos"] = todos
    return Command(update=update)


class WriteTodosInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    todos: list[Todo] = Field(description="List of Todo items with content and status")
    runtime: ToolRuntime


class ReadTodosInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    runtime: ToolRuntime


@tool(description=WRITE_TODOS_DESCRIPTION, args_schema=WriteTodosInput)
def write_todos(todos: list[Todo], runtime: ToolRuntime) -> Command:
    return _todo_tool_response(runtime, f"Updated todo list to {todos}", todos=todos)


@tool(description=READ_TODOS_DESCRIPTION, args_schema=ReadTodosInput)
def read_todos(runtime: ToolRuntime) -> Command:
    todos = runtime.state.get("todos", [])
    if not todos:
        return _todo_tool_response(runtime, "No todos currently in the list.")

    status_emoji = {"pending": "⏳", "in_progress": "🔄", "completed": "✅"}
    lines = [
        f"{i}. {status_emoji.get(todo['status'], '❓')} {todo['content']} ({todo['status']})"
        for i, todo in enumerate(todos, 1)
    ]
    return _todo_tool_response(runtime, "Current TODO List:\n" + "\n".join(lines))
