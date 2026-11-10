"""数据库引擎与会话工厂。

默认 SQLite(aiosqlite);换 PostgreSQL 只需把 DATABASE_URL 改成
postgresql+asyncpg://... 并安装对应驱动,上层只依赖 repository。
"""

import asyncio

from sqlalchemy import event, inspect, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.db.models import Base, Conversation, Memory
from app.utils import logger

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _add_missing_columns(sync_conn) -> None:
    """create_all 不会给已存在的表加列;这里补上新增的可空列,避免引入迁移框架。"""
    existing_tables = set(inspect(sync_conn).get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {c["name"] for c in inspect(sync_conn).get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            if not col.nullable:
                raise RuntimeError(f"Cannot auto-add NOT NULL column {table.name}.{col.name}")
            ddl = col.type.compile(dialect=sync_conn.dialect)
            sync_conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}"))
            logger.info(f"Added column {table.name}.{col.name}")
        # create_all 也不会给已存在的表补索引
        for idx in table.indexes:
            idx.create(sync_conn, checkfirst=True)


def _set_sqlite_pragmas(dbapi_conn, _record) -> None:
    """WAL 让读写互不阻塞;busy_timeout 避免并发写直接报 database is locked;
    foreign_keys 让 ondelete=CASCADE 在 SQLite 上真正生效。"""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


async def init_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        return
    _engine = create_async_engine(settings.database_url)
    if _engine.dialect.name == "sqlite":
        event.listen(_engine.sync_engine, "connect", _set_sqlite_pragmas)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(_add_missing_columns)
        await conn.run_sync(Base.metadata.create_all)
        from app.db.migrations import migrate_runs

        await conn.run_sync(migrate_runs)
        # 查询直接用 owner == ? 以命中索引,所以旧数据里的 NULL 统一规范成 ""
        for model in (Conversation, Memory):
            await conn.execute(update(model).where(model.owner.is_(None)).values(owner=""))
    logger.info(f"Database ready: {_engine.url.render_as_string(hide_password=True)}")


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine, _session_factory = None, None


def get_session() -> AsyncSession:
    """返回新的 AsyncSession;用法: async with get_session() as s。"""
    if _session_factory is None:
        raise RuntimeError("Database is not initialized")
    return _session_factory()


def is_db_ready() -> bool:
    return _session_factory is not None


async def db_health(timeout: float = 3.0) -> bool:
    """真实执行一次 SELECT 1(探针用):初始化过不代表文件还能读写、连接池还正常。"""
    if _engine is None:
        return False
    try:
        async with asyncio.timeout(timeout), _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("Database health check failed")
        return False
