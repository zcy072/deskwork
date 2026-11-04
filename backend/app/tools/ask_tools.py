from typing import Literal

from langchain.tools import tool
from langgraph.types import interrupt
from pydantic import BaseModel, Field

ASK_USER_INPUT_DESCRIPTION = """Ask the user a clarifying question and wait for their answer before continuing.

Use when the request is ambiguous, a required detail is missing, or an action could destroy data and needs
confirmation. Do NOT use for things you can find out yourself (read the file, inspect the data) or for
trivial preferences you can reasonably default.

- `single_select`: user picks one option (also use for yes/no confirmation)
- `multi_select`: user picks one or more options
- `free_text`: user types an answer (leave `options` empty)
- Ask one focused question per call, give 2-5 short options, and put your recommended option first.
- The run pauses here; the return value is the user's answer."""


class AskUserInput(BaseModel):
    question: str = Field(description="The question to show the user. One short sentence.")
    type: Literal["single_select", "multi_select", "free_text"] = Field(
        default="single_select", description="How the user answers."
    )
    options: list[str] = Field(
        default_factory=list,
        description="Choices for select types (2-5 short labels). Empty for free_text.",
    )


@tool(description=ASK_USER_INPUT_DESCRIPTION, args_schema=AskUserInput)
async def ask_user_input(
    question: str, type: str = "single_select", options: list[str] | None = None
) -> str:
    answer = interrupt({"question": question, "kind": type, "options": options or []})
    return f"User answered: {answer}"
