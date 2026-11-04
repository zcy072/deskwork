from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.config import get_stream_writer
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field


class DisplayWidgetInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    title: str = Field(..., description="Title displayed above the visualization widget.")
    html_content: str = Field(
        ...,
        description="Complete, self-contained HTML string to render.",
    )
    description: str = Field(..., description="Short explanation of what this widget visualizes.")
    runtime: ToolRuntime
    height: int = Field(default=500, description="Pixel height for the widget iframe.")


@tool(
    description=(
        "Display an HTML visualization widget inline in the chat. "
        "Use after receiving HTML from a sub-agent or constructing HTML yourself."
    ),
    args_schema=DisplayWidgetInput,
)
async def display_widget(
    title: str,
    html_content: str,
    description: str,
    runtime: ToolRuntime,
    height: int = 500,
) -> Command:
    """Display an HTML visualization widget inline in the chat."""
    widget = {
        "title": title,
        "html_content": html_content,
        "height": height,
    }
    get_stream_writer()({"kind": "widget.presented", "widgets": [widget]})
    msg = f"Widget '{title}' displayed successfully."
    return Command(
        update={
            "messages": [ToolMessage(msg, tool_call_id=runtime.tool_call_id)],
        }
    )
