"""LangGraph message-protocol repair and interrupt selection."""

import copy
from collections import defaultdict

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from app.utils import logger

NULL_TASK_ID = "00000000-0000-0000-0000-000000000000"
USER_CANCELLED_TOOL_MESSAGE = "Execution cancelled by user."
INTERRUPTED_TOOL_MESSAGE = "Execution was interrupted before this tool call finished."


class ResumeConflictError(ValueError):
    pass


async def fork_answered_interrupt(
    agent, config: dict, snapshot, destination: str, discard: set[str] | None = None
) -> dict:
    """Copy history without replaying completed siblings or reusing rewound answers.

    Parallel answers share one checkpoint; each is a task's pending writes there.
    ``discard`` names the interrupts whose answers are rewound (the target and
    later answers). Earlier answers stay. ``None`` (legacy runs that did not
    record their interrupt) clears every answer at the pause.
    """
    saver = agent.checkpointer
    interrupt_ids = {i.id for task in snapshot.tasks for i in task.interrupts}
    if discard is not None:
        interrupt_ids &= discard
    interrupted = {t.id for t in snapshot.tasks if any(i.id in interrupt_ids for i in t.interrupts)}
    cid = config["configurable"]["checkpoint_id"]
    source = config["configurable"]["thread_id"]
    rows = [row async for row in saver.alist({"configurable": {"thread_id": source}}, limit=20001)]
    if len(rows) > 20000:
        raise ResumeConflictError("执行历史过大,无法安全复制这条确认的恢复点")
    heads = {"": cid}
    interrupted_tasks = {"": interrupted}
    # A task-tool interrupt may originate in a nested graph. Restore that graph
    # to its matching pause as well, not its later completed checkpoint.
    for row in rows:
        namespace = row.config["configurable"].get("checkpoint_ns", "")
        if not namespace:
            continue
        tasks = {
            task_id
            for task_id, channel, values in row.pending_writes or []
            if channel == "__interrupt__" and any(i.id in interrupt_ids for i in values)
        }
        if tasks and namespace not in heads:
            heads[namespace] = row.config["configurable"]["checkpoint_id"]
            interrupted_tasks[namespace] = tasks
    for row in reversed(rows):
        configurable = row.config["configurable"]
        namespace = configurable.get("checkpoint_ns", "")
        if namespace in heads and configurable["checkpoint_id"] > heads[namespace]:
            continue
        base = {"thread_id": destination, "checkpoint_ns": namespace}
        if row.parent_config:
            base["checkpoint_id"] = row.parent_config["configurable"]["checkpoint_id"]
        checkpoint = copy.deepcopy(row.checkpoint)
        copied = await saver.aput(
            {"configurable": base}, checkpoint, row.metadata, checkpoint["channel_versions"]
        )
        writes = defaultdict(list)
        for task_id, channel, value in row.pending_writes or []:
            if namespace in heads and configurable["checkpoint_id"] == heads[namespace]:
                if task_id == NULL_TASK_ID and channel == "__resume__":
                    continue  # a thread-wide resume map would re-answer rewound questions
                if task_id in interrupted_tasks[namespace] and channel != "__interrupt__":
                    continue  # includes the task's __resume__ answer
            writes[task_id].append((channel, value))
        for task_id, values in writes.items():
            await saver.aput_writes(copied, values, task_id)
    # Resume the new thread's head, rather than explicitly replaying the selected
    # checkpoint. Replay deliberately re-executes even completed sibling tasks.
    return {"thread_id": destination}


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
        updated = await agent.aupdate_state(config, {"messages": missing_messages}, as_node="tools")
        if config["configurable"].get("checkpoint_id") and updated:
            config["configurable"].update(updated["configurable"])


def resumed_interrupt_id(command: Command | None, snapshot) -> str | None:
    """The interrupt a resume Command answers, recorded so a rewind keeps earlier answers."""
    if command is None:
        return None
    if isinstance(command.resume, dict):
        return next(iter(command.resume), None)
    pending = _unanswered_interrupts(snapshot)
    return pending[0].id if pending else None


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


async def _resume_command(
    agent,
    config: dict,
    answer: str,
    *,
    interrupt_id: str | None = None,
    mode: str = "auto",
    snapshot=None,
) -> Command | None:
    """线程停在提问上时,返回恢复它的 Command;否则返回 None。

    模型并行调用多个需要确认的工具时会同时挂起多个 interrupt,LangGraph 要求按 id 恢复。
    界面一次只展示最早的一个未回答项(见 _run_agent 的 ask.user 发送),所以答案只能属于它;
    其余仍挂起,答完一个后再作为下一张卡片展示。
    """
    if snapshot is None:
        snapshot = await agent.aget_state(config)
    pending = _unanswered_interrupts(snapshot)
    if not pending:
        if mode == "resume" or interrupt_id:
            raise ResumeConflictError("该问题已失效,请重新打开会话")
        return None
    if mode == "new":
        raise ResumeConflictError("会话正在等待确认,请先回答或拒绝当前操作")
    if interrupt_id:
        if interrupt_id not in {item.id for item in pending}:
            raise ResumeConflictError("该问题已失效,请重新打开会话")
        return Command(resume={interrupt_id: answer})
    if len(snapshot.interrupts) == 1:
        return Command(resume=answer)
    return Command(resume={pending[0].id: answer})
