"""会话、消息、上传文件的表结构。

assistant 消息的 parts 按时间顺序保存 text / tool_call / tool_result / files / widget,
前端可以据此原样还原一轮对话的执行过程。
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SchemaRevision(Base):
    __tablename__ = "schema_revisions"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)


class Conversation(Base):
    __tablename__ = "conversations"

    # 与 session_id / LangGraph thread_id 一致
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    # 见 app/owner.py;NULL 是引入 owner 之前的旧数据,启动时由 adopt_legacy_rows 归属
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # 最近一轮的终态 complete|waiting|error|cancelled;运行中不入库(看 Redis),NULL 表示无记录
    last_run_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 该终态用户还没看过;NULL(旧数据)按已读处理
    unread: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    graph_thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    graph_checkpoint_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.id"
    )

    __table_args__ = (Index("ix_conversations_owner_updated", "owner", "updated_at"),)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    prompt_id: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")  # 纯文本摘要,便于检索
    parts: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(
        String(16), default="complete"
    )  # running|complete|error|cancelled
    # 仅 user 消息使用:该消息开始前的 LangGraph checkpoint id,用于回退重发。
    # "" 表示空线程(第一条消息),NULL 表示未记录(旧数据),不可回退
    checkpoint_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 仅 assistant 消息使用:这一轮的 usage 事件(token、by_model)。NULL 表示未记录
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_event_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Run(Base):
    """Durable execution facts; asyncio tasks and transport status are not persisted here."""

    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    client_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    turn_id: Mapped[str] = mapped_column(String(64))
    resume_of_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    input_committed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="preparing")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    public_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("conversation_id", "client_request_id", name="uq_run_request"),
        Index("ix_runs_latest", "conversation_id", "created_at", "id"),
    )


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id"), nullable=True, index=True
    )
    file_name: Mapped[str] = mapped_column(String(255))
    container_path: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Memory(Base):
    """跨会话的长期记忆,按 owner 隔离:同一个 Key 的所有会话共享一份。"""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content: Mapped[str] = mapped_column(String(500))
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # NULL = 永不过期
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
