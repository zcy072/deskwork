"""/health 用的数据库探针:真实查询,而不只是看是否初始化过。"""

from app.config import settings
from app.db import close_db, db_health, init_db


async def test_db_health_runs_a_real_query(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    assert not await db_health()  # 未初始化
    await init_db()
    try:
        assert await db_health()
    finally:
        await close_db()


async def test_db_health_reports_broken_database(tmp_path, monkeypatch):
    # 目录不存在:引擎能建出来,但第一次连接就失败
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/missing/x.sqlite"
    )
    from sqlalchemy.ext.asyncio import create_async_engine

    import app.db as db

    monkeypatch.setattr(db, "_engine", create_async_engine(settings.DATABASE_URL))
    assert not await db_health()
    await db._engine.dispose()
