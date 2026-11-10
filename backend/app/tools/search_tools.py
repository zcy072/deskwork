from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.config import get_stream_writer
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from app.agents.context import session_id_of
from app.services.doc_search import (
    Bm25Index,
    format_results,
    get_index,
    search_index,
    source_references,
)
from app.utils import logger

SEARCH_DOCUMENT_DESCRIPTION = """Search a long document by keywords and return the most relevant passages.

Use it when a file is too long to read in full (roughly more than a few thousand characters) and the
question is about specific facts: names, numbers, dates, clauses. Works on txt/md/csv and, via
conversion, pdf/docx/pptx/xlsx. It is keyword (BM25) search, plus semantic matching when embeddings are configured: put words that are
likely to appear in the original text in `query`, and retry with synonyms if nothing is found. If still nothing,
say the document does not seem to contain it rather than guessing.

It cannot count or aggregate across the whole document; for that, process the file with bash/python."""
SEARCH_DOCUMENT_DESCRIPTION += """
PDF, pptx and plain-text results include original page/slide/line links when available.
When using a passage to support an answer, copy its citation link exactly next to the supported claim.
Never invent source IDs or page numbers. An empty PDF may be scanned and need OCR."""


class SearchDocumentInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: str = Field(..., description="File to search, e.g. /workspace/uploads/report.pdf")
    query: str = Field(..., description="Keywords likely to appear in the relevant passage.")
    top_k: int = Field(default=5, ge=1, le=10, description="Number of passages to return.")
    description: str = Field(..., description="How this tool call will be useful.")
    runtime: ToolRuntime


@tool(description=SEARCH_DOCUMENT_DESCRIPTION, args_schema=SearchDocumentInput)
async def search_document(
    path: str, query: str, description: str, runtime: ToolRuntime, top_k: int = 5
) -> Command:
    session_id = session_id_of(runtime)
    try:
        index: Bm25Index = await get_index(session_id, path)
        results = await search_index(index, query, top_k)
        sources = source_references(index, results)
        feedback = format_results(results, len(index.chunks), sources)
        if sources:
            get_stream_writer()({"kind": "sources.presented", "sources": sources})
        status = "success"
    except Exception as exc:
        logger.error(f"search_document failed: {exc}")
        feedback, status = f"Error: {exc}", "error"
    return Command(
        update={
            "messages": [ToolMessage(feedback, tool_call_id=runtime.tool_call_id, status=status)]
        }
    )
