"""LangGraph checkpointer 的生命周期管理。

生产环境在 FastAPI lifespan 中用 SQLite 持久化,重启后对话上下文不丢;
未初始化时(单元测试等)退回内存实现,保持 import 无副作用。
"""

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.config import settings
from app.utils import logger

_saver: BaseCheckpointSaver | None = None
_conn: aiosqlite.Connection | None = None


async def init_checkpointer() -> BaseCheckpointSaver:
    global _saver, _conn
    if _saver is None:
        path = settings.checkpoint_db_path
        _conn = await aiosqlite.connect(path)
        _saver = AsyncSqliteSaver(_conn)
        await _saver.setup()
        logger.info(f"Checkpointer ready: SQLite at {path}")
    return _saver


async def close_checkpointer() -> None:
    global _saver, _conn
    if _conn is not None:
        await _conn.close()
    _saver, _conn = None, None


def get_checkpointer() -> BaseCheckpointSaver:
    global _saver
    if _saver is None:
        logger.warning("Checkpointer not initialized; falling back to in-memory saver")
        _saver = InMemorySaver()
    return _saver
