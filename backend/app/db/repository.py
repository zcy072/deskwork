"""会话 / 消息 / 文件元数据的读写。上层服务只通过这里访问数据库。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update

from app.db import get_session
from app.db.models import Conversation, Memory, Message, UploadedFile

TITLE_MAX_CHARS = 40


class NotOwnerError(Exception):
    """会话属于另一个 owner。"""


def make_title(text: str) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= TITLE_MAX_CHARS:
        return one_line
    return one_line[: TITLE_MAX_CHARS - 1] + "…"


async def conversation_visible(
    conversation_id: str, owner: str, *, allow_missing: bool = True
) -> bool:
    """会话是否对 owner 可见。

    allow_missing=True 时不存在也算可见(新会话由第一次发送创建);
    读取类接口传 False,避免凭一个没有记录的 session_id 访问沙箱文件或事件流。
    属于别人的会话任何情况下都不可见。
    """
    async with get_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is None:
            return allow_missing
        return (conv.owner or "") == owner


async def ensure_conversation(
    conversation_id: str, first_query: str = "", owner: str = ""
) -> Conversation:
    """会话不存在则创建(标题取首条消息);存在则返回。属于别的 owner 抛 NotOwnerError。"""
    async with get_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is not None and (conv.owner or "") != owner:
            raise NotOwnerError(conversation_id)
        if conv is None:
            conv = Conversation(id=conversation_id, title=make_title(first_query), owner=owner)
            s.add(conv)
            await s.commit()
        elif not conv.title and first_query:
            conv.title = make_title(first_query)
            await s.commit()
        return conv


async def save_assistant_message(
    conversation_id: str,
    prompt_id: str,
    *,
    content: str,
    parts: list,
    status: str,
    usage: dict | None = None,
    message_id: int | None = None,
) -> Message:
    """同一轮(prompt_id)只有一条 assistant 消息:不存在则创建,存在则整体更新。

    调用方已知行 id 时(message_id)直接按主键更新,省掉查询;行不存在则退回查找/创建。

    生成过程中以 status="running" 反复调用,保证服务中途崩溃时已产出的内容不丢;结束时写终态。
    """
    async with get_session() as s:
        if message_id is not None:
            values = {"content": content, "parts": list(parts), "status": status}
            if usage is not None:
                values["usage"] = usage
            result = await s.execute(
                update(Message)
                .where(Message.id == message_id, Message.conversation_id == conversation_id)
                .values(**values)
            )
            if result.rowcount:
                await s.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(updated_at=datetime.now(UTC))
                )
                await s.commit()
                return Message(
                    id=message_id,
                    conversation_id=conversation_id,
                    prompt_id=prompt_id,
                    role="assistant",
                    **values,
                )
        msg = (
            await s.execute(
                select(Message).where(
                    Message.conversation_id == conversation_id,
                    Message.prompt_id == prompt_id,
                    Message.role == "assistant",
                )
            )
        ).scalar_one_or_none()
        if msg is None:
            msg = Message(conversation_id=conversation_id, prompt_id=prompt_id, role="assistant")
            s.add(msg)
        msg.content, msg.parts, msg.status = content, list(parts), status
        if usage is not None:
            msg.usage = usage
        conv = await s.get(Conversation, conversation_id)
        if conv is not None:
            conv.updated_at = datetime.now(UTC)
        await s.commit()
        return msg


async def set_run_outcome(conversation_id: str, status: str) -> None:
    """记下最近一轮的终态并标为未读;会话已被删除则什么都不做。"""
    async with get_session() as s:
        await s.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(last_run_status=status, unread=True)
        )
        await s.commit()


async def clear_run_state(conversation_id: str) -> None:
    """新一轮开始:上一轮的终态和未读标记都作废。"""
    async with get_session() as s:
        await s.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(last_run_status=None, unread=False)
        )
        await s.commit()


async def mark_read(conversation_id: str, owner: str = "") -> bool:
    """清除未读;不是自己的或不存在的会话返回 False。"""
    async with get_session() as s:
        result = await s.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.owner == owner)
            .values(unread=False)
        )
        await s.commit()
        return bool(result.rowcount)


async def mark_interrupted_messages() -> int:
    """启动时调用:仍是 running 的 assistant 消息属于上一个进程,改为 error,
    所在会话记为 error 且未读。"""
    async with get_session() as s:
        stuck = select(Message.conversation_id).where(
            Message.role == "assistant", Message.status == "running"
        )
        await s.execute(
            update(Conversation)
            .where(Conversation.id.in_(stuck))
            .values(last_run_status="error", unread=True)
        )
        result = await s.execute(
            update(Message)
            .where(Message.role == "assistant", Message.status == "running")
            .values(status="error")
        )
        await s.commit()
        return result.rowcount or 0


async def add_user_message_with_files(
    conversation_id: str,
    *,
    prompt_id: str,
    content: str,
    parts: list,
    files: list[dict],
) -> Message:
    """同一个事务里写用户消息和它的附件记录:任一步失败,两者都不落库。"""
    async with get_session() as s:
        msg = Message(
            conversation_id=conversation_id,
            prompt_id=prompt_id,
            role="user",
            content=content,
            parts=parts,
            status="complete",
        )
        s.add(msg)
        await s.flush()  # 取得 msg.id,附件要引用它
        s.add_all(
            UploadedFile(
                conversation_id=conversation_id,
                message_id=msg.id,
                file_name=f["file_name"],
                container_path=f["container_path"],
            )
            for f in files
        )
        conv = await s.get(Conversation, conversation_id)
        if conv is not None:
            conv.updated_at = datetime.now(UTC)
        await s.commit()
        return msg


async def import_conversation(
    conversation_id: str,
    owner: str,
    *,
    title: str,
    created_at: datetime,
    updated_at: datetime,
    messages: list[dict],
) -> None:
    """一个事务里建出会话、消息和附件记录;消息 id 重新分配。已有同 id 会话时抛 NotOwnerError。"""
    async with get_session() as s:
        if await s.get(Conversation, conversation_id) is not None:
            raise NotOwnerError(conversation_id)
        s.add(
            Conversation(
                id=conversation_id,
                title=title,
                owner=owner,
                created_at=created_at,
                updated_at=updated_at,
                last_run_status=None,
                unread=False,
            )
        )
        for m in messages:
            msg = Message(
                conversation_id=conversation_id,
                prompt_id=m["prompt_id"],
                role=m["role"],
                content=m["content"],
                parts=m["parts"],
                status=m["status"],
                checkpoint_id=m["checkpoint_id"],
                usage=m["usage"],
                created_at=m["created_at"],
            )
            s.add(msg)
            if m["attachments"]:
                await s.flush()
                s.add_all(
                    UploadedFile(
                        conversation_id=conversation_id,
                        message_id=msg.id,
                        file_name=a["file_name"],
                        container_path=a["container_path"],
                        created_at=m["created_at"],
                    )
                    for a in m["attachments"]
                )
        await s.commit()


async def list_conversations(
    limit: int = 50, offset: int = 0, owner: str = ""
) -> list[tuple[Conversation, int]]:
    """按最近更新倒序,同时返回每个会话的消息数。"""
    async with get_session() as s:
        # 只对分页后的会话做相关子查询计数,不在全表上 GROUP BY
        n = (
            select(func.count(Message.id))
            .where(Message.conversation_id == Conversation.id)
            .correlate(Conversation)
            .scalar_subquery()
        )
        stmt = (
            select(Conversation, n)
            .where(Conversation.owner == owner)
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [(c, n) for c, n in (await s.execute(stmt)).all()]


def _like_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet(content: str, q: str, radius: int = 30) -> str:
    i = content.lower().find(q.lower())
    if i < 0:
        return content[: radius * 2].replace("\n", " ")
    start = max(0, i - radius)
    text = content[start : i + len(q) + radius].replace("\n", " ")
    return ("…" if start else "") + text + ("…" if i + len(q) + radius < len(content) else "")


async def search_conversations(
    q: str, owner: str = "", limit: int = 30
) -> list[tuple[Conversation, str]]:
    """按标题与消息正文搜索(不区分大小写的子串匹配),只返回 owner 自己的会话。

    返回 (会话, 命中片段),按最近更新倒序;标题命中的片段为空串,正文命中取最新一条命中的消息。
    """
    pattern = f"%{_like_escape(q)}%"
    async with get_session() as s:
        title_hits = (
            await s.execute(
                select(Conversation)
                .where(Conversation.owner == owner, Conversation.title.like(pattern, escape="\\"))
                .order_by(Conversation.updated_at.desc())
                .limit(limit)
            )
        ).scalars()
        found: dict[str, tuple[Conversation, str]] = {c.id: (c, "") for c in title_hits}
        body_hits = await s.execute(
            select(Conversation, Message.content)
            .join(Message, Message.conversation_id == Conversation.id)
            .where(Conversation.owner == owner, Message.content.like(pattern, escape="\\"))
            .order_by(Message.id.desc())
            .limit(limit * 20)
        )
        for conv, content in body_hits.all():
            hit = found.get(conv.id)
            if hit is None:
                if len(found) < limit * 2:
                    found[conv.id] = (conv, _snippet(content, q))
            elif not hit[1]:
                found[conv.id] = (hit[0], _snippet(content, q))
    ranked = sorted(found.values(), key=lambda t: t[0].updated_at, reverse=True)
    return ranked[:limit]


_DOWNLOADABLE_PREFIXES = ("/workspace/uploads/", "/workspace/output/")


def normalize_delivered_parts(parts: list | None) -> list:
    """旧版本的交付记录把 container_path 记成 agent 传入的原路径(可能在 scratchpad/),
    而下载接口只放行 uploads/ 与 output/。交付时文件已复制到 output/<文件名>,读出时改指向那份副本。
    不改库里的数据,也不改动已经合规的记录。"""
    result = []
    for p in parts or []:
        if isinstance(p, dict) and p.get("type") == "files":
            files = []
            for f in p.get("files", []):
                path = f.get("container_path") if isinstance(f, dict) else None
                name = f.get("file_name") if isinstance(f, dict) else None
                if path and name and not path.startswith(_DOWNLOADABLE_PREFIXES):
                    f = {**f, "container_path": f"/workspace/output/{name}"}
                files.append(f)
            p = {**p, "files": files}
        result.append(p)
    return result


async def get_messages(
    conversation_id: str,
    *,
    owner: str | None = None,
    limit: int | None = None,
    before_id: int | None = None,
    from_id: int | None = None,
) -> list[Message] | None:
    """会话不存在(或 owner 不匹配,仅当传了 owner)返回 None。

    limit/before_id 都不传则返回全部(按 id 升序)。传 limit 时取 id < before_id 的最后 limit 条,
    结果仍按 id 升序。from_id 只要 id >= from_id 的消息,用来刷新已加载的那一段。
    """
    async with get_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is None or (owner is not None and (conv.owner or "") != owner):
            return None
        stmt = select(Message).where(Message.conversation_id == conversation_id)
        if before_id is not None:
            stmt = stmt.where(Message.id < before_id)
        if from_id is not None:
            stmt = stmt.where(Message.id >= from_id)
        if limit is None:
            rows = list((await s.execute(stmt.order_by(Message.id))).scalars())
        else:
            rows = list((await s.execute(stmt.order_by(Message.id.desc()).limit(limit))).scalars())
            rows.reverse()
        return rows


async def get_conversation(conversation_id: str, owner: str | None = None) -> Conversation | None:
    async with get_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is None or (owner is not None and (conv.owner or "") != owner):
            return None
        return conv


async def set_user_checkpoint(conversation_id: str, prompt_id: str, checkpoint_id: str) -> None:
    """记录某轮用户消息开始前的 checkpoint;"" 表示空线程。"""
    async with get_session() as s:
        await s.execute(
            update(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.prompt_id == prompt_id,
                Message.role == "user",
            )
            .values(checkpoint_id=checkpoint_id)
        )
        await s.commit()


async def get_message(conversation_id: str, message_id: int) -> Message | None:
    async with get_session() as s:
        msg = await s.get(Message, message_id)
        if msg is None or msg.conversation_id != conversation_id:
            return None
        return msg


async def get_message_files(message_id: int) -> list[dict]:
    async with get_session() as s:
        stmt = select(UploadedFile).where(UploadedFile.message_id == message_id)
        return [
            {"file_name": f.file_name, "container_path": f.container_path}
            for f in (await s.execute(stmt)).scalars()
        ]


async def get_files_for_messages(message_ids: list[int]) -> dict[int, list[dict]]:
    """一次查询取多条消息的附件,按 message_id 分组。"""
    if not message_ids:
        return {}
    async with get_session() as s:
        stmt = (
            select(UploadedFile)
            .where(UploadedFile.message_id.in_(message_ids))
            .order_by(UploadedFile.id)
        )
        grouped: dict[int, list[dict]] = {}
        for f in (await s.execute(stmt)).scalars():
            grouped.setdefault(f.message_id, []).append(
                {"file_name": f.file_name, "container_path": f.container_path}
            )
        return grouped


async def truncate_from(conversation_id: str, message_id: int) -> int:
    """删除该会话中 id >= message_id 的消息及其文件记录(沙箱里的文件保留),返回删除的消息数。"""
    async with get_session() as s:
        ids = select(Message.id).where(
            Message.conversation_id == conversation_id, Message.id >= message_id
        )
        await s.execute(delete(UploadedFile).where(UploadedFile.message_id.in_(ids)))
        result = await s.execute(
            delete(Message).where(
                Message.conversation_id == conversation_id, Message.id >= message_id
            )
        )
        await s.commit()
        return result.rowcount or 0


async def rename_conversation(conversation_id: str, title: str) -> bool:
    async with get_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is None:
            return False
        conv.title = make_title(title)
        await s.commit()
        return True


async def delete_conversation(conversation_id: str) -> bool:
    async with get_session() as s:
        if await s.get(Conversation, conversation_id) is None:
            return False
        # 先批量删子表(文件 -> 消息),避免 ORM 级联把每条 Message 加载进内存;
        # uploaded_files.message_id 外键指向 messages,所以文件必须先删
        await s.execute(delete(UploadedFile).where(UploadedFile.conversation_id == conversation_id))
        await s.execute(delete(Message).where(Message.conversation_id == conversation_id))
        await s.execute(delete(Conversation).where(Conversation.id == conversation_id))
        await s.commit()
        return True


async def get_usage_rows(conversation_id: str) -> list[dict] | None:
    """会话内各 assistant 消息记录的 usage;会话不存在返回 None。"""
    async with get_session() as s:
        if await s.get(Conversation, conversation_id) is None:
            return None
        stmt = select(Message.usage).where(
            Message.conversation_id == conversation_id, Message.usage.is_not(None)
        )
        return [u for u in (await s.execute(stmt)).scalars() if u]


async def get_conversation_assets(conversation_id: str, owner: str | None = None) -> dict | None:
    """整个会话的上传、交付文件和最新计划,不受消息分页影响。会话不存在返回 None。

    交付文件与计划藏在 assistant 消息的 parts 里,只能按行读出再汇总;parts 可能很大,
    所以分批流式读取 parts,不一次把整个会话的正文和 widget HTML 装入内存。
    """
    async with get_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is None or (owner is not None and (conv.owner or "") != owner):
            return None
        uploads = (
            await s.execute(
                select(UploadedFile.file_name, UploadedFile.container_path)
                .where(UploadedFile.conversation_id == conversation_id)
                .order_by(UploadedFile.id)
            )
        ).all()
        rows = await s.stream_scalars(
            select(Message.parts)
            .where(Message.conversation_id == conversation_id, Message.role == "assistant")
            .order_by(Message.id)
            .execution_options(yield_per=100)
        )
        delivered: dict[str, dict] = {}
        todos: list = []
        async for parts in rows:
            for p in normalize_delivered_parts(parts):
                if p.get("type") == "files":
                    for f in p.get("files", []):
                        if isinstance(f, dict):
                            delivered[f.get("container_path") or f.get("file_name", "")] = f
                elif (
                    p.get("type") == "tool_call"
                    and p.get("name") == "write_todos"
                    and isinstance((p.get("args") or {}).get("todos"), list)
                ):
                    todos = p["args"]["todos"]
        return {
            "uploaded": [{"file_name": n, "container_path": c} for n, c in uploads],
            "delivered": list(delivered.values()),
            "todos": todos,
        }


# ---------- 长期记忆 ----------


def _active(now: datetime):
    return (Memory.expires_at.is_(None)) | (Memory.expires_at > now)


def _mine(owner: str):
    return Memory.owner == owner  # NULL 已在启动时规范成 ""(见 db.init_db)


async def adopt_legacy_rows(owner: str) -> int:
    """owner 列加入之前的旧数据(NULL)和未开认证时建的数据("")归给 owner,返回处理的行数。"""
    total = 0
    async with get_session() as s:
        for model in (Conversation, Memory):
            result = await s.execute(
                update(model)
                .where((model.owner.is_(None)) | (model.owner == ""))
                .values(owner=owner)
            )
            total += result.rowcount or 0
        await s.commit()
    return total


async def list_memories(owner: str = "", now: datetime | None = None) -> list[Memory]:
    """某 owner 未过期的记忆,最近使用的在前。"""
    now = now or datetime.now(UTC)
    async with get_session() as s:
        stmt = (
            select(Memory)
            .where(_active(now), _mine(owner))
            .order_by(Memory.last_used_at.desc(), Memory.id.desc())
        )
        return list((await s.execute(stmt)).scalars())


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite 读回来可能丢掉时区


async def add_memory(content: str, ttl_days: int | None, max_items: int, owner: str = "") -> Memory:
    """保存一条记忆。内容完全相同则只刷新时间与过期;超过 max_items 时淘汰该 owner 最久未使用的。"""
    now = datetime.now(UTC)
    expires = now + timedelta(days=ttl_days) if ttl_days else None
    async with get_session() as s:
        await s.execute(delete(Memory).where(~_active(now)))
        existing = (
            await s.execute(select(Memory).where(Memory.content == content, _mine(owner)))
        ).scalar_one_or_none()
        if existing is not None:
            existing.last_used_at, existing.expires_at = now, expires
            await s.commit()
            return existing
        mem = Memory(
            content=content, owner=owner, created_at=now, last_used_at=now, expires_at=expires
        )
        s.add(mem)
        await s.flush()
        total = (await s.execute(select(func.count(Memory.id)).where(_mine(owner)))).scalar_one()
        if max_items and total > max_items:
            oldest = (
                select(Memory.id)
                .where(Memory.id != mem.id, _mine(owner))
                .order_by(Memory.last_used_at.asc(), Memory.id.asc())
                .limit(total - max_items)
            )
            await s.execute(delete(Memory).where(Memory.id.in_(oldest)))
        await s.commit()
        return mem


async def touch_memories(ids: list[int], owner: str = "") -> None:
    """被召回的记忆更新最近使用时间;有过期时间的按各自原有时长顺延(用得越多越不容易过期)。

    时长 = expires_at - last_used_at:写入/刷新时两者相差正好是该条记忆的 ttl,之后每次顺延都保持它,
    所以单条记忆自定义的 ttl 不会被默认值覆盖。
    """
    if not ids:
        return
    now = datetime.now(UTC)
    async with get_session() as s:
        rows = (await s.execute(select(Memory).where(Memory.id.in_(ids), _mine(owner)))).scalars()
        for mem in rows:
            if mem.expires_at is not None:
                mem.expires_at = now + (_aware(mem.expires_at) - _aware(mem.last_used_at))
            mem.last_used_at = now
        await s.commit()


async def delete_memory(memory_id: int, owner: str = "") -> bool:
    async with get_session() as s:
        result = await s.execute(delete(Memory).where(Memory.id == memory_id, _mine(owner)))
        await s.commit()
        return bool(result.rowcount)
