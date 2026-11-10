"""Run persistence and atomic message/terminal commits."""

from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select, update

from app.db import get_session
from app.db.models import Conversation, Message, Run, UploadedFile
from app.db.repository import NotOwnerError, make_title

ACTIVE_STATUSES = ("preparing", "running", "finalizing")
TERMINAL_STATUSES = ("complete", "waiting", "error", "cancelled")


class RequestIdConflictError(ValueError):
    pass


async def accepted(session_id: str, owner: str, request_id: str, fingerprint: str) -> str | None:
    async with get_session() as db:
        conv = await db.get(Conversation, session_id)
        if conv is not None and (conv.owner or "") != owner:
            raise NotOwnerError(session_id)
        run = await db.scalar(
            select(Run).where(
                Run.conversation_id == session_id, Run.client_request_id == request_id
            )
        )
        if run is None:
            return None
        if run.request_fingerprint != fingerprint:
            raise RequestIdConflictError(
                "client_request_id was already used for a different message"
            )
        return run.id


async def accept(
    run_id: str,
    session_id: str,
    owner: str,
    input_json: dict,
    request_id: str | None,
    fingerprint: str,
    trace_id: str,
) -> Run:
    async with get_session() as db:
        conv = await db.get(Conversation, session_id)
        if conv is not None and (conv.owner or "") != owner:
            raise NotOwnerError(session_id)
        if conv is None:
            conv = Conversation(id=session_id, owner=owner, title=make_title(input_json["text"]))
            db.add(conv)
            await db.flush()
        input_json = dict(
            input_json, graph_thread_id=conv.graph_thread_id or session_id, snapshot_ready=False
        )
        run = Run(
            id=run_id,
            conversation_id=session_id,
            client_request_id=request_id,
            request_fingerprint=fingerprint,
            input_json=input_json,
            trace_id=trace_id,
            turn_id=run_id,
            status="preparing",
        )
        db.add(run)
        conv.unread = False
        await db.commit()
        return run


async def get(run_id: str, owner: str | None = None) -> Run | None:
    async with get_session() as db:
        query = select(Run).join(Conversation).where(Run.id == run_id)
        if owner is not None:
            query = query.where(Conversation.owner == owner)
        return await db.scalar(query)


async def by_request(session_id: str, request_id: str, owner: str) -> Run | None:
    async with get_session() as db:
        return await db.scalar(
            select(Run)
            .join(Conversation)
            .where(
                Run.conversation_id == session_id,
                Run.client_request_id == request_id,
                Conversation.owner == owner,
            )
        )


async def latest(session_id: str) -> Run | None:
    async with get_session() as db:
        return await db.scalar(
            select(Run)
            .where(Run.conversation_id == session_id)
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(1)
        )


async def message(run_id: str) -> Message | None:
    async with get_session() as db:
        return await db.scalar(
            select(Message)
            .join(
                Run,
                and_(Message.prompt_id == Run.id, Message.conversation_id == Run.conversation_id),
            )
            .where(Run.id == run_id, Message.role == "assistant")
        )


async def for_messages(run_ids: list[str], session_id: str) -> dict[str, Run]:
    if not run_ids:
        return {}
    async with get_session() as db:
        return {
            r.id: r
            for r in (
                await db.scalars(
                    select(Run).where(Run.id.in_(run_ids), Run.conversation_id == session_id)
                )
            ).all()
        }


async def graph_run(
    session_id: str, graph_run_id: str | None, checkpoint_id: str | None = None
) -> Run | None:
    """Find the run that produced the selected graph state, including imported IDs."""
    async with get_session() as db:
        base = select(Run).where(Run.conversation_id == session_id, Run.input_committed.is_(True))
        if graph_run_id:
            result = await db.scalar(
                base.where(
                    or_(
                        Run.id == graph_run_id,
                        Run.input_json["source_run_id"].as_string() == graph_run_id,
                    )
                )
            )
            if result is not None:
                return result
        # For old checkpoints, an input pointing to this state was submitted
        # after it. Exclude those attempts and any abandoned future branch.
        if checkpoint_id:
            boundary = await db.scalar(
                select(func.min(Run.created_at))
                .join(
                    Message,
                    and_(
                        Message.prompt_id == Run.id, Message.conversation_id == Run.conversation_id
                    ),
                )
                .where(
                    Message.conversation_id == session_id,
                    Message.role == "user",
                    Message.checkpoint_id == checkpoint_id,
                )
            )
            if boundary is not None:
                base = base.where(Run.created_at < boundary)
        return await db.scalar(base.order_by(Run.created_at.desc(), Run.id.desc()).limit(1))


async def snapshot_finished(run_id: str, ready: bool) -> None:
    async with get_session() as db:
        run = await db.get(Run, run_id)
        if run is None:
            raise LookupError("Run not found")
        run.input_json = dict(run.input_json, snapshot_ready=ready)
        await db.commit()


async def turn_usage(turn_id: str, exclude_run: str) -> tuple[int, int]:
    async with get_session() as db:
        rows = (
            await db.scalars(select(Run.usage).where(Run.turn_id == turn_id, Run.id != exclude_run))
        ).all()
        return (
            sum(int((r or {}).get("llm_calls", 0)) for r in rows),
            sum(int((r or {}).get("total_tokens", 0)) for r in rows),
        )


async def latest_statuses(session_ids: list[str]) -> dict[str, str]:
    if not session_ids:
        return {}
    async with get_session() as db:
        ranked = (
            select(
                Run.conversation_id,
                Run.status,
                func.row_number()
                .over(
                    partition_by=Run.conversation_id,
                    order_by=(Run.created_at.desc(), Run.id.desc()),
                )
                .label("rank"),
            )
            .where(Run.conversation_id.in_(session_ids))
            .subquery()
        )
        rows = await db.execute(
            select(ranked.c.conversation_id, ranked.c.status).where(ranked.c.rank == 1)
        )
        return dict(rows.all())


async def start(
    run_id: str,
    text: str,
    files: list[dict],
    checkpoint_id: str,
    rewind_message_id: int | None = None,
    graph_ref: dict | None = None,
    resume_from: Run | None = None,
) -> None:
    async with get_session() as db:
        run = await db.get(Run, run_id)
        if run is None or run.status != "preparing":
            raise RuntimeError("Run is not preparing")
        sid = run.conversation_id
        if rewind_message_id is not None:
            ids = select(Message.id).where(
                Message.conversation_id == sid, Message.id >= rewind_message_id
            )
            await db.execute(delete(UploadedFile).where(UploadedFile.message_id.in_(ids)))
            await db.execute(
                delete(Message).where(
                    Message.conversation_id == sid, Message.id >= rewind_message_id
                )
            )
        user = Message(
            conversation_id=sid,
            prompt_id=run_id,
            role="user",
            content=text,
            parts=[{"type": "text", "text": text}],
            checkpoint_id=checkpoint_id,
        )
        db.add(user)
        await db.flush()
        db.add_all(
            UploadedFile(
                conversation_id=sid,
                message_id=user.id,
                file_name=f["file_name"],
                container_path=f["container_path"],
            )
            for f in files
        )
        run.status = "running"
        run.input_committed = True
        if resume_from is not None:
            if resume_from.conversation_id != sid:
                raise ValueError("Resume source belongs to another conversation")
            run.turn_id = resume_from.turn_id
            run.resume_of_run_id = resume_from.id
        if graph_ref is not None:
            await db.execute(
                update(Conversation)
                .where(Conversation.id == sid)
                .values(
                    graph_thread_id=graph_ref["thread_id"],
                    graph_checkpoint_id=graph_ref.get("checkpoint_id"),
                )
            )
        await db.execute(
            update(Conversation)
            .where(Conversation.id == sid)
            .values(last_run_status=None, unread=False, updated_at=datetime.now(UTC))
        )
        await db.commit()


async def graph_config(session_id: str) -> dict:
    async with get_session() as db:
        conv = await db.get(Conversation, session_id)
        config = {"thread_id": (conv.graph_thread_id if conv else None) or session_id}
        if conv and conv.graph_checkpoint_id:
            config["checkpoint_id"] = conv.graph_checkpoint_id
        return {"configurable": config}


async def graph_threads(session_id: str) -> set[str]:
    config = await graph_config(session_id)
    async with get_session() as db:
        inputs = (
            await db.scalars(select(Run.input_json).where(Run.conversation_id == session_id))
        ).all()
    return {
        session_id,
        config["configurable"]["thread_id"],
        *(i["graph_thread_id"] for i in inputs if i.get("graph_thread_id")),
    }


async def rewind_history(session_id: str, message_id: int, graph_ref: dict) -> None:
    async with get_session() as db:
        ids = select(Message.id).where(
            Message.conversation_id == session_id, Message.id >= message_id
        )
        await db.execute(delete(UploadedFile).where(UploadedFile.message_id.in_(ids)))
        await db.execute(
            delete(Message).where(Message.conversation_id == session_id, Message.id >= message_id)
        )
        await db.execute(
            update(Conversation)
            .where(Conversation.id == session_id)
            .values(
                graph_thread_id=graph_ref["thread_id"],
                graph_checkpoint_id=graph_ref.get("checkpoint_id"),
            )
        )
        await db.commit()


async def graph_was_written(session_id: str, run_id: str, agent) -> bool:
    config = await graph_config(session_id)
    latest_config = {"configurable": {"thread_id": config["configurable"]["thread_id"]}}
    latest_state = await agent.aget_state(latest_config)
    return (getattr(latest_state, "metadata", None) or {}).get("run_id") == run_id


async def save(
    run_id: str,
    parts: list,
    text: str,
    seq: int,
    usage: dict,
    status: str = "running",
    error_code: str | None = None,
    public_error: str | None = None,
    graph_advanced: bool = False,
) -> None:
    async with get_session() as db:
        run = await db.get(Run, run_id)
        if run is None:
            raise LookupError("Run not found")
        if run.status in TERMINAL_STATUSES:
            return  # A retry cannot overwrite a durable result.
        if status in TERMINAL_STATUSES and not run.input_committed and "text" in run.input_json:
            user = await db.scalar(
                select(Message).where(
                    Message.conversation_id == run.conversation_id,
                    Message.prompt_id == run_id,
                    Message.role == "user",
                )
            )
            if user is None:
                user = Message(
                    conversation_id=run.conversation_id,
                    prompt_id=run_id,
                    role="user",
                    content=run.input_json["text"],
                    parts=[{"type": "text", "text": run.input_json["text"]}],
                    status="cancelled" if status == "cancelled" else "error",
                    created_at=run.created_at,
                )
                db.add(user)
                await db.flush()
                db.add_all(
                    UploadedFile(
                        conversation_id=run.conversation_id,
                        message_id=user.id,
                        file_name=f["file_name"],
                        container_path=f["container_path"],
                    )
                    for f in run.input_json.get("files", [])
                )
        message = await db.scalar(
            select(Message).where(
                Message.conversation_id == run.conversation_id,
                Message.prompt_id == run_id,
                Message.role == "assistant",
            )
        )
        if message is None:
            message = Message(
                conversation_id=run.conversation_id, prompt_id=run_id, role="assistant"
            )
            db.add(message)
        message.parts, message.content = parts, text
        message.status = "complete" if status == "waiting" else status
        message.last_event_seq, message.usage = seq, usage
        run.usage = usage
        if status in TERMINAL_STATUSES:
            run.status, run.error_code, run.public_error = status, error_code, public_error
            run.finished_at = datetime.now(UTC)
            await db.execute(
                update(Conversation)
                .where(Conversation.id == run.conversation_id)
                .values(last_run_status=status, unread=True, updated_at=run.finished_at)
            )
            if graph_advanced:
                await db.execute(
                    update(Conversation)
                    .where(Conversation.id == run.conversation_id)
                    .values(graph_checkpoint_id=None)
                )
        await db.commit()


async def recover_interrupted(agent=None) -> int:
    async with get_session() as db:
        interrupted = list(
            (await db.scalars(select(Run).where(Run.status.in_(ACTIVE_STATUSES)))).all()
        )
    for run in interrupted:
        previous = await message(run.id)
        advanced = bool(
            agent is not None and await graph_was_written(run.conversation_id, run.id, agent)
        )
        parts = list(previous.parts) if previous else []
        pending = []
        if advanced:
            from app.agents.recovery import _unanswered_interrupts

            config = await graph_config(run.conversation_id)
            config["configurable"].pop("checkpoint_id", None)
            pending = _unanswered_interrupts(await agent.aget_state(config))
        if pending and not any(
            p.get("type") == "ask" and p.get("interrupt_id") == pending[0].id for p in parts
        ):
            value = pending[0].value if isinstance(pending[0].value, dict) else {}
            parts.append({**value, "type": "ask", "interrupt_id": pending[0].id})
        await save(
            run.id,
            parts,
            previous.content if previous else "",
            (previous.last_event_seq or 0) if previous else 0,
            run.usage or {},
            "waiting" if pending else "error",
            None if pending else "process_interrupted",
            None if pending else "服务重启,本次生成已中断",
            advanced,
        )
    return len(interrupted)
