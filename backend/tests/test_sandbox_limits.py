"""输出上限与大文件写入。"""

import asyncio

import pytest

from app.sandbox import manager_client, session_paths
from app.sandbox.bash_handler import BashSession
from app.sandbox.manager_client import RemoteProcess
from app.sandbox.session_paths import SessionPaths


@pytest.fixture(autouse=True)
def _data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(session_paths, "SANDBOX_DATA_ROOT", tmp_path)


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader(limit=1024 * 1024)
        self.stderr = asyncio.StreamReader(limit=1024 * 1024)


class FloodClient:
    """write_stdin 之后持续往 stdout 灌数据,模拟 `yes`。"""

    def __init__(self) -> None:
        self.terminated = 0
        self.spawned = 0
        self._tasks: list[asyncio.Task] = []

    async def spawn(self, container, argv):
        self.spawned += 1
        return FakeProcess()

    async def write_stdin(self, process, data: str) -> None:
        if "umask" in data:
            return

        async def flood():
            line = b"y" * 1023 + b"\n"
            while True:
                process.stdout.feed_data(line * 256)
                await asyncio.sleep(0)

        self._tasks.append(asyncio.create_task(flood()))

    async def terminate(self, process, grace_sec: float = 5.0) -> None:
        self.terminated += 1
        for t in self._tasks:
            t.cancel()
        process.returncode = -15
        process.stdout.feed_eof()


async def test_output_flood_is_cut_off(tmp_path, monkeypatch):
    monkeypatch.setattr("app.sandbox.bash_handler.MAX_COMMAND_OUTPUT_BYTES", 2 * 1024 * 1024)
    paths = SessionPaths.create("flood", sessions_root=tmp_path)
    paths.scratchpad.mkdir(parents=True)
    client = FloodClient()
    session = BashSession(paths, manager_client=client)
    out, is_error = await asyncio.wait_for(session.execute("yes", timeout=30), timeout=20)
    assert is_error
    assert "Output limit exceeded" in out
    assert client.terminated >= 1
    await session.close()


class FakeWS:
    def __init__(self, frames):
        self.frames = frames
        self.closed = False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for f in self.frames:
            yield f
            await asyncio.sleep(0)

    async def close(self):
        self.closed = True


async def test_remote_process_closes_on_unread_buffer(monkeypatch):
    monkeypatch.setattr(manager_client, "MAX_BUFFERED_BYTES", 1024 * 1024)
    chunk = bytes([manager_client.CH_STDOUT]) + b"x" * 65536
    ws = FakeWS([chunk] * 100)
    proc = RemoteProcess(ws)
    await asyncio.wait_for(proc._closed.wait(), timeout=5)
    assert proc.output_limit_exceeded
    assert ws.closed
    assert proc.returncode == -1


async def test_remote_process_normal_output_unaffected():
    chunk = bytes([manager_client.CH_STDOUT]) + b"hi\n"
    proc = RemoteProcess(FakeWS([chunk, '{"exit": 0}']))
    await asyncio.wait_for(proc._closed.wait(), timeout=5)
    assert not proc.output_limit_exceeded
    assert await proc.stdout.readline() == b"hi\n"
    assert proc.returncode == 0


async def test_remote_process_reports_manager_rejection():
    ws = FakeWS([])
    ws.close_code = 4429
    proc = RemoteProcess(ws)
    await asyncio.wait_for(proc._closed.wait(), timeout=5)
    assert proc.returncode == -1
    assert "MANAGER_MAX_SPAWN" in proc.close_reason


async def test_session_start_error_names_manager_rejection(tmp_path):
    class RejectingClient:
        async def spawn(self, container, argv):
            ws = FakeWS([])
            ws.close_code = 4429
            proc = RemoteProcess(ws)
            await proc._closed.wait()
            return proc

    paths = SessionPaths.create("rejected", sessions_root=tmp_path)
    for d in paths.subdirs:
        d.mkdir(parents=True)
    session = BashSession(paths, manager_client=RejectingClient())
    with pytest.raises(RuntimeError, match="concurrent shell limit"):
        await session._ensure_started()


async def test_large_file_write_via_host_mount(tmp_path):
    paths = SessionPaths.create("bigwrite", sessions_root=tmp_path)
    for d in paths.subdirs:
        d.mkdir(parents=True)
    session = BashSession(paths, manager_client=object())
    content = "é" * 250_000  # 500KB utf-8
    target = f"{paths.container_root}/output/sub/dir/big.txt"
    await session._write_container_content(target, content, mkdir_parents=True)
    assert (paths.output / "sub/dir/big.txt").read_text(encoding="utf-8") == content
    await session._write_container_content(f"{paths.container_root}/output/sub/dir/big.txt", "x")
    assert (paths.output / "sub/dir/big.txt").read_text() == "x"


async def test_write_refuses_symlink_escape(tmp_path):
    from app.sandbox.errors import ContainerFileSystemError

    paths = SessionPaths.create("symw", sessions_root=tmp_path)
    for d in paths.subdirs:
        d.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (paths.output / "link").symlink_to(outside)
    session = BashSession(paths, manager_client=object())
    with pytest.raises(ContainerFileSystemError):
        await session._write_container_content(f"{paths.container_root}/output/link/x.txt", "x")
    assert not (outside / "x.txt").exists()
    (paths.output / "flink").symlink_to(outside / "t.txt")
    with pytest.raises(ContainerFileSystemError):
        await session._write_container_content(f"{paths.container_root}/output/flink", "x")
    assert not (outside / "t.txt").exists()


async def test_missing_manager_url_fails_with_clear_error(monkeypatch):
    monkeypatch.delenv("SANDBOX_MANAGER_URL", raising=False)
    client = manager_client.ManagerClient()
    with pytest.raises(RuntimeError, match="SANDBOX_MANAGER_URL"):
        await client.spawn("c", ["bash"])
    with pytest.raises(RuntimeError, match="SANDBOX_MANAGER_URL"):
        await client.run_exec("c", "true")
