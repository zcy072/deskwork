"""Numbered, transactional data migrations for the existing SQLite application."""

from itertools import groupby

from sqlalchemy import func, insert, select

from app.db.models import Conversation, Message, Run, SchemaRevision
from app.utils import logger


def migrate_runs(conn) -> None:
    if conn.scalar(select(SchemaRevision.version).where(SchemaRevision.version == 1)):
        return
    messages = Message.__table__
    conversations = {r.id: r for r in conn.execute(select(Conversation.__table__))}
    existing = set(conn.scalars(select(Run.id)))
    orphaned = conn.scalar(
        select(func.count())
        .select_from(
            messages.outerjoin(
                Conversation.__table__, messages.c.conversation_id == Conversation.id
            )
        )
        .where(Conversation.id.is_(None))
    )
    if orphaned:
        logger.warning(
            "Skipping %d orphaned legacy messages during run migration; original rows retained",
            orphaned,
        )
    rows = conn.execute(
        select(messages)
        .join(Conversation.__table__, messages.c.conversation_id == Conversation.id)
        .where(messages.c.prompt_id != "")
        .order_by(messages.c.conversation_id, messages.c.prompt_id, messages.c.id)
    )
    for (sid, pid), items in groupby(rows, lambda r: (r.conversation_id, r.prompt_id)):
        if pid in existing:
            continue  # Ambiguous legacy prompt IDs remain readable, without inventing a mapping.
        group = list(items)
        user = next((r for r in group if r.role == "user"), None)
        assistant = next((r for r in reversed(group) if r.role == "assistant"), None)
        status = assistant.status if assistant else "error"
        if status == "running":
            status = "error"
        conv = conversations[sid]
        last_pid = conn.scalar(
            select(messages.c.prompt_id)
            .where(messages.c.conversation_id == sid)
            .order_by(messages.c.id.desc())
            .limit(1)
        )
        if pid == last_pid and conv.last_run_status in (
            "complete",
            "waiting",
            "error",
            "cancelled",
        ):
            status = conv.last_run_status
        conn.execute(
            insert(Run).values(
                id=pid,
                conversation_id=sid,
                turn_id=pid,
                input_json={"legacy": True},
                input_committed=user is not None,
                status=status,
                created_at=group[0].created_at,
                finished_at=group[-1].created_at,
                usage=assistant.usage if assistant else None,
                request_fingerprint="",
                trace_id="",
            )
        )
        existing.add(pid)
    conn.execute(insert(SchemaRevision).values(version=1))
