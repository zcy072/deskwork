"""SQLite checkpointer:落盘、重启后可读回、未初始化时退回内存。"""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agents import checkpointers
from app.config import settings


@pytest.fixture(autouse=True)
async def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    await checkpointers.close_checkpointer()
    yield
    await checkpointers.close_checkpointer()


def test_uninitialized_falls_back_to_memory():
    assert isinstance(checkpointers.get_checkpointer(), InMemorySaver)


async def test_init_creates_sqlite_file_and_is_idempotent(tmp_path):
    saver = await checkpointers.init_checkpointer()
    assert isinstance(saver, AsyncSqliteSaver)
    assert await checkpointers.init_checkpointer() is saver
    assert (tmp_path / "checkpoints.sqlite").exists()
    assert checkpointers.get_checkpointer() is saver


async def test_checkpoint_survives_reopen():
    config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    checkpoint = {
        "v": 4,
        "id": "1ef4f797-8335-6428-8001-8a1503f9b875",
        "ts": "2026-10-25T00:00:00+00:00",
        "channel_values": {"k": "v"},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": None,
    }
    saver = await checkpointers.init_checkpointer()
    await saver.aput(config, checkpoint, {"source": "input", "step": 0}, {})

    await checkpointers.close_checkpointer()  # 模拟进程重启
    saver2 = await checkpointers.init_checkpointer()
    assert saver2 is not saver
    got = await saver2.aget_tuple(config)
    assert got is not None
    assert got.checkpoint["channel_values"] == {"k": "v"}
