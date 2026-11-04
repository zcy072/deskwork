from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.services.image_view import NOT_CONFIGURED, describe_image, load_image, vision_enabled
from app.tools.turn_limits import call_index_this_turn
from app.utils import logger

TOOL_NAME = "view_image"

VIEW_IMAGE_DESCRIPTION = """Look at an image (png / jpg / jpeg / webp / gif) with a vision model and get a text answer.

Use it for screenshots, photos, charts and scanned pages that the user uploaded to /workspace/uploads
or that you produced in /workspace/output. You cannot see images any other way: `view_file` and
`bash_tool` only return bytes or text. Ask a specific `question` (e.g. "transcribe all text",
"what does the chart show?"); the answer describes only what is visible.

Limits: one image per call, a size cap per image and a cap on calls per user turn. If the tool says
no vision model is configured, tell the user you cannot see the image; never guess its content."""


class ViewImageInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: str = Field(..., description="Path of the image, e.g. /workspace/uploads/screenshot.png")
    question: str = Field(
        default="", description="What to find out about the image; empty = describe it in detail."
    )
    description: str = Field(..., description="How this tool call will be useful.")
    runtime: ToolRuntime


@tool(description=VIEW_IMAGE_DESCRIPTION, args_schema=ViewImageInput)
async def view_image(
    path: str, description: str, runtime: ToolRuntime, question: str = ""
) -> Command:
    session_id = runtime.config.get("configurable", {})["thread_id"]
    status = "error"
    try:
        if not vision_enabled():
            feedback = NOT_CONFIGURED
        elif call_index_this_turn(runtime, (TOOL_NAME,)) > settings.VIEW_IMAGE_MAX_PER_TURN:
            feedback = (
                f"Error: 本轮已查看 {settings.VIEW_IMAGE_MAX_PER_TURN} 张图片,达到上限;"
                "请基于已有结果回答,或请用户在下一条消息里继续。"
            )
        else:
            data, mime = await load_image(session_id, path)
            feedback = await describe_image(data, mime, question)
            if feedback:
                status = "success"
            else:
                feedback = "Error: 视觉模型没有返回内容。"
    except Exception as exc:
        logger.error(f"view_image failed: {exc}")
        feedback = f"Error: {exc}"
    return Command(
        update={
            "messages": [
                ToolMessage(
                    feedback, tool_call_id=runtime.tool_call_id, name=TOOL_NAME, status=status
                )
            ]
        }
    )
