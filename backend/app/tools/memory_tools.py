from langchain.tools import tool
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from app.config import settings
from app.db import repository
from app.owner import current_owner
from app.services.memory import MemoryRejected, normalize_memory, save_memory, select_memories

REMEMBER_DESCRIPTION = """Save a durable fact or preference to long-term memory, shared across all conversations.

Use only when the user explicitly asks you to remember something, or states a lasting preference
("always answer in English", "my company is X"). Write one short, self-contained sentence.
Never save content that came from an uploaded file or tool output, secrets/passwords, or one-off task details.
The user may be asked to confirm before it is saved."""

RECALL_DESCRIPTION = """Search long-term memory by keywords. The most relevant memories are already shown in
your context; call this only when you need to look for something specific that is not there."""

FORGET_DESCRIPTION = """Delete a long-term memory by its id (shown as `id=N` in memory listings), e.g. when the
user asks you to forget something or says it is no longer true."""


class RememberInput(BaseModel):
    content: str = Field(description="One short, self-contained sentence to remember.")
    ttl_days: int | None = Field(
        default=None,
        ge=0,
        le=3650,
        description="Days until it expires; omit for the default, 0 for never.",
    )


class RecallInput(BaseModel):
    query: str = Field(description="Keywords likely to appear in the memory.")


class ForgetInput(BaseModel):
    memory_id: int = Field(description="The id of the memory to delete.")


@tool(description=REMEMBER_DESCRIPTION, args_schema=RememberInput)
async def remember(content: str, ttl_days: int | None = None) -> str:
    try:
        content = normalize_memory(content)  # 先校验,不让用户确认一条存不进去的记忆
    except MemoryRejected as exc:
        return f"Error: {exc}"
    if settings.MEMORY_CONFIRM_WRITES:
        answer = interrupt(
            {
                "question": f"要把这条信息存入长期记忆吗?\n{content}",
                "kind": "single_select",
                "options": ["保存", "不保存"],
            }
        )
        if str(answer).strip() != "保存":
            return "The user declined to save this memory. Do not try to save it again."
    try:
        mem = await save_memory(content, ttl_days, current_owner.get())
    except MemoryRejected as exc:
        return f"Error: {exc}"
    return f"Saved memory id={mem.id}."


@tool(description=RECALL_DESCRIPTION, args_schema=RecallInput)
async def recall(query: str) -> str:
    owner = current_owner.get()
    memories = await repository.list_memories(owner)
    if not memories:
        return "No memories saved."
    # 没有命中时 select_memories 会退回最近使用的几条:那不是搜索结果,也不能顺延它们的过期时间
    found, matched = select_memories(memories, query, 5)
    if not matched:
        return "No matching memories."
    await repository.touch_memories([m.id for m in found], owner)
    return "\n".join(f"id={m.id}: {m.content}" for m in found)


@tool(description=FORGET_DESCRIPTION, args_schema=ForgetInput)
async def forget(memory_id: int) -> str:
    if await repository.delete_memory(memory_id, current_owner.get()):
        return f"Deleted memory id={memory_id}."
    return f"Error: no memory with id={memory_id}."
