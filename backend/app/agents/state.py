from typing import Annotated, Literal, NotRequired

from langchain.agents import AgentState
from typing_extensions import TypedDict


class Todo(TypedDict):
    """A structured task item for tracking progress through complex workflows.

    Attributes:
        content: Short, specific description of the task
        status: Current state - pending, in_progress, or completed
    """

    content: str
    status: Literal["pending", "in_progress", "completed"]


def _last_write_wins(_old: list[Todo], new: list[Todo]) -> list[Todo]:
    return new


class DeepAgentState(AgentState):
    """Extended agent state that includes task tracking.

    Inherits from LangGraph's AgentState and adds:
    - todos: List of Todo items for task planning and progress tracking.
      带 reducer:同一次回复里并行调用 write_todos 时取最后一次,而不是让整轮报 InvalidUpdateError。
    """

    todos: NotRequired[Annotated[list[Todo], _last_write_wins]]
