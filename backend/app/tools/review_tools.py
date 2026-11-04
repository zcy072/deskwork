from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from app.services.doc_review import render_pages, review_pages
from app.utils import logger

REVIEW_DOCUMENT_DESCRIPTION = """Visually check a generated deliverable (docx / pptx / xlsx / pdf) for layout problems.

The file is rendered to page images and inspected by a vision model. It returns `VERDICT: PASS`
or `VERDICT: FIX` with a list of concrete problems (overflow, overlap, squeezed tables, unreadable
text, garbled characters, ...).

Use it AFTER creating or editing a document/presentation and BEFORE `present_files`.
If the verdict is FIX, repair the issues and re-check; stop after at most 2 repair rounds
and tell the user about any remaining problems. Do not use it on plain text, CSV or images."""


class ReviewDocumentInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: str = Field(
        ..., description="Path of the file to check, e.g. /workspace/output/report.docx"
    )
    request: str = Field(
        default="", description="One sentence describing what the user asked this file to be."
    )
    description: str = Field(..., description="How this tool call will be useful.")
    runtime: ToolRuntime


@tool(description=REVIEW_DOCUMENT_DESCRIPTION, args_schema=ReviewDocumentInput)
async def review_document(
    path: str, description: str, runtime: ToolRuntime, request: str = ""
) -> Command:
    session_id = runtime.config.get("configurable", {})["thread_id"]
    try:
        pages = await render_pages(session_id, path)
        verdict = await review_pages(pages, kind=path.rsplit(".", 1)[-1], request=request)
        status = "success"
        feedback = f"(checked {len(pages)} page(s))\n{verdict}"
    except Exception as exc:
        logger.error(f"review_document failed: {exc}")
        status = "error"
        feedback = (
            f"Error: {exc}\nThe automatic layout check is unavailable; "
            "continue without it and mention that the layout was not verified."
        )
    return Command(
        update={
            "messages": [ToolMessage(feedback, tool_call_id=runtime.tool_call_id, status=status)]
        }
    )
