"""SQLite 连接必须开启 WAL / busy_timeout / foreign_keys。"""

from sqlalchemy import text

from app.config import settings
from app.db import close_db, get_session, init_db


async def test_sqlite_pragmas_are_set(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    await init_db()
    try:
        async with get_session() as s:
            assert (await s.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
            assert (await s.execute(text("PRAGMA busy_timeout"))).scalar() == 5000
            assert (await s.execute(text("PRAGMA foreign_keys"))).scalar() == 1
    finally:
        await close_db()
