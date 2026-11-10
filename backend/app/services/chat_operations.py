import asyncio
import contextlib
import hashlib
import json
import uuid
from collections.abc import AsyncGenerator

from anthropic import APITimeoutError as AnthropicTimeout
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from openai import APITimeoutError as OpenAITimeout

from app.agents import get_agent
from app.agents.context import RunContext
from app.agents.recovery import (
    ResumeConflictError,
    _repair_cancelled_tool_calls,
    _resume_command,
    fork_answered_interrupt,
)
from app.config import settings
from app.db import is_db_ready, repository
from app.db import runs as run_store
from app.db.models import Run
from app.owner import current_owner
from app.runtime.events import RunEvents
from app.runtime.model_usage import RunUsageCallback
from app.runtime.sessions import ActiveRunError as ActiveRunError
from app.runtime.sessions import TooManyRunsError as TooManyRunsError
from app.runtime.sessions import (
    complete_before_cancelling,
    file_io,
    sessions,
)
from app.sandbox import CONTAINER_MANAGER
from app.sandbox.constants import AI_TOKEN_BATCH_SIZE
from app.services import rewind as rewind_service
from app.services.budget import BudgetExceeded, BudgetGuard
from app.services.file_operations import InMemoryUpload, upload_session_files
from app.services.file_preview import is_office, prewarm
from app.services.history import (
    CHECKPOINT_EVENTS,
    persist_assistant_turn,
    persist_user_turn,
)
from app.services.rewind import RewindError as RewindError
from app.services.usage import UsageTracker
from app.utils import (
    add_token,
    get_thread_status,
    get_trace_id,
    logger,
    mark_done,
    prompt_belongs_to_session,
    read_stream,
    refresh_running_ttl,
    register_prompt,
    session_id_var,
    set_thread_status,
)
from app.utils.redis_utils import append_existing, finish_existing_stream, stream_exists

_background_tasks: set[asyncio.Task] = set()

USER_CANCELLED_TOOL_MESSAGE = "Execution cancelled by user."
# 上一轮因出错或预算上限中断时留下的未完成调用;不能说成用户取消,模型会误以为用户不要这一步
INTERRUPTED_TOOL_MESSAGE = "Execution was interrupted before this tool call finished."
# 取消请求落在启动步骤(上传、快照、写库)期间时,最多等这么久让任务建起来再取消
_START_WAIT_SECONDS = 30.0

# create_agent 里调用主模型(含 wrap_model_call 中间件,如模型切换)的图节点名
_MODEL_NODE = "model"


def _request_fingerprint(
    user_query: str,
    uploads: list[InMemoryUpload],
    rewind_to_message_id: int | None,
    options: dict | None = None,
) -> str:
    h = hashlib.sha256()
    h.update(json.dumps([user_query, rewind_to_message_id, options], sort_keys=True).encode())
    for u in uploads:
        h.update(hashlib.sha256(u.content).digest())
        h.update(u.filename.encode())
    return h.hexdigest()


def build_agent_query(user_query: str, uploaded_files: list[dict]) -> str:
    main_query = ""
    if uploaded_files:
        main_query += "<uploaded_files>\n"
        for file in uploaded_files:
            main_query += "  <file>\n"
            main_query += f"    <name>{file['file_name']}</name>\n"
            main_query += f"    <path>{file['container_path']}</path>\n"
            main_query += "  </file>\n"
        main_query += "</uploaded_files>\n"
    main_query += f"<user_query>\n{user_query}\n</user_query>\n"
    return main_query.strip()


async def _push(prompt_id: str, event: str, data: dict) -> None:
    data = dict(data)
    envelope = {k: data.pop(k) for k in ("run_id", "seq", "schema_version") if k in data}
    payload = json.dumps({"event": event, "data": data, **envelope})
    logger.debug("stream event queued: event=%s payload_chars=%d", event, len(payload))
    handle = next((h for h in sessions.active() if h.prompt_id == prompt_id), None)
    if handle and handle.stream_started:
        await append_existing(prompt_id, payload)
    else:
        await add_token(prompt_id, payload)
        if handle:
            handle.stream_started = True


def _normalize_custom(payload) -> tuple[str, dict] | None:
    if not isinstance(payload, dict):
        return None

    kind = payload.get("kind")
    if kind == "tool.result":
        return "tool.result", {
            "id": payload.get("tool_call_id") or payload.get("id"),
            "name": payload.get("name"),
            "content": payload.get("content"),
            "status": payload.get("status", "success"),
        }
    if kind == "files.presented":
        # host_path 是服务器上的绝对路径,不发给客户端也不落库
        files = [
            {k: v for k, v in f.items() if k != "host_path"} if isinstance(f, dict) else f
            for f in payload.get("files", [])
        ]
        return "files.presented", {"files": files}
    if kind == "widget.presented":
        return "widget.presented", {"widgets": payload.get("widgets", [])}
    if kind == "sources.presented":
        return "sources.presented", {"sources": payload.get("sources", [])}

    return None


def _ai_tool_call_event(message: AIMessage) -> dict:
    return {
        "text": message.text or None,
        "tool_calls": [
            {
                "id": tool_call["id"],
                "name": tool_call["name"],
                "args": tool_call["args"],
            }
            for tool_call in message.tool_calls
        ],
    }


class _TokenBatcher:
    def __init__(self, batch_size: int) -> None:
        self._batch_size = batch_size
        self._buffer: list[str] = []

    def add(self, text: str) -> str | None:
        self._buffer.append(text)
        if len(self._buffer) >= self._batch_size:
            return self.flush()
        return None

    def flush(self) -> str | None:
        if not self._buffer:
            return None
        combined = "".join(self._buffer)
        self._buffer.clear()
        return combined


async def start_chat(
    session_id: str,
    user_query: str,
    uploads: list[InMemoryUpload],
    *,
    rewind_to_message_id: int | None = None,
    owner: str = "",
    client_request_id: str | None = None,
    rewind_mode: str = "full",
    interrupt_id: str | None = None,
    mode: str = "auto",
) -> str:
    """rewind_to_message_id 不为空时,先把对话回退到该用户消息之前再重新发送,
    并沿用原消息的附件(编辑重发/重新生成)。

    client_request_id:客户端为一次发送生成的 ID。响应丢失后带同一 ID 重试,返回原 prompt_id,
    不会再建一轮;同 ID 但内容不同则拒绝。"""
    reused_files: list[dict] = []
    if not user_query.strip() and not uploads and rewind_to_message_id is None:
        raise ValueError("Either user_query or uploaded_files must be provided")

    fingerprint = None
    if client_request_id:
        # 上传可达 100MB,哈希放到线程里,免得卡住其他会话的事件流
        fingerprint = await asyncio.to_thread(
            _request_fingerprint,
            user_query,
            uploads,
            rewind_to_message_id,
            {"rewind_mode": rewind_mode, "interrupt_id": interrupt_id, "mode": mode},
        )
        existing = (
            await run_store.accepted(session_id, owner, client_request_id, fingerprint)
            if is_db_ready()
            else None
        )
        if existing is not None:
            return existing

    handle = sessions.reserve(session_id, limit=settings.MAX_CONCURRENT_RUNS)
    start_finished = handle.ready
    marked_running = False
    accepted_run = None
    try:
        from app.services.conversation_cleanup import PENDING_PURGE_DIR

        if (PENDING_PURGE_DIR / session_id).exists():
            raise ActiveRunError("会话正在等待清理,请稍后重试")
        if is_db_ready() and not await repository.conversation_visible(session_id, owner):
            raise repository.NotOwnerError(session_id)
        if is_db_ready():
            # Claim file ownership before any upload can leave durable bytes behind.
            await repository.ensure_conversation(session_id, user_query, owner)
        if rewind_to_message_id is not None:
            target_message = await _check_rewind_target(session_id, rewind_to_message_id)
            if is_db_ready() and rewind_mode == "full":
                if not await rewind_service.files_available(session_id, target_message.prompt_id):
                    raise rewind_service.FilesUnavailable(
                        "文件快照不可用,可选择仅回退对话并保留当前文件"
                    )
        # 先上传再回退:上传失败时不能已经删掉历史和 checkpoint
        new_files = await upload_session_files(session_id, uploads)
        if rewind_to_message_id is not None:
            # 只读取原附件;真正的回退放到所有可能失败的启动步骤之后
            reused_files = (
                await rewind_service.reused_files(session_id, rewind_to_message_id)
                if is_db_ready()
                else await repository.get_message_files(rewind_to_message_id)
            )
        uploaded_records = [
            {k: v for k, v in f.items() if k != "host_path"} for f in reused_files + new_files
        ]
        query = build_agent_query(user_query, uploaded_records)

        prompt_id = str(uuid.uuid4())
        if not is_db_ready():
            await register_prompt(session_id, prompt_id)
        # 从置 running 起就登记 prompt_id:快照、写库期间刷新页面也能接回,文件操作也会被拦下
        handle.prompt_id = prompt_id
        if not is_db_ready():
            await set_thread_status(session_id, "running")
        marked_running = True

        await _push(prompt_id, "upload.complete", {"files": uploaded_records})
        await _push(prompt_id, "user.message", {"text": user_query, "files": uploaded_records})
        if is_db_ready():
            accepted_run = await run_store.accept(
                prompt_id,
                session_id,
                owner,
                {
                    "text": user_query,
                    "files": uploaded_records,
                    "rewind_to_message_id": rewind_to_message_id,
                    "rewind_mode": rewind_mode,
                    "interrupt_id": interrupt_id,
                    "mode": mode,
                },
                client_request_id,
                fingerprint or "",
                get_trace_id(),
            )
        else:
            # Direct unit-test invocations have explicit fake persistence/transport.
            if rewind_to_message_id is not None:
                await _rewind(session_id, rewind_to_message_id)
            await persist_user_turn(session_id, prompt_id, user_query, uploaded_records, owner)

        _launch_agent_task(session_id, prompt_id, query, user_query, owner, accepted_run)
        return prompt_id
    except BaseException as exc:
        if marked_running and handle.task is None and is_db_ready():
            # Cancellation can arrive after COMMIT but before accept() returns.
            # Reconcile the receipt instead of leaving an accepted run orphaned.
            receipt = accepted_run or await run_store.get(prompt_id)
            if receipt is not None:
                await run_store.save(
                    prompt_id,
                    [],
                    "",
                    0,
                    {},
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                    "admission_interrupted",
                    "任务启动已中断",
                )
        if marked_running:  # Close a partially initialized transport on admission failure.
            if not is_db_ready():
                with contextlib.suppress(Exception):
                    await set_thread_status(session_id, "error")
            # 已推过事件的流要收尾并设过期,否则这个 Redis 键永远留着
            with contextlib.suppress(Exception):
                if is_db_ready():
                    await finish_existing_stream(prompt_id)
                else:
                    await mark_done(prompt_id)
        raise
    finally:
        start_finished.set()
        if handle.task is None:
            sessions.release(handle)


def _launch_agent_task(
    session_id: str,
    prompt_id: str,
    agent_input: str,
    raw_answer: str = "",
    owner: str = "",
    run_record: Run | None = None,
) -> asyncio.Task:
    handle = sessions.get(session_id)
    if handle is None:
        raise RuntimeError("Run launched without owning its session")
    session_token = session_id_var.set(session_id)
    try:
        task = asyncio.create_task(
            _run_agent(prompt_id, session_id, agent_input, raw_answer, owner, run_record)
        )
    finally:
        session_id_var.reset(session_token)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    handle.task = task
    handle.prompt_id = prompt_id

    def _remove_active_task(completed_task: asyncio.Task) -> None:
        if not completed_task.cancelled() and completed_task.exception() is not None:
            logger.error("Background run failed", exc_info=completed_task.exception())
            handle.phase = "recovery_required"
        if completed_task.cancelled() and not handle.started:
            return  # cancel_chat owns the not-yet-entered run's durable finalization.
        if handle.operation == "run" and handle.phase != "recovery_required":
            sessions.release(handle)

    task.add_done_callback(_remove_active_task)
    return task


def get_active_prompt_id(session_id: str) -> str | None:
    """本进程中该会话正在运行的 prompt_id,供刷新页面后的客户端接回事件流。"""
    handle = sessions.get(session_id)
    return handle.prompt_id if handle and handle.operation == "run" else None


async def cancel_chat(session_id: str) -> tuple[str, bool]:
    handle = sessions.get(session_id)
    if handle is None:
        status, _ = await get_run_status(session_id)
        if status is None:
            raise LookupError(f"No generation was found for session '{session_id}'")
        return status, False
    handle.cancel_requested = True
    if handle.task is None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(handle.ready.wait(), timeout=_START_WAIT_SECONDS)
    task = handle.task
    if task is None:
        return (
            "cancelling",
            True,
        )  # Preparation observes cancel_requested before entering the graph.
    if not task.done() and not task.cancelling():
        task.cancel()
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            raise
    if not handle.started:
        if is_db_ready() and await run_store.get(handle.prompt_id):
            await run_store.save(handle.prompt_id, [], "", 0, {}, "cancelled")
        if handle.prompt_id:
            with contextlib.suppress(Exception):
                await _push(handle.prompt_id, "stream.end", {"status": "cancelled"})
                if is_db_ready():
                    await finish_existing_stream(handle.prompt_id)
                else:
                    await mark_done(handle.prompt_id)
        with contextlib.suppress(Exception):
            await set_thread_status(session_id, "cancelled")
        if handle.operation == "run":
            sessions.release(handle)
    status, _ = await get_run_status(session_id)
    return status or "cancelled", True


async def get_run_status(session_id: str) -> tuple[str | None, str | None]:
    handle = sessions.get(session_id)
    if handle and handle.operation == "run":
        status = "error" if handle.phase == "recovery_required" else "running"
        return status, handle.prompt_id
    if is_db_ready():
        run = await run_store.latest(session_id)
        if run:
            return (
                "running" if run.status in run_store.ACTIVE_STATUSES else run.status,
                run.id if run.status in run_store.ACTIVE_STATUSES else None,
            )
        conv = await repository.get_conversation(session_id)
        return (conv.last_run_status if conv else None), None
    return await get_thread_status(session_id), None


async def _check_rewind_target(session_id: str, message_id: int):
    msg = await repository.get_message(session_id, message_id)
    if msg is None or msg.role != "user":
        raise RewindError(f"Message {message_id} is not a user message of this conversation")
    if msg.checkpoint_id is None:
        raise RewindError("This message predates checkpoint tracking and cannot be rewound")
    return msg


async def _rewind(session_id: str, message_id: int) -> list[dict]:
    """Explicit conversation-only compatibility entry; production uses RewindOperation."""
    message = await _check_rewind_target(session_id, message_id)
    graph = (await run_store.graph_config(session_id))["configurable"]
    graph_ref = (
        {"thread_id": str(uuid.uuid4())}
        if message.checkpoint_id == ""
        else {"thread_id": graph["thread_id"], "checkpoint_id": message.checkpoint_id}
    )
    files = await repository.get_message_files(message_id)
    await run_store.rewind_history(session_id, message_id, graph_ref)
    return files


async def _record_checkpoint(agent, config: dict, session_id: str, prompt_id: str) -> None:
    """记下本轮用户消息开始前的 checkpoint,之后才能回退到这里。"""
    if not is_db_ready():
        return
    try:
        snapshot = await agent.aget_state(config)
        checkpoint_id = snapshot.config["configurable"].get("checkpoint_id", "")
        await repository.set_user_checkpoint(session_id, prompt_id, checkpoint_id)
    except Exception:
        logger.exception("Failed to record checkpoint for session '%s'", session_id)


async def _record_outcome(session_id: str, status: str) -> None:
    """记下这一轮的终态供会话列表展示;失败只记日志,不影响流。"""
    if not is_db_ready():
        return
    try:
        await repository.set_run_outcome(session_id, status)
    except Exception:
        logger.exception("Failed to record run outcome for session '%s'", session_id)


async def _run_agent(
    prompt_id: str,
    session_id: str,
    agent_input: str,
    raw_answer: str = "",
    owner: str = "",
    run_record: Run | None = None,
) -> None:
    current_owner.set(owner)
    handle = sessions.get(session_id)
    if handle and handle.prompt_id == prompt_id:
        handle.started = True
    events = RunEvents(prompt_id, _push)
    recorder = events.recorder
    usage = UsageTracker()
    guard = BudgetGuard(
        settings.AGENT_MAX_LLM_CALLS,
        settings.AGENT_MAX_TOTAL_TOKENS,
        settings.AGENT_MAX_REPEATED_CALLS,
    )
    config = {"configurable": {"thread_id": session_id}, "metadata": {"trace_id": get_trace_id()}}
    stream = None
    agent = None
    outcome = "complete"
    error_code = public_error = None
    asked = False
    office: list[str] = []
    durable = run_record is not None
    graph_advanced = False
    run = run_record
    rewind_operation = None
    token_batcher = _TokenBatcher(AI_TOKEN_BATCH_SIZE)
    last_saved_seq = -1
    ticker = None
    ticker_error = None
    streamed_this_model = False
    owner_task = asyncio.current_task()
    save_lock = asyncio.Lock()

    async def save(status: str) -> bool:
        async with save_lock:
            return await save_snapshot(status)

    async def save_snapshot(status: str) -> bool:
        nonlocal last_saved_seq
        if durable:
            parts, text, seq = events.snapshot()
            for attempt in range(3 if status != "running" else 1):
                try:
                    await run_store.save(
                        prompt_id,
                        parts,
                        text,
                        seq,
                        usage.to_event(),
                        status,
                        error_code,
                        public_error,
                        graph_advanced,
                    )
                    last_saved_seq = seq
                    return True
                except Exception:
                    logger.exception("Saving run %s failed", prompt_id)
                    if status != "running" and attempt < 2:
                        await asyncio.sleep(0.5)
            return False
        ok = await persist_assistant_turn(
            session_id,
            prompt_id,
            recorder,
            "complete" if status == "waiting" else status,
            usage.to_event() if usage.llm_calls else None,
        )
        if ok:
            last_saved_seq = events.seq
        return ok

    async def emit(event: str, data: dict) -> None:
        await events.emit(event, data)
        if event == "files.presented":
            office.extend(
                f["container_path"]
                for f in data.get("files", [])
                if f.get("container_path") and is_office(f["container_path"])
            )
        if event in CHECKPOINT_EVENTS:
            await save("running")

    async def flush() -> None:
        text = token_batcher.flush()
        if text:
            await emit("ai.token", {"text": text})

    async def tick() -> None:
        nonlocal ticker_error
        try:
            ticks = 0
            while True:
                await asyncio.sleep(2)
                if events.seq != last_saved_seq and recorder.parts:
                    await save("running")
                ticks += 1
                if ticks % 15 == 0:
                    if not durable:
                        await refresh_running_ttl(session_id)
                    await emit("heartbeat", {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ticker_error = exc
            if owner_task:
                owner_task.cancel()

    deadline = asyncio.timeout(settings.AGENT_RUN_TIMEOUT_SECONDS or None)
    try:
        async with deadline:
            if run is None:
                run = await run_store.get(prompt_id) if is_db_ready() else None
                durable = run is not None
            if handle and handle.cancel_requested:
                raise asyncio.CancelledError
            agent = get_agent()
            if durable:
                config = await run_store.graph_config(session_id)
                config["metadata"] = {"trace_id": get_trace_id(), "run_id": prompt_id}
                config["callbacks"] = [RunUsageCallback(usage, guard)]
                target = run.input_json.get("rewind_to_message_id")
                if target is not None:
                    await drain_session_work(session_id)
                    rewind_operation = await rewind_service.prepare(
                        session_id,
                        prompt_id,
                        target,
                        run.input_json.get("rewind_mode") == "conversation_only",
                    )
                    config["configurable"] = rewind_operation.graph_ref
                state = await agent.aget_state(config)
                expected = config["configurable"].get("checkpoint_id")
                if (
                    expected
                    and (state.config or {}).get("configurable", {}).get("checkpoint_id")
                    != expected
                ):
                    raise RewindError("Checkpoint is no longer available")
                if rewind_operation and any(t.interrupts for t in state.tasks):
                    fork_thread = str(uuid.uuid4())
                    await file_io(rewind_operation.set_fork_thread, fork_thread)
                    config["configurable"] = await fork_answered_interrupt(
                        agent, config, state, fork_thread
                    )
                    rewind_operation.graph_ref = config["configurable"]
                    state = await agent.aget_state(config)
                resume = await _resume_command(
                    agent,
                    config,
                    raw_answer or agent_input,
                    interrupt_id=run.input_json.get("interrupt_id"),
                    mode=run.input_json.get("mode", "auto"),
                    snapshot=state,
                )
                source = (
                    await run_store.graph_run(
                        session_id,
                        (state.metadata or {}).get("run_id"),
                        state.config["configurable"].get("checkpoint_id"),
                    )
                    if resume is not None
                    else None
                )
                if rewind_operation:
                    session = await CONTAINER_MANAGER.get_or_create(session_id)
                    await session.close()
                    async with session.workspace_lock:
                        await file_io(rewind_operation.apply)
                checkpoint = state.config["configurable"].get("checkpoint_id", "")
                await run_store.start(
                    prompt_id,
                    run.input_json["text"],
                    run.input_json["files"],
                    checkpoint,
                    target,
                    rewind_operation.graph_ref if rewind_operation else None,
                    resume_from=source,
                )
                if source is not None:
                    run.turn_id = source.turn_id
                guard.initial_calls, guard.initial_tokens = await run_store.turn_usage(
                    run.turn_id, prompt_id
                )
                if rewind_operation:
                    await rewind_service.recover_operation(
                        rewind_operation, True, agent.checkpointer
                    )
                    rewind_operation = None
                # Only committed inputs may prune retained snapshots. A cancelled RPC
                # drains before releasing the workspace lock, so no writer outlives it.
                ready = False
                try:
                    ready = bool(await CONTAINER_MANAGER.snapshot_session(session_id, prompt_id))
                except Exception:
                    logger.exception("Snapshot unavailable for run %s", prompt_id)
                await run_store.snapshot_finished(prompt_id, ready)
                await _repair_cancelled_tool_calls(agent, config)
            else:
                await _repair_cancelled_tool_calls(agent, config)
                await _record_checkpoint(agent, config, session_id, prompt_id)
                resume = await _resume_command(agent, config, raw_answer or agent_input)
            if handle:
                handle.phase = "running"
            input_state = (
                resume if resume is not None else {"messages": [HumanMessage(content=agent_input)]}
            )
            ticker = asyncio.create_task(tick())
            context_args = (
                {"context": RunContext(session_id, owner, prompt_id, run.turn_id, get_trace_id())}
                if durable
                else {}
            )
            stream = agent.astream(
                input_state,
                config=config,
                stream_mode=["messages", "updates", "custom"],
                **context_args,
            )
            async for chunk_type, chunk_data in stream:
                if chunk_type == "messages":
                    token, metadata = chunk_data
                    if (
                        isinstance(token, AIMessageChunk)
                        and metadata.get("langgraph_node") == _MODEL_NODE
                        and not token.tool_call_chunks
                        and token.text
                    ):
                        streamed_this_model = True
                        batched = token_batcher.add(token.text)
                        if batched is not None:
                            await emit("ai.token", {"text": batched})
                elif chunk_type == "updates":
                    await flush()
                    for source, update in chunk_data.items():
                        if source == "__interrupt__":
                            if not asked and update:
                                item = update[0]
                                payload = dict(item.value) if isinstance(item.value, dict) else {}
                                payload["interrupt_id"] = item.id
                                asked = True
                                await emit("ask.user", payload)
                            continue
                        if not isinstance(update, dict):
                            continue
                        messages = update.get("messages") or []
                        if not messages or not isinstance(messages[-1], AIMessage):
                            continue
                        message = messages[-1]
                        usage.add(message)
                        if message.tool_calls:
                            await emit("ai.tool_call", _ai_tool_call_event(message))
                        elif message.text and not streamed_this_model:
                            await emit("ai.token", {"text": message.text})
                        streamed_this_model = False
                        guard.check(usage, message.tool_calls)
                elif chunk_type == "custom":
                    await flush()
                    normalized = _normalize_custom(chunk_data)
                    if normalized:
                        await emit(*normalized)
            await flush()
            outcome = "waiting" if asked else "complete"
    except TimeoutError:
        outcome = "error"
        error_code = "run_timeout" if deadline.expired() else "execution_timeout"
        public_error = "任务执行超时,已停止本次运行。可以拆分任务或稍后重试。"
    except (AnthropicTimeout, OpenAITimeout):
        outcome, error_code = "error", "model_timeout"
        public_error = "模型服务响应超时,请稍后重试或切换模型。"
    except asyncio.CancelledError:
        outcome = "error" if ticker_error else "cancelled"
        if ticker_error:
            error_code, public_error = "stream_unavailable", "事件流暂时不可用,本次生成已停止"
    except Exception as exc:
        outcome = "error"
        error_code = "stream_unavailable" if events.transport_failed else "generation_failed"
        public_error = (
            str(exc)
            if isinstance(exc, (BudgetExceeded, ResumeConflictError, rewind_service.RewindError))
            else f"生成过程中出现内部错误,请稍后重试(trace id: {get_trace_id()})"
        )
        logger.exception("Run %s failed", prompt_id)
    finally:

        async def finalize() -> None:
            nonlocal outcome, error_code, public_error, graph_advanced
            if handle:
                handle.phase = "finalizing"
            if ticker:
                ticker.cancel()
                await asyncio.gather(ticker, return_exceptions=True)
            if stream is not None:
                try:
                    await stream.aclose()
                except Exception:
                    outcome, error_code = "error", "cleanup_failed"
                    public_error = "无法确认工具执行已停止,请重启服务后重试"
                    if handle:
                        handle.phase = "recovery_required"
                    logger.exception("Closing graph stream %s failed", prompt_id)
            if rewind_operation:
                try:
                    stored_run = await run_store.get(prompt_id)
                    await rewind_service.recover_operation(
                        rewind_operation,
                        bool(stored_run and stored_run.input_committed),
                        agent.checkpointer if agent else None,
                    )
                except Exception:
                    outcome, error_code = "error", "recovery_required"
                    if handle:
                        handle.phase = "recovery_required"
                    logger.exception("Rewind recovery for %s failed", prompt_id)
            if outcome == "cancelled" and agent is not None:
                try:
                    await _repair_cancelled_tool_calls(agent, config, USER_CANCELLED_TOOL_MESSAGE)
                except Exception:
                    logger.exception("Repairing cancelled graph %s failed", prompt_id)
            # Notification failures cannot bypass the durable terminal transaction.
            if not events.transport_failed:
                with contextlib.suppress(Exception):
                    await emit("usage", usage.to_event())
            if events.transport_failed and outcome in ("complete", "waiting"):
                outcome, error_code = "error", "stream_unavailable"
                public_error = "事件流暂时不可用,本次生成已停止"
            if public_error and not events.transport_failed:
                with contextlib.suppress(Exception):
                    await emit("error", {"code": error_code, "message": public_error})
            if durable and agent is not None:
                try:
                    graph_advanced = await run_store.graph_was_written(session_id, prompt_id, agent)
                except Exception:
                    outcome, error_code = "error", "recovery_required"
                    if handle:
                        handle.phase = "recovery_required"
                    logger.exception("Recording graph head for %s failed", prompt_id)
            saved = await save(outcome)
            if not saved:
                outcome = "error"
                if handle:
                    handle.phase = "recovery_required"
                with contextlib.suppress(Exception):
                    await emit(
                        "error",
                        {
                            "code": "history_save_failed",
                            "message": "回答已生成,但保存到历史记录失败,刷新页面后可能看不到这一轮内容。",
                        },
                    )
            if not durable:
                await _record_outcome(session_id, outcome)
                with contextlib.suppress(Exception):
                    await set_thread_status(session_id, outcome)
            with contextlib.suppress(Exception):
                await emit("stream.end", {"status": outcome})
            with contextlib.suppress(Exception):
                if durable:
                    await finish_existing_stream(prompt_id)
                else:
                    await mark_done(prompt_id)
            if office and outcome == "complete":
                _spawn_prewarm(session_id, office)

        await complete_before_cancelling(asyncio.create_task(finalize()))


def _virtual_to_relative(path: str) -> str:
    """/workspace/output/a.docx -> output/a.docx"""
    return path.removeprefix("/workspace").lstrip("/")


def _spawn_prewarm(session_id: str, container_paths: list[str]) -> None:
    """后台预转换;转换在会话 shell 里执行,所以放到本轮结束之后,不挡住 agent 的命令。"""
    if not settings.PREVIEW_PREWARM:
        return
    # 同名文件多次交付只转最后一次;路径转成相对会话根的形式
    rels = list(dict.fromkeys(_virtual_to_relative(p) for p in container_paths))
    task = asyncio.create_task(prewarm(session_id, rels))
    _background_tasks.add(task)

    def done(completed):
        _background_tasks.discard(completed)
        if not completed.cancelled() and completed.exception() is not None:
            logger.warning(
                "Preview prewarm for %s failed", session_id, exc_info=completed.exception()
            )

    task.add_done_callback(done)


async def drain_session_work(session_id: str | None = None) -> None:
    from app.services.doc_search import drain_indexes
    from app.services.file_preview import drain_previews

    await drain_previews(session_id)
    await drain_indexes(session_id)


async def shutdown_runs() -> None:
    sessions.closing = True
    occupied = sessions.active()
    for handle in occupied:
        if handle.operation == "run" and handle.task is None:
            handle.cancel_requested = True
    await asyncio.gather(
        *(
            h.released.wait() if h.operation != "run" else h.ready.wait()
            for h in occupied
            if h.operation != "run" or h.task is None
        )
    )
    await asyncio.gather(
        *(cancel_chat(h.session_id) for h in sessions.active() if h.task and not h.task.done()),
        return_exceptions=True,
    )
    if _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)
    await drain_session_work()


async def stream_chat_events(
    session_id: str,
    prompt_id: str,
    start_id: str = "0-0",
    block_ms: int = 100,
) -> AsyncGenerator[str, None]:
    if is_db_ready() and (run := await run_store.get(prompt_id)) is not None:
        if run.conversation_id != session_id:
            raise LookupError("Run not found")
        if not await stream_exists(prompt_id):
            raise StreamExpiredError("Event stream expired; reload the saved run")
    elif not await prompt_belongs_to_session(session_id, prompt_id):
        raise LookupError(f"prompt_id '{prompt_id}' does not belong to session '{session_id}'")

    async for entry_id, token in read_stream(prompt_id, block_ms, start_id):
        payload = json.loads(token)
        yield f"data: {json.dumps({'id': entry_id, **payload})}\n\n"


class StreamExpiredError(LookupError):
    pass
