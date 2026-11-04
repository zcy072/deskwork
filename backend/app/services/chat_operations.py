import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import AsyncGenerator

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command

from app.agents import get_agent
from app.agents.checkpointers import get_checkpointer
from app.config import settings
from app.db import is_db_ready, repository
from app.owner import current_owner
from app.sandbox import CONTAINER_MANAGER
from app.sandbox.constants import AI_TOKEN_BATCH_SIZE
from app.services.budget import BudgetExceeded, BudgetGuard
from app.services.file_operations import InMemoryUpload, upload_session_files
from app.services.file_preview import is_office, prewarm
from app.services.history import (
    CHECKPOINT_EVENTS,
    TurnRecorder,
    persist_assistant_turn,
    persist_user_turn,
)
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

_background_tasks: set[asyncio.Task] = set()
_active_agent_tasks: dict[str, asyncio.Task] = {}
_active_prompt_ids: dict[str, str] = {}
_starting_sessions: set[str] = set()
_start_finished: dict[str, asyncio.Event] = {}
_deleting_sessions: set[str] = set()  # 删除进行中,期间不允许启动新一轮
_started_prompts: set[str] = set()  # 已开始运行的 prompt_id

_STATUS_REFRESH_INTERVAL = 60.0  # 秒;running 状态 TTL 是 3600s,每分钟续一次足够

USER_CANCELLED_TOOL_MESSAGE = "Execution cancelled by user."
# 上一轮因出错或预算上限中断时留下的未完成调用;不能说成用户取消,模型会误以为用户不要这一步
INTERRUPTED_TOOL_MESSAGE = "Execution was interrupted before this tool call finished."
# 取消请求落在启动步骤(上传、快照、写库)期间时,最多等这么久让任务建起来再取消
_START_WAIT_SECONDS = 30.0

# create_agent 里调用主模型(含 wrap_model_call 中间件,如模型切换)的图节点名
_MODEL_NODE = "model"


class TooManyRunsError(RuntimeError):
    """同时运行的会话数已达上限(MAX_CONCURRENT_RUNS)。"""


class ActiveRunError(RuntimeError):
    """Raised when a session already owns a running agent task."""


class RequestIdConflictError(ValueError):
    """同一个 client_request_id 被用来发送了不同的内容。"""


_REQUEST_ID_TTL = 600.0  # 秒;覆盖响应丢失后的客户端重试窗口
_REQUEST_ID_MAX = 1000
# (owner, session_id, request_id) -> (内容指纹, prompt_id, 过期时间)。进程内,与单进程约束一致。
_accepted_requests: dict[tuple[str, str, str], tuple[str, str, float]] = {}


def _request_fingerprint(
    user_query: str, uploads: list[InMemoryUpload], rewind_to_message_id: int | None
) -> str:
    h = hashlib.sha256()
    h.update(json.dumps([user_query, rewind_to_message_id]).encode())
    for u in uploads:
        h.update(hashlib.sha256(u.content).digest())
        h.update(u.filename.encode())
    return h.hexdigest()


def _lookup_accepted(key: tuple[str, str, str], fingerprint: str) -> str | None:
    """已接受过的请求返回原 prompt_id;内容不同则拒绝。"""
    now = time.monotonic()
    for k in [k for k, v in _accepted_requests.items() if v[2] <= now]:
        del _accepted_requests[k]
    hit = _accepted_requests.get(key)
    if hit is None:
        return None
    if hit[0] != fingerprint:
        raise RequestIdConflictError("client_request_id was already used for a different message")
    return hit[1]


def _remember_accepted(key: tuple[str, str, str], fingerprint: str, prompt_id: str) -> None:
    if len(_accepted_requests) >= _REQUEST_ID_MAX:
        _accepted_requests.pop(next(iter(_accepted_requests)))  # 最早插入的
    _accepted_requests[key] = (fingerprint, prompt_id, time.monotonic() + _REQUEST_ID_TTL)


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
    payload = json.dumps({"event": event, "data": data})
    logger.debug("stream event queued: event=%s payload_chars=%d", event, len(payload))
    await add_token(prompt_id, payload)


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
) -> str:
    """rewind_to_message_id 不为空时,先把对话回退到该用户消息之前再重新发送,
    并沿用原消息的附件(编辑重发/重新生成)。

    client_request_id:客户端为一次发送生成的 ID。响应丢失后带同一 ID 重试,返回原 prompt_id,
    不会再建一轮;同 ID 但内容不同则拒绝。"""
    reused_files: list[dict] = []
    if not user_query.strip() and not uploads and rewind_to_message_id is None:
        raise ValueError("Either user_query or uploaded_files must be provided")

    request_key = fingerprint = None
    if client_request_id:
        request_key = (owner, session_id, client_request_id)
        # 上传可达 100MB,哈希放到线程里,免得卡住其他会话的事件流
        fingerprint = await asyncio.to_thread(
            _request_fingerprint, user_query, uploads, rewind_to_message_id
        )
        if (existing := _lookup_accepted(request_key, fingerprint)) is not None:
            return existing

    # 检查与占位之间不能有 await,否则两个并发请求都能通过检查
    busy = set(_active_agent_tasks) | _starting_sessions
    if (
        settings.MAX_CONCURRENT_RUNS
        and session_id not in busy
        and len(busy) >= settings.MAX_CONCURRENT_RUNS
    ):
        raise TooManyRunsError(f"同时运行的会话已达上限({settings.MAX_CONCURRENT_RUNS}),请稍后再试")
    if session_id in _starting_sessions or session_id in _deleting_sessions:
        raise ActiveRunError(f"A generation is already running for session '{session_id}'")
    _starting_sessions.add(session_id)
    start_finished = _start_finished[session_id] = asyncio.Event()
    marked_running = False
    try:
        if await get_thread_status(session_id) == "running":
            raise ActiveRunError(f"A generation is already running for session '{session_id}'")
        if is_db_ready() and not await repository.conversation_visible(session_id, owner):
            raise repository.NotOwnerError(session_id)
        if rewind_to_message_id is not None:
            await _check_rewind_target(session_id, rewind_to_message_id)
        # 先上传再回退:上传失败时不能已经删掉历史和 checkpoint
        new_files = await upload_session_files(session_id, uploads)
        if rewind_to_message_id is not None:
            # 只读取原附件;真正的回退放到所有可能失败的启动步骤之后
            reused_files = await repository.get_message_files(rewind_to_message_id)
        uploaded_records = [
            {k: v for k, v in f.items() if k != "host_path"} for f in reused_files + new_files
        ]
        query = build_agent_query(user_query, uploaded_records)

        prompt_id = str(uuid.uuid4())
        await register_prompt(session_id, prompt_id)
        # 从置 running 起就登记 prompt_id:快照、写库期间刷新页面也能接回,文件操作也会被拦下
        _active_prompt_ids[session_id] = prompt_id
        await set_thread_status(session_id, "running")
        marked_running = True

        await _push(prompt_id, "upload.complete", {"files": uploaded_records})
        await _push(prompt_id, "user.message", {"text": user_query, "files": uploaded_records})
        # 回退是破坏性的,放在此处:此前任何一步失败,历史和 checkpoint 都原样保留
        if rewind_to_message_id is not None:
            await _rewind(session_id, rewind_to_message_id)
        try:
            await CONTAINER_MANAGER.snapshot_session(session_id, prompt_id)
        except Exception:
            logger.exception("Snapshot before turn failed for session '%s'", session_id)
        await persist_user_turn(session_id, prompt_id, user_query, uploaded_records, owner)

        _launch_agent_task(session_id, prompt_id, query, user_query, owner)
        if request_key is not None:
            _remember_accepted(request_key, fingerprint, prompt_id)
        return prompt_id
    except BaseException:
        if session_id not in _active_agent_tasks:
            _active_prompt_ids.pop(session_id, None)
        if marked_running:  # 启动中途失败,不能让状态在 TTL 内一直卡在 running
            await set_thread_status(session_id, "error")
            # 已推过事件的流要收尾并设过期,否则这个 Redis 键永远留着
            with contextlib.suppress(Exception):
                await mark_done(prompt_id)
        raise
    finally:
        _starting_sessions.discard(session_id)
        _start_finished.pop(session_id, None)
        start_finished.set()


def _launch_agent_task(
    session_id: str,
    prompt_id: str,
    agent_input: str,
    raw_answer: str = "",
    owner: str = "",
) -> asyncio.Task:
    session_token = session_id_var.set(session_id)
    try:
        task = asyncio.create_task(
            _run_agent(prompt_id, session_id, agent_input, raw_answer, owner)
        )
    finally:
        session_id_var.reset(session_token)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    _active_agent_tasks[session_id] = task
    _active_prompt_ids[session_id] = prompt_id

    def _remove_active_task(completed_task: asyncio.Task) -> None:
        if _active_agent_tasks.get(session_id) == completed_task:
            _active_agent_tasks.pop(session_id, None)
            _active_prompt_ids.pop(session_id, None)

    task.add_done_callback(_remove_active_task)
    return task


def get_active_prompt_id(session_id: str) -> str | None:
    """本进程中该会话正在运行的 prompt_id,供刷新页面后的客户端接回事件流。"""
    return _active_prompt_ids.get(session_id)


async def cancel_chat(session_id: str) -> tuple[str, bool]:
    status = await get_thread_status(session_id)
    if status is None:
        raise LookupError(f"No generation was found for session '{session_id}'")
    if status != "running":
        return status, False

    task = _active_agent_tasks.get(session_id)
    # 启动步骤还没跑完(快照可能要复制几百 MB):等任务建起来再取消,而不是报 409
    starting = _start_finished.get(session_id)
    if task is None and starting is not None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(starting.wait(), timeout=_START_WAIT_SECONDS)
        task = _active_agent_tasks.get(session_id)
    if task is None:
        if (await get_thread_status(session_id)) != "running":
            return (await get_thread_status(session_id)) or "complete", False
        raise RuntimeError(f"The active generation for session '{session_id}' is unavailable")

    prompt_id = _active_prompt_ids.get(session_id)
    task_had_started = prompt_id in _started_prompts
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    if not task_had_started:
        if prompt_id is not None:
            await _push(prompt_id, "stream.end", {})
            await mark_done(prompt_id)
        await set_thread_status(session_id, "complete")

    return (await get_thread_status(session_id)) or "complete", True


async def _repair_cancelled_tool_calls(
    agent, config: dict, content: str = INTERRUPTED_TOOL_MESSAGE
) -> None:
    """给最后一条 AI 消息里没有结果的工具调用补一条结果,否则下一次模型调用会因缺少 ToolMessage 报错。

    取消时由清理阶段以"用户取消"补上;每轮开始时再兜底一次,处理出错、超预算或进程崩溃留下的调用。
    """
    snapshot = await agent.aget_state(config)
    if getattr(snapshot, "interrupts", None):
        # 工具正在等用户回答(ask_user_input),不是被取消,不能补"已取消"
        return
    messages = snapshot.values.get("messages", [])
    if not messages:
        return

    answered_tool_call_ids = {
        message.tool_call_id
        for message in messages
        if isinstance(message, ToolMessage) and message.tool_call_id
    }

    last_ai_message = next(
        (message for message in reversed(messages) if isinstance(message, AIMessage)),
        None,
    )
    if last_ai_message is None:
        return

    missing_messages: list[ToolMessage] = []
    for tool_call in last_ai_message.tool_calls:
        tool_call_id = tool_call.get("id")
        if not tool_call_id or tool_call_id in answered_tool_call_ids:
            continue
        missing_messages.append(
            ToolMessage(
                content=content,
                tool_call_id=tool_call_id,
                name=tool_call.get("name"),
            )
        )
        answered_tool_call_ids.add(tool_call_id)

    if missing_messages:
        logger.info(
            "Repairing cancelled tool calls for session '%s' with messages: %s",
            config["configurable"]["thread_id"],
            len(missing_messages),
        )
        await agent.aupdate_state(config, {"messages": missing_messages}, as_node="tools")


class RewindError(RuntimeError):
    """目标消息不存在、不是用户消息,或没有可回退的 checkpoint。"""


async def _check_rewind_target(session_id: str, message_id: int):
    msg = await repository.get_message(session_id, message_id)
    if msg is None or msg.role != "user":
        raise RewindError(f"Message {message_id} is not a user message of this conversation")
    if msg.checkpoint_id is None:
        raise RewindError("This message predates checkpoint tracking and cannot be rewound")
    return msg


async def _rewind(session_id: str, message_id: int) -> list[dict]:
    """把 checkpoint 与消息记录都回退到 message_id 这条用户消息之前,返回该消息原有的附件。"""
    msg = await _check_rewind_target(session_id, message_id)

    checkpointer = get_checkpointer()
    if msg.checkpoint_id == "":
        # 第一条消息之前就是空线程
        await checkpointer.adelete_thread(session_id)
    else:
        target = {
            "configurable": {
                "thread_id": session_id,
                "checkpoint_ns": "",
                "checkpoint_id": msg.checkpoint_id,
            }
        }
        # 在旧 checkpoint 上写一次空更新,使其成为线程的最新分支头;旧分支仍保留在库里
        await get_agent().aupdate_state(target, {"messages": []})
    if msg.prompt_id:
        try:
            if await CONTAINER_MANAGER.restore_snapshot(session_id, msg.prompt_id):
                logger.info("Restored sandbox files of session '%s'", session_id)
        except Exception:
            logger.exception("Restoring sandbox files of session '%s' failed", session_id)
    files = await repository.get_message_files(message_id)
    removed = await repository.truncate_from(session_id, message_id)
    logger.info(
        "Rewound session '%s' to before message %s (%s removed)", session_id, message_id, removed
    )
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


def _unanswered_interrupts(snapshot) -> list:
    """还在等回答的 interrupt,按提出顺序。

    并行工具调用会同时挂起多个 interrupt;回答其中一个后,快照的 interrupts 里仍留着它,
    只是对应的 task 已带结果。必须据此排除,否则永远只会重复回答第一个。
    """
    tasks = getattr(snapshot, "tasks", None) or ()
    answered = {
        i.id for t in tasks if getattr(t, "result", None) for i in getattr(t, "interrupts", ())
    }
    return [i for i in snapshot.interrupts if i.id not in answered]


async def _resume_command(agent, config: dict, answer: str) -> Command | None:
    """线程停在提问上时,返回恢复它的 Command;否则返回 None。

    模型并行调用多个需要确认的工具时会同时挂起多个 interrupt,LangGraph 要求按 id 恢复。
    界面一次只展示最早的一个未回答项(见 _run_agent 的 ask.user 发送),所以答案只能属于它;
    其余仍挂起,答完一个后再作为下一张卡片展示。
    """
    snapshot = await agent.aget_state(config)
    pending = _unanswered_interrupts(snapshot)
    if not pending:
        return None
    if len(snapshot.interrupts) == 1:
        return Command(resume=answer)
    return Command(resume={pending[0].id: answer})


async def _record_outcome(session_id: str, status: str) -> None:
    """记下这一轮的终态供会话列表展示;失败只记日志,不影响流。"""
    if not is_db_ready():
        return
    try:
        await repository.set_run_outcome(session_id, status)
    except Exception:
        logger.exception("Failed to record run outcome for session '%s'", session_id)


async def _run_agent(
    prompt_id: str, session_id: str, agent_input: str, raw_answer: str = "", owner: str = ""
) -> None:
    current_owner.set(owner)  # 记忆工具和中间件靠它区分用户;任务自己的上下文副本,不会串到别的运行
    _started_prompts.add(prompt_id)
    stream = None
    cancelled = False
    failed = False
    asked = False
    recorder = TurnRecorder()
    usage = UsageTracker()
    guard = BudgetGuard(
        settings.AGENT_MAX_LLM_CALLS,
        settings.AGENT_MAX_TOTAL_TOKENS,
        settings.AGENT_MAX_REPEATED_CALLS,
    )

    persisted = False

    last_refresh = time.monotonic()

    async def _keep_running_alive() -> None:
        # running 状态的 Redis 键有 TTL:长任务要定期续期,否则超时后同一会话会被放行启动第二个任务
        nonlocal last_refresh
        now = time.monotonic()
        if now - _STATUS_REFRESH_INTERVAL < last_refresh:
            return
        last_refresh = now
        try:
            await refresh_running_ttl(session_id)
        except Exception:
            logger.warning("Refreshing running status TTL failed for session '%s'", session_id)

    presented_office: list[str] = []

    async def emit(event: str, data: dict) -> None:
        recorder.record(event, data)
        if event == "files.presented":
            for f in data.get("files", []):
                path = f.get("container_path") if isinstance(f, dict) else None
                if path and is_office(path):
                    presented_office.append(path)
        await _push(prompt_id, event, data)
        await _keep_running_alive()
        if event in CHECKPOINT_EVENTS and not persisted:
            await persist_assistant_turn(session_id, prompt_id, recorder, "running")

    async def persist(status: str) -> bool:
        # 必须先于"状态置 complete / stream.end":否则下一轮的用户消息可能先落库。
        # 只有写库成功才记为已保存,失败时 finally 里的兜底会再试(同一行更新,不会重复建消息)
        nonlocal persisted
        if persisted:
            return True
        ok = await persist_assistant_turn(
            session_id, prompt_id, recorder, status, usage.to_event() if usage.llm_calls else None
        )
        persisted = ok
        return ok

    config = {
        "configurable": {"thread_id": session_id},
        "metadata": {"trace_id": get_trace_id()},
    }
    try:
        agent = get_agent()
        await _repair_cancelled_tool_calls(agent, config)
        await _record_checkpoint(agent, config, session_id, prompt_id)

        resume = await _resume_command(agent, config, raw_answer or agent_input)
        if resume is not None:
            # 上一轮停在向用户提问上:这条消息就是答案,恢复被挂起的工具调用
            input_state = resume
        else:
            hm = HumanMessage(content=agent_input)
            logger.debug("agent input received: chars=%d", len(agent_input))
            input_state = {"messages": [hm]}

        token_batcher = _TokenBatcher(AI_TOKEN_BATCH_SIZE)

        async def _flush_token_batch() -> None:
            batched = token_batcher.flush()
            if batched:
                await emit("ai.token", {"text": batched})

        stream = agent.astream(
            input_state,
            config=config,
            stream_mode=["messages", "updates", "custom"],
        )
        async for chunk in stream:
            chunk_type = chunk[0]
            chunk_data = chunk[1]

            if chunk_type == "messages":
                token, metadata = chunk_data
                # 只转发主模型节点的输出:摘要中间件、工具内部的模型调用(view_image、
                # review_document)也会经 messages 流出来,不能拼进回答
                if (
                    isinstance(token, AIMessageChunk)
                    and metadata.get("langgraph_node") == _MODEL_NODE
                ):
                    if token.tool_call_chunks:
                        continue
                    text = token.text
                    if text:
                        batched = token_batcher.add(text)
                        if batched is not None:
                            await emit("ai.token", {"text": batched})

            elif chunk_type == "updates":
                await _flush_token_batch()
                for _source, update in chunk_data.items():
                    if _source == "__interrupt__":
                        # 并行挂起多个提问时一次只展示最早的一个,答完再展示下一个
                        if not asked and update:
                            item = update[0]
                            payload = item.value if isinstance(item.value, dict) else {}
                            asked = True
                            await emit("ask.user", payload)
                        continue
                    if not isinstance(update, dict):
                        continue
                    messages = update.get("messages") or []
                    if not messages:
                        continue
                    message = messages[-1]
                    if isinstance(message, AIMessage):
                        usage.add(message)
                    if isinstance(message, AIMessage) and message.tool_calls:
                        await emit("ai.tool_call", _ai_tool_call_event(message))
                    if isinstance(message, AIMessage):
                        guard.check(usage, message.tool_calls)

            elif chunk_type == "custom":
                await _flush_token_batch()
                normalized = _normalize_custom(chunk_data)
                if normalized:
                    event_name, event_data = normalized
                    await emit(event_name, event_data)

        await _flush_token_batch()
        await emit("usage", usage.to_event())
        saved = await persist("complete")
        if saved:
            outcome = "waiting" if asked else "complete"
            await _record_outcome(session_id, outcome)
            await set_thread_status(session_id, outcome)
        else:
            await _record_outcome(session_id, "error")
            # 生成完成不等于已保存:告诉用户这轮内容刷新后可能看不到
            await emit(
                "error",
                {
                    "code": "history_save_failed",
                    "message": "回答已生成,但保存到历史记录失败,刷新页面后可能看不到这一轮内容。",
                },
            )
            await set_thread_status(session_id, "error")
        await emit("stream.end", {})
        if presented_office:
            _spawn_prewarm(session_id, presented_office)

    except asyncio.CancelledError:
        cancelled = True
        logger.info("Cancelling stream for session '%s'", session_id)
        raise
    except Exception as exc:
        failed = True
        # 预算/防循环的提示本来就是写给用户看的,原样展示;其它异常可能带路径、SQL、密钥等内部细节,
        # 只给通用提示和 trace id,详情留在日志里
        if isinstance(exc, BudgetExceeded):
            message = str(exc)
            logger.warning("Agent run stopped for session '%s': %s", session_id, exc)
        else:
            logger.exception("Stream error for session '%s'", session_id)
            message = f"生成过程中出现内部错误,请稍后重试(trace id: {get_trace_id()})"
        await emit("usage", usage.to_event())
        await emit("error", {"message": message})
        await persist("error")
        await _record_outcome(session_id, "error")
        await set_thread_status(session_id, "error")
        await emit("stream.end", {})
    finally:

        async def _finalize() -> None:
            if stream is not None:
                with contextlib.suppress(Exception):
                    await stream.aclose()

            if cancelled:
                try:
                    await _repair_cancelled_tool_calls(
                        get_agent(), config, USER_CANCELLED_TOOL_MESSAGE
                    )
                except Exception:
                    logger.exception(
                        "Failed to repair cancelled tool calls for session '%s'", session_id
                    )
                await persist("cancelled")
                await _record_outcome(session_id, "cancelled")
                await set_thread_status(session_id, "complete")
                await emit("stream.end", {})

            try:
                # 兜底:上面任何一步抛异常都没走到落库时,这里补上
                await persist("cancelled" if cancelled else "error" if failed else "complete")
            finally:
                # 落库失败也要清掉登记并关闭流,否则 cancel_chat 会误判、客户端会一直等
                _started_prompts.discard(prompt_id)
                await mark_done(prompt_id)

        # 清理必须跑完:用户连点停止会再次 cancel 当前任务,若打断清理,
        # stream.end 和状态复位就丢了,客户端会一直等。shield 后被打断也继续等清理结束
        cleanup = asyncio.ensure_future(_finalize())
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                if cleanup.done():
                    break
                interrupted = True
        cleanup.result()
        if interrupted and not cancelled:
            raise asyncio.CancelledError


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
    task.add_done_callback(_background_tasks.discard)


async def stream_chat_events(
    session_id: str,
    prompt_id: str,
    start_id: str = "0-0",
    block_ms: int = 100,
) -> AsyncGenerator[str, None]:
    if not await prompt_belongs_to_session(session_id, prompt_id):
        raise LookupError(f"prompt_id '{prompt_id}' does not belong to session '{session_id}'")

    async for entry_id, token in read_stream(prompt_id, block_ms, start_id):
        payload = json.loads(token)
        yield f"data: {json.dumps({'id': entry_id, **payload})}\n\n"
