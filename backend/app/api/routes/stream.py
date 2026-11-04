import asyncio
import hashlib
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app.agents.checkpointers import get_checkpointer
from app.api.security import get_owner, rate_limit_default, rate_limit_send, require_api_key
from app.config import settings
from app.db import is_db_ready, repository
from app.sandbox.quota import QuotaExceededError
from app.sandbox.session_paths import SessionPaths
from app.schemas import SESSION_ID_PATTERN, StreamEventsRequest
from app.services import session_export
from app.services.chat_operations import (
    ActiveRunError,
    RewindError,
    TooManyRunsError,
    cancel_chat,
    get_active_prompt_id,
    start_chat,
    stream_chat_events,
)
from app.services.checkpoint_transfer import CheckpointRejected, export_thread
from app.services.document_sources import read_source
from app.services.file_operations import InMemoryUpload
from app.services.file_preview import PreviewError, office_to_pdf
from app.services.file_versions import find_delivered, list_versions, read_version
from app.services.storage import (
    clear_history_versions,
    clear_scratchpad,
    delete_session_file,
    list_session_files,
    storage_breakdown,
)
from app.utils import get_thread_status, get_trace_id, logger

stream_router = APIRouter(
    tags=["stream"], dependencies=[Depends(require_api_key), Depends(rate_limit_default)]
)

_MB = 1024 * 1024


async def _read_uploads(files: list[UploadFile]) -> list[InMemoryUpload]:
    """分块读取并边读边计数,超过数量/单文件/总量上限就 413,不会先把超大文件整个读进内存。"""
    if len(files) > settings.UPLOAD_MAX_FILES:
        raise HTTPException(413, f"一次最多上传 {settings.UPLOAD_MAX_FILES} 个文件")
    per_file, total_cap = settings.UPLOAD_MAX_FILE_MB * _MB, settings.UPLOAD_MAX_TOTAL_MB * _MB
    uploads, total = [], 0
    for f in files:
        chunks, size = [], 0
        while chunk := await f.read(_MB):
            size += len(chunk)
            total += len(chunk)
            if size > per_file:
                raise HTTPException(
                    413, f"文件 {f.filename} 超过单文件上限 {settings.UPLOAD_MAX_FILE_MB}MB"
                )
            if total > total_cap:
                raise HTTPException(413, f"上传总量超过 {settings.UPLOAD_MAX_TOTAL_MB}MB")
            chunks.append(chunk)
        uploads.append(
            InMemoryUpload(
                filename=f.filename or "upload",
                content=b"".join(chunks),
                content_type=f.content_type or "application/octet-stream",
            )
        )
    return uploads


SessionIdPath = Annotated[str, PathParam(pattern=SESSION_ID_PATTERN)]


async def _require_visible(request: Request, session_id: str, *, allow_new: bool = False) -> None:
    """会话属于别的 owner、或(allow_new=False 时)根本不存在,都按不存在处理(404)。

    只有 send-message 允许 allow_new:新会话由第一次发送创建。
    """
    if not is_db_ready():
        return
    if not await repository.conversation_visible(
        session_id, get_owner(request), allow_missing=allow_new
    ):
        raise HTTPException(status_code=404, detail="Conversation not found")


@stream_router.post("/send-message", dependencies=[Depends(rate_limit_send)])
async def send_message_endpoint(
    request: Request,
    session_id: str = Form(..., pattern=SESSION_ID_PATTERN),
    user_query: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    rewind_to_message_id: int | None = Form(None, ge=1),
    client_request_id: str | None = Form(None, max_length=64),
):
    """Accept a message request, upload files, return prompt_id.

    rewind_to_message_id: 先回退到该用户消息之前再发送,用于编辑重发与重新生成。
    """
    await _require_visible(request, session_id, allow_new=True)
    uploads = await _read_uploads(files)

    try:
        prompt_id = await start_chat(
            session_id,
            user_query,
            uploads,
            rewind_to_message_id=rewind_to_message_id,
            owner=get_owner(request),
            client_request_id=client_request_id,
        )
    except QuotaExceededError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except repository.NotOwnerError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc
    except (ValueError, RewindError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TooManyRunsError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ActiveRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to start chat for session '%s'", session_id)
        raise HTTPException(
            status_code=500, detail=f"Failed to start generation (trace id: {get_trace_id()})"
        ) from exc

    return JSONResponse(content={"prompt_id": prompt_id})


@stream_router.post("/stream-events")
async def stream_events_endpoint(http_request: Request, request: StreamEventsRequest):
    """Stream agent events from Redis for a given prompt_id."""
    await _require_visible(http_request, request.session_id)
    events = stream_chat_events(
        request.session_id,
        request.prompt_id,
        request.start_id,
        request.block_ms,
    )

    try:
        first_line = await events.__anext__()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StopAsyncIteration:

        async def _empty():
            if False:
                yield ""

        return StreamingResponse(_empty(), media_type="text/event-stream")

    async def generate():
        yield first_line
        async for line in events:
            yield line

    return StreamingResponse(generate(), media_type="text/event-stream")


@stream_router.get("/thread-status/{session_id}")
async def thread_status(request: Request, session_id: SessionIdPath):
    await _require_visible(request, session_id)
    status = await get_thread_status(session_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    prompt_id = get_active_prompt_id(session_id) if status == "running" else None
    return JSONResponse(
        content={"session_id": session_id, "status": status, "prompt_id": prompt_id}
    )


@stream_router.post("/thread-cancel/{session_id}")
async def cancel_thread(request: Request, session_id: SessionIdPath):
    await _require_visible(request, session_id)
    try:
        status, cancelled = await cancel_chat(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not cancelled:
        return JSONResponse(
            content={
                "session_id": session_id,
                "status": status,
                "cancelled": False,
                "message": "Generation is complete and cannot be stopped.",
            }
        )

    return JSONResponse(content={"session_id": session_id, "status": status, "cancelled": True})


@stream_router.get("/files/download")
async def download_file(
    request: Request,
    session_id: Annotated[str, Query(pattern=SESSION_ID_PATTERN)],
    relative_path: str,
    fingerprint: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None,
):
    await _require_visible(request, session_id)

    paths = SessionPaths.create(session_id)
    try:
        file_path = paths.resolve_host_path(relative_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 白名单要对解析后的路径判断,否则 output/../scratchpad/x 能绕过前缀检查
    session_root = paths.session_root.resolve()
    if not any(file_path.is_relative_to(session_root / d) for d in ("uploads", "output")):
        raise HTTPException(status_code=400, detail="Invalid path")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    if fingerprint is not None:
        # 校验和响应使用同一份字节,避免校验后文件被改写造成引用错位。
        try:
            data = await asyncio.to_thread(read_source, paths, relative_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=404, detail="File not found") from exc
        if hashlib.sha256(data).hexdigest() != fingerprint:
            raise HTTPException(
                status_code=409, detail="原文件已变化,无法定位这条旧引用。请重新检索文件。"
            )
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
            },
        )

    # html/svg 会被浏览器当页面执行,下载接口只该当附件:禁止嗅探并套上 sandbox CSP
    return FileResponse(
        path=file_path,
        filename=Path(relative_path).name,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


@stream_router.get("/files/storage")
async def session_storage(
    request: Request,
    session_id: Annotated[str, Query(pattern=SESSION_ID_PATTERN)],
):
    """分类别的磁盘占用(字节)。quota 只统计 uploads/output/scratchpad,其余仅供查看。"""
    await _require_visible(request, session_id)
    sizes = await asyncio.to_thread(storage_breakdown, SessionPaths.create(session_id))
    quota_used = sizes["uploads"] + sizes["output"] + sizes["scratchpad"]
    return {
        "sizes": sizes,
        "quota_used": quota_used,
        "quota_limit": settings.SESSION_MAX_STORAGE_MB * 1024 * 1024,
    }


@stream_router.get("/files/list")
async def list_files(
    request: Request,
    session_id: Annotated[str, Query(pattern=SESSION_ID_PATTERN)],
):
    """uploads/ 与 output/ 里现在还在的文件,前端据此隐藏已被删除的历史文件条目。"""
    await _require_visible(request, session_id)
    return await asyncio.to_thread(list_session_files, SessionPaths.create(session_id))


@stream_router.delete("/files")
async def delete_file(
    request: Request,
    session_id: Annotated[str, Query(pattern=SESSION_ID_PATTERN)],
    relative_path: str,
):
    """删除 uploads/ 或 output/ 下的单个文件。生成中不允许,避免 agent 正在使用的文件被抽走。"""
    await _require_visible(request, session_id)
    if get_active_prompt_id(session_id) is not None:
        raise HTTPException(status_code=409, detail="会话正在生成,请等待结束后再删除文件")
    try:
        freed = await asyncio.to_thread(
            delete_session_file, SessionPaths.create(session_id), relative_path
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    return {"freed": freed}


@stream_router.delete("/files/versions")
async def clear_versions(
    request: Request,
    session_id: Annotated[str, Query(pattern=SESSION_ID_PATTERN)],
):
    """清空所有文件的历史版本(保留各文件最近一次交付的副本)。生成中不允许。"""
    await _require_visible(request, session_id)
    if get_active_prompt_id(session_id) is not None:
        raise HTTPException(status_code=409, detail="会话正在生成,请等待结束后再清理")
    freed = await asyncio.to_thread(clear_history_versions, SessionPaths.create(session_id))
    return {"freed": freed}


@stream_router.delete("/files/scratchpad")
async def clear_scratch(
    request: Request,
    session_id: Annotated[str, Query(pattern=SESSION_ID_PATTERN)],
):
    """清空暂存目录(agent 的中间文件)。生成中不允许,避免抽走正在使用的文件。"""
    await _require_visible(request, session_id)
    if get_active_prompt_id(session_id) is not None:
        raise HTTPException(status_code=409, detail="会话正在生成,请等待结束后再清理")
    freed = await asyncio.to_thread(clear_scratchpad, SessionPaths.create(session_id))
    return {"freed": freed}


@stream_router.get("/files/export")
async def export_session(
    request: Request,
    session_id: Annotated[str, Query(pattern=SESSION_ID_PATTERN)],
):
    """会话备份包(zip):消息、附件元数据,以及 uploads/ output/ 下的文件和 sha256 清单。"""
    await _require_visible(request, session_id)
    if get_active_prompt_id(session_id) is not None:
        raise HTTPException(status_code=409, detail="会话正在生成,请等待结束后再导出")
    owner = get_owner(request)
    conv = await repository.get_conversation(session_id, owner)
    messages = await repository.get_messages(session_id, owner=owner)
    if conv is None or messages is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    attachments = await repository.get_files_for_messages([m.id for m in messages])
    document = session_export.conversation_document(conv, messages, attachments)
    for m in document["messages"]:
        m["parts"] = repository.normalize_delivered_parts(m["parts"])

    checkpoint, checkpoint_note = None, None
    try:
        checkpoint = await export_thread(get_checkpointer(), session_id)
        if checkpoint is None:
            checkpoint_note = "当前服务没有持久化 checkpoint"
    except CheckpointRejected as exc:
        checkpoint_note = str(exc)
    dest = session_export.new_archive_path()
    try:
        await asyncio.to_thread(
            session_export.build_session_archive,
            SessionPaths.create(session_id),
            document,
            dest,
            checkpoint,
            checkpoint_note,
        )
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return FileResponse(
        path=dest,
        filename=f"deskwork-{session_id}.zip",
        media_type="application/zip",
        background=BackgroundTask(dest.unlink, missing_ok=True),
        headers={"X-Content-Type-Options": "nosniff"},
    )


@stream_router.get("/files/preview")
async def preview_file(
    request: Request,
    session_id: Annotated[str, Query(pattern=SESSION_ID_PATTERN)],
    relative_path: str,
):
    """把 docx/xlsx/pptx 等转成 PDF 供前端预览(转换在沙箱里进行,结果有缓存)。"""
    await _require_visible(request, session_id)
    try:
        pdf = await office_to_pdf(session_id, relative_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PreviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"},
    )


@stream_router.get("/files/versions")
async def file_versions(
    request: Request,
    session_id: Annotated[str, Query(pattern=SESSION_ID_PATTERN)],
    file_name: Annotated[str, Query(min_length=1, max_length=255)],
):
    """output/ 里同名文件被覆盖前留下的历史版本(新的在前)。"""
    await _require_visible(request, session_id)
    try:
        return await asyncio.to_thread(list_versions, SessionPaths.create(session_id), file_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@stream_router.get("/files/delivered")
async def download_delivered_file(
    request: Request,
    session_id: Annotated[str, Query(pattern=SESSION_ID_PATTERN)],
    file_name: Annotated[str, Query(min_length=1, max_length=255)],
    sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    size: Annotated[int | None, Query(ge=0)] = None,
):
    """按交付时记下的摘要取回那次交付的内容(旧消息里的文件卡片用);版本已被裁剪时 410。"""
    await _require_visible(request, session_id)
    try:
        data = await asyncio.to_thread(
            find_delivered, SessionPaths.create(session_id), file_name, sha256, size
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if data is None:
        raise HTTPException(status_code=410, detail="该版本已不在保留范围内,请下载最新版本。")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


@stream_router.get("/files/version")
async def download_file_version(
    request: Request,
    session_id: Annotated[str, Query(pattern=SESSION_ID_PATTERN)],
    file_name: Annotated[str, Query(min_length=1, max_length=255)],
    version: Annotated[str, Query(pattern=r"^\d{1,25}$")],
):
    await _require_visible(request, session_id)
    try:
        data = await asyncio.to_thread(
            read_version, SessionPaths.create(session_id), file_name, version
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Version not found") from exc
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{version}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )
