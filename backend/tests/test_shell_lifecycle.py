"""bash 会话生命周期:结束标记不可伪造、并发首次创建、空闲 shell 回收。"""

import asyncio
import re

import pytest

from app.config import settings
from app.sandbox import session_paths
from app.sandbox.bash_handler import BashSessionsManager
from app.sandbox.constants import BASH_SENTINEL


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()


class FakeClient:
    async def aclose(self):
        pass

    """write_stdin 按 reply 生成输出;reply 拿到包装脚本,可以取出本条命令的结束标记。"""

    def __init__(self) -> None:
        self.provisioned: list[str] = []
        self.spawned: list[FakeProcess] = []
        self.terminated: list[FakeProcess] = []
        self.reply = lambda sentinel: [f"ok\n{sentinel}:0:/workspace/scratchpad\n"]

    async def provision_session(self, container, path):
        await asyncio.sleep(0.01)  # 让出事件循环,暴露创建过程中的竞态
        self.provisioned.append(path)

    async def spawn(self, container, argv):
        proc = FakeProcess()
        self.spawned.append(proc)
        return proc

    async def write_stdin(self, process, data: str) -> None:
        if "umask" in data:
            return
        sentinel = re.search(rf"{BASH_SENTINEL}[0-9a-f]+", data).group()
        for chunk in self.reply(sentinel):
            process.stdout.feed_data(chunk.encode())

    async def terminate(self, process, grace_sec: float = 5.0) -> None:
        process.returncode = -15
        process.stdout.feed_eof()
        process.stderr.feed_eof()
        self.terminated.append(process)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(session_paths, "SANDBOX_DATA_ROOT", tmp_path)
    monkeypatch.setattr(settings, "SESSION_MAX_STORAGE_MB", 0)
    monkeypatch.setattr(settings, "SESSION_MAX_FILE_MB", 0)
    mgr = BashSessionsManager(host_sessions_root=tmp_path / "sessions")
    mgr._manager_client = FakeClient()
    return mgr


async def test_concurrent_get_or_create_returns_same_session(manager):
    a, b = await asyncio.gather(manager.get_or_create("race"), manager.get_or_create("race"))
    assert a is b
    assert len(manager._manager_client.provisioned) == 1


async def test_spoofed_sentinel_in_output_does_not_end_command(manager):
    """输出里出现固定前缀的假标记(例如 cat 了含它的文件)只是普通输出。"""
    client = manager._manager_client
    client.reply = lambda sentinel: [
        f"{BASH_SENTINEL}:0:/workspace/output\n",
        "after\n",
        f"{sentinel}:0:/workspace/scratchpad\n",
    ]
    session = await manager.get_or_create("spoof")
    out, err = await session.execute("cat notes.txt")
    assert not err
    assert f"{BASH_SENTINEL}:0:/workspace/output" in out and "after" in out
    assert session._current_cwd == "/workspace/scratchpad"
    await manager.close_all()


async def test_each_command_gets_a_fresh_sentinel(manager):
    session = await manager.get_or_create("fresh")
    first = session._wrap_command("ls")
    second = session._wrap_command("ls")
    assert first != second


async def test_idle_shells_are_closed_and_restart_on_next_command(manager, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_SHELL_IDLE_SEC", 60)
    monkeypatch.setattr(settings, "SANDBOX_MAX_SHELLS", 0)
    client = manager._manager_client
    old = await manager.get_or_create("old")
    await old.execute("ls")
    old.last_used -= 120  # 已空闲两分钟

    recent = await manager.get_or_create("recent")
    await recent.execute("ls")

    assert not old.shell_alive and recent.shell_alive
    assert client.terminated == client.spawned[:1]

    out, err = await (await manager.get_or_create("old")).execute("ls")
    assert not err and "ok" in out and old.shell_alive  # 自动重启
    await manager.close_all()


async def test_shell_count_is_capped_by_least_recently_used(manager, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_SHELL_IDLE_SEC", 0)
    monkeypatch.setattr(settings, "SANDBOX_MAX_SHELLS", 2)
    sessions = []
    for sid in ("s1", "s2", "s3"):
        s = await manager.get_or_create(sid)
        await s.execute("ls")
        sessions.append(s)
    assert [s.shell_alive for s in sessions] == [False, True, True]
    await manager.close_all()
    assert not any(s.shell_alive for s in sessions)


async def test_busy_shell_is_not_reaped(manager, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_SHELL_IDLE_SEC", 60)
    monkeypatch.setattr(settings, "SANDBOX_MAX_SHELLS", 0)
    client = manager._manager_client
    client.reply = lambda sentinel: []  # 命令一直不结束
    busy = await manager.get_or_create("busy")
    task = asyncio.create_task(busy.execute("sleep 999", timeout=30))
    await asyncio.sleep(0.4)
    busy.last_used -= 120
    await manager.get_or_create("other")
    assert busy.shell_alive
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await manager.close_all()
