import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi import Path as PathParam
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel, Field

from app.agents.checkpointers import get_checkpointer
from app.api.security import get_owner, rate_limit_default, require_api_key
from app.config import settings
from app.db import repository
from app.db import runs as run_store
from app.runtime.sessions import complete_before_cancelling, file_io, sessions
from app.sandbox import CONTAINER_MANAGER
from app.sandbox.session_paths import SessionPaths
from app.schemas import SESSION_ID_PATTERN
from app.services import session_export, session_import
from app.services.chat_operations import ActiveRunError
from app.services.checkpoint_transfer import CheckpointRejected, import_thread
from app.services.conversation_cleanup import delete_conversation_data, discard_uncommitted_import
from app.services.mcp_client import mcp_status
from app.services.memory import MEMORY_MAX_CHARS, MemoryRejected, save_memory
from app.services.usage import aggregate_usage
from app.utils import logger

conversations_router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(require_api_key), Depends(rate_limit_default)],
)

ConversationId = Annotated[str, PathParam(pattern=SESSION_ID_PATTERN)]


def _iso(dt: datetime) -> str:
    """SQLite 读回的 datetime 不带时区,统一按 UTC 输出,前端才能正确换算本地时间。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def _check_owner(request: Request, conversation_id: str) -> None:
    """别人的会话按不存在处理;真不存在的由各路由自己返回 404。"""
    if not await repository.conversation_visible(conversation_id, get_owner(request)):
        raise HTTPException(status_code=404, detail="Conversation not found")


class RenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


@conversations_router.get("")
async def list_conversations(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    rows = await repository.list_conversations(limit, offset, get_owner(request))
    states = await _current_states([c.id for c, _ in rows])
    return [
        {
            "id": c.id,
            "title": c.title,
            "message_count": n,
            "created_at": _iso(c.created_at),
            "updated_at": _iso(c.updated_at),
            # 运行中以 Redis 为准(进程崩溃不会在库里留下假的 running);否则取库里记的最近终态
            "status": "running"
            if states.get(c.id) in run_store.ACTIVE_STATUSES
            else states.get(c.id, c.last_run_status),
            "unread": bool(c.unread) and states.get(c.id) not in run_store.ACTIVE_STATUSES,
        }
        for c, n in rows
    ]


async def _current_states(conversation_ids: list[str]) -> dict[str, str]:
    """Redis 不可用时当作没有在运行,列表仍能返回库里的状态。"""
    try:
        states = await run_store.latest_statuses(conversation_ids)
        for sid in conversation_ids:
            handle = sessions.get(sid)
            if handle and handle.operation == "run":
                states[sid] = "error" if handle.phase == "recovery_required" else "running"
        return states
    except Exception:
        logger.warning("Reading thread statuses for the conversation list failed")
        return {}


@conversations_router.get("/search")
async def search_conversations(
    request: Request,
    q: Annotated[str, Query(min_length=1, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=50)] = 30,
):
    """按标题与消息正文搜索会话;结果带命中片段(标题命中时为空)。"""
    query = q.strip()
    if not query:
        raise HTTPException(status_code=422, detail="q must not be blank")
    rows = await repository.search_conversations(query, get_owner(request), limit)
    return [
        {
            "id": c.id,
            "title": c.title,
            "snippet": snippet,
            "updated_at": _iso(c.updated_at),
        }
        for c, snippet in rows
    ]


IMPORT_CHUNK = 1024 * 1024


async def _spool_upload(file: UploadFile) -> Path:
    """上传的备份包落到临时文件,超过上限就中止。"""
    dest = session_export.new_archive_path()
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file_io(file.file.read, IMPORT_CHUNK):
                size += len(chunk)
                if size > session_import.MAX_ARCHIVE_BYTES:
                    raise HTTPException(status_code=413, detail="备份包过大")
                await file_io(out.write, chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest


@conversations_router.post("/import", status_code=201)
async def import_conversation(request: Request, file: Annotated[UploadFile, File()]):
    """把备份包导入为一个新会话(新 id),不会覆盖已有会话。"""
    owner = get_owner(request)
    new_id = str(uuid.uuid4())
    try:
        # Include upload spooling, IO and rollback in shutdown's operation gate.
        with sessions.operation(new_id, "import"):
            return await _import_conversation(file, new_id, owner)
    except ActiveRunError as exc:
        raise HTTPException(status_code=409, detail="服务正在关闭,请稍后导入") from exc
    except (session_import.ImportRejected, CheckpointRejected) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _import_conversation(file: UploadFile, new_id: str, owner: str) -> dict:
    archive = await _spool_upload(file)
    wrote_checkpoint = False
    try:
        paths = SessionPaths.create(new_id)
        checkpointer = get_checkpointer()
        plan = await file_io(session_import.inspect_archive, archive)
        if plan.checkpoint is not None and not isinstance(checkpointer, AsyncSqliteSaver):
            plan.checkpoint = None
            for m in plan.messages:
                m["checkpoint_id"] = None
            plan.warnings.append("当前服务没有持久化 checkpoint,只导入了消息和文件")
        await complete_before_cancelling(
            asyncio.create_task(CONTAINER_MANAGER.get_or_create(new_id))
        )
        await file_io(session_import.extract_files, archive, plan, paths)
        if plan.checkpoint is not None:
            wrote_checkpoint = True
            await complete_before_cancelling(
                asyncio.create_task(import_thread(checkpointer, new_id, plan.checkpoint))
            )
        # COMMIT may finish after its waiter is cancelled. Settle the whole
        # transaction before the cleanup task checks the durable receipt.
        await complete_before_cancelling(
            asyncio.create_task(
                repository.import_conversation(
                    new_id,
                    owner,
                    title=plan.title,
                    created_at=plan.created_at,
                    updated_at=plan.updated_at,
                    messages=plan.messages,
                    graph_checkpoint_id=plan.graph_checkpoint_id
                    if plan.checkpoint is not None
                    else None,
                )
            )
        )
    except BaseException:
        await complete_before_cancelling(
            asyncio.create_task(discard_uncommitted_import(new_id, wrote_checkpoint))
        )
        raise
    finally:
        archive.unlink(missing_ok=True)
    return {
        "id": new_id,
        "title": plan.title,
        "message_count": len(plan.messages),
        "file_count": len(plan.files),
        "missing": plan.missing,
        "resumable": plan.checkpoint is not None,
        "warnings": plan.warnings,
    }


@conversations_router.get("/{conversation_id}/messages")
async def get_messages(
    request: Request,
    conversation_id: ConversationId,
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    from_id: Annotated[int | None, Query(ge=1)] = None,
):
    """默认返回全部消息;传 limit 则取最近 limit 条(配合 before_id 向前翻页)。
    from_id 只返回 id >= from_id 的消息(刷新已加载的那一段)。"""
    # 别人的会话与不存在的会话都是 404
    messages = await repository.get_messages(
        conversation_id,
        owner=get_owner(request),
        limit=limit,
        before_id=before_id,
        from_id=from_id,
    )
    if messages is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    files = await repository.get_files_for_messages([m.id for m in messages if m.role == "user"])
    run_records = await run_store.for_messages([m.prompt_id for m in messages], conversation_id)
    return [
        {
            "id": m.id,
            "prompt_id": m.prompt_id,
            "role": m.role,
            "content": m.content,
            "parts": repository.normalize_delivered_parts(m.parts),
            "status": m.status,
            "last_event_seq": m.last_event_seq or 0,
            "error_text": run_records[m.prompt_id].public_error
            if m.prompt_id in run_records
            else None,
            "rewindable": m.role == "user" and m.checkpoint_id is not None,
            "created_at": _iso(m.created_at),
            **({"attachments": files.get(m.id, [])} if m.role == "user" else {}),
        }
        for m in messages
    ]


@conversations_router.get("/{conversation_id}/requests/{request_id}")
async def accepted_request(
    request: Request,
    conversation_id: ConversationId,
    request_id: Annotated[str, PathParam(max_length=64)],
):
    run = await run_store.by_request(conversation_id, request_id, get_owner(request))
    if run is None:
        raise HTTPException(404, "Accepted request not found")
    return {"id": run.id, "status": run.status}


@conversations_router.get("/{conversation_id}/assets")
async def get_assets(request: Request, conversation_id: ConversationId):
    """整个会话的上传文件、交付文件和最新计划;消息分页加载时,文件面板靠它补全未加载部分。"""
    summary = await repository.get_conversation_assets(conversation_id, owner=get_owner(request))
    if summary is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return summary


@conversations_router.get("/{conversation_id}/usage")
async def get_usage(request: Request, conversation_id: ConversationId):
    await _check_owner(request, conversation_id)
    rows = await repository.get_usage_rows(conversation_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return aggregate_usage(rows)


@conversations_router.patch("/{conversation_id}")
async def rename_conversation(
    request: Request, conversation_id: ConversationId, body: RenameRequest
):
    await _check_owner(request, conversation_id)
    if not await repository.rename_conversation(conversation_id, body.title):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"id": conversation_id, "title": body.title}


@conversations_router.post("/{conversation_id}/read", status_code=204)
async def mark_conversation_read(request: Request, conversation_id: ConversationId):
    if not await repository.mark_read(conversation_id, get_owner(request)):
        raise HTTPException(status_code=404, detail="Conversation not found")


@conversations_router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(request: Request, conversation_id: ConversationId):
    await _check_owner(request, conversation_id)
    try:
        deleted = await delete_conversation_data(conversation_id)
    except ActiveRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")


memories_router = APIRouter(
    prefix="/memories",
    tags=["memories"],
    dependencies=[Depends(require_api_key), Depends(rate_limit_default)],
)


class MemoryCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=MEMORY_MAX_CHARS)
    ttl_days: int | None = Field(default=None, ge=0, le=3650)  # None=默认,0=永不过期


def _memory_dict(m) -> dict:
    return {
        "id": m.id,
        "content": m.content,
        "created_at": _iso(m.created_at),
        "last_used_at": _iso(m.last_used_at),
        "expires_at": _iso(m.expires_at) if m.expires_at else None,
    }


@memories_router.post("", status_code=201)
async def create_memory(request: Request, body: MemoryCreate):
    """手动添加一条记忆(不经过 Agent 的确认流程,调用者已通过认证)。"""
    try:
        mem = await save_memory(body.content, body.ttl_days, get_owner(request))
    except MemoryRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _memory_dict(mem)


@memories_router.get("")
async def list_memories(request: Request):
    return [_memory_dict(m) for m in await repository.list_memories(get_owner(request))]


@memories_router.delete("/{memory_id}", status_code=204)
async def delete_memory(request: Request, memory_id: int):
    if not await repository.delete_memory(memory_id, get_owner(request)):
        raise HTTPException(status_code=404, detail="Memory not found")


capabilities_router = APIRouter(
    tags=["capabilities"],
    dependencies=[Depends(require_api_key), Depends(rate_limit_default)],
)


@capabilities_router.get("/capabilities")
async def capabilities():
    """当前实例开启了哪些可选能力,供评测等客户端探测。需要认证,不暴露在 /health。"""
    return {
        "memory": settings.MEMORY_ENABLED,
        "memory_confirm_writes": settings.MEMORY_CONFIRM_WRITES,
        "mcp_servers": mcp_status(),
    }
