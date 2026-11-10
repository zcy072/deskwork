import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agents import get_agent
from app.agents.checkpointers import close_checkpointer, init_checkpointer
from app.agents.permissions import parse_policy
from app.api.routes import routes
from app.api.security import require_api_key, warn_if_auth_disabled
from app.config import settings
from app.db import close_db, init_db, repository
from app.db.runs import recover_interrupted
from app.owner import owner_of_key
from app.runtime.sessions import sessions
from app.sandbox import CONTAINER_MANAGER
from app.sandbox.constants import SESSION_ROOT
from app.services import session_export
from app.services.conversation_cleanup import retry_pending_purges
from app.services.mcp_client import init_mcp
from app.services.rewind import recover_operations
from app.utils import (
    close_redis,
    connect_redis,
    logger,
    recover_orphaned_runs,
    session_id_var,
    trace_id_var,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sessions.closing = False
    warn_if_auth_disabled()
    # 限制性配置写错不能静默当成"不限制":直接拒绝启动
    parse_policy(settings.TOOL_POLICY)
    await connect_redis()
    try:
        try:
            # 单进程部署:启动时仍是 running 的会话都属于上一个进程,已经没有任务在跑
            if n := await recover_orphaned_runs():
                logger.warning("启动时发现 %d 个上次中断的生成,已标记为 error", n)
        except Exception:
            logger.exception("Recovering orphaned runs failed")
        await init_db()
        try:
            if n := await repository.mark_interrupted_messages():
                logger.warning("启动时发现 %d 条中断的助手消息,已标记为 error", n)
        except Exception:
            logger.exception("Marking interrupted messages failed")
        try:
            if settings.api_keys:
                # 开启认证前留下的数据没有归属:统一划给 API_KEYS 里的第一个 Key,否则所有人都看不到
                if n := await repository.adopt_legacy_rows(owner_of_key(settings.api_keys[0])):
                    logger.warning("已把 %d 条无归属的旧数据划给 API_KEYS 的第一个 Key", n)
        except Exception:
            logger.exception("Adopting legacy rows failed")
        await init_checkpointer()
        await recover_operations(SESSION_ROOT)
        await init_mcp()
        app.state.agent = get_agent()
        await recover_interrupted(app.state.agent)
        try:
            await asyncio.to_thread(session_export.sweep_stale_archives)
        except Exception:
            logger.exception("Sweeping stale export archives failed")
        try:
            await retry_pending_purges()
        except Exception:
            logger.exception("Retrying pending sandbox purges failed")
        yield
    finally:
        from app.services.chat_operations import shutdown_runs

        for close in (
            shutdown_runs,
            CONTAINER_MANAGER.close_all,
            close_checkpointer,
            close_db,
            close_redis,
        ):
            try:
                await close()
            except Exception:
                logger.exception("Application resource cleanup failed: %s", close.__name__)
        get_agent.cache_clear()


app = FastAPI(
    title="Deskwork",
    description=(
        "Claude-style AI workspace for working with files. Upload documents, "
        "execute tools, generate artifacts, run code in a sandbox, and build "
        "complex workflows through an extensible agent architecture."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


_QUIET_PATHS = frozenset({"/health", "/stream-events"})


@app.middleware("http")
async def log_requests_middleware(request: Request, call_next):
    trace_id = f"G-{uuid.uuid4().hex[-8:]}"
    trace_token = trace_id_var.set(trace_id)
    session_token = session_id_var.set("-")
    start_time = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Trace-ID"] = trace_id
        return response
    except Exception:
        status_code = 500
        raise
    finally:
        process_time = time.perf_counter() - start_time
        # 健康检查每 10 秒一次、事件流空闲时约每秒重连一次,正常响应只在 DEBUG 下记录
        quiet = request.url.path in _QUIET_PATHS and status_code < 400
        logger.log(
            logging.DEBUG if quiet else logging.INFO,
            f"Endpoint: {request.url.path} | Type: {request.method} | "
            f"Time Taken: {process_time:.4f}s | Status Code: {status_code}",
        )
        trace_id_var.reset(trace_token)
        session_id_var.reset(session_token)


# FastAPI 先把 multipart 请求体整个读完(落到临时文件)才执行路由依赖,
# 所以这两个上传接口的认证要在读请求体之前做,否则未认证的请求也能让服务器写满 100MB+ 的临时文件。
# 路由上的 require_api_key 照常保留;成功时这里不计数,不会重复消耗限流额度。
_UPLOAD_PATHS = frozenset({"/send-message", "/conversations/import"})


@app.middleware("http")
async def auth_before_upload_middleware(request: Request, call_next):
    if request.method == "POST" and request.url.path in _UPLOAD_PATHS:
        try:
            await require_api_key(request)
        except HTTPException as exc:
            return JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type"],
    expose_headers=["X-Trace-ID", "Content-Disposition"],
)

for route in routes:
    app.include_router(route)
