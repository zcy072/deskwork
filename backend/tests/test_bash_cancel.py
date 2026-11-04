"""BashSession 的取消 / 超时路径:命令还在沙箱里跑时,取消和超时都必须把 shell 进程清掉。"""

import asyncio
import re

import pytest

from app.sandbox import session_paths
from app.sandbox.bash_handler import BashSession
from app.sandbox.constants import BASH_SENTINEL
from app.sandbox.session_paths import SessionPaths


def sentinel_of(script: str) -> str:
    """包装脚本里本条命令的结束标记(带随机 nonce)。"""
    return re.search(rf"{BASH_SENTINEL}[0-9a-f]+", script).group()


@pytest.mark.parametrize("operation", ["execute", "reset"])
async def test_cancel_during_shell_initialization_cleans_process(tmp_path, monkeypatch, operation):
    client = FakeClient("hang")
    session = _session(tmp_path, client)
    initializing = asyncio.Event()

    async def slow_init():
        initializing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(session, "_init_shell_umask", slow_init)
    task = asyncio.create_task(
        session.execute("sleep 30") if operation == "execute" else session.reset()
    )
    await asyncio.wait_for(initializing.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not session.shell_alive
    assert client.terminated == client.spawned


async def test_cancel_sending_spawn_request_closes_websocket(monkeypatch):
    from app.sandbox import manager_client

    sending = asyncio.Event()

    class Socket:
        closed = False

        async def send(self, _data):
            sending.set()
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    socket = Socket()

    async def connect(*_args, **_kwargs):
        return socket

    monkeypatch.setattr(manager_client.websockets, "connect", connect)
    client = manager_client.ManagerClient("http://manager", "test")
    task = asyncio.create_task(client.spawn("sandbox", ["bwrap"]))
    await sending.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.closed


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = object()  # FakeClient.write_stdin 不会用到
        self.writes: list[str] = []


class FakeClient:
    """假 docker 客户端:记录 spawn / terminate;write_stdin 按 behavior 决定是否回显结果。"""

    def __init__(self, behavior: str = "echo") -> None:
        self.behavior = behavior  # echo = 立刻输出并结束;hang = 一直没输出
        self.spawned: list[FakeProcess] = []
        self.terminated: list[FakeProcess] = []

    async def spawn(self, container, argv, user=None):
        proc = FakeProcess()
        self.spawned.append(proc)
        return proc

    async def write_stdin(self, process, data: str) -> None:
        process.writes.append(data)
        if "umask" in data or self.behavior == "hang":
            return
        process.stdout.feed_data(b"hello\n")
        process.stdout.feed_data(f"{sentinel_of(data)}:0:/workspace/scratchpad\n".encode())

    async def terminate(self, process, grace_sec: float = 5.0) -> None:
        process.returncode = -15
        process.stdout.feed_eof()
        process.stderr.feed_eof()
        self.terminated.append(process)


@pytest.fixture(autouse=True)
def _data_root(tmp_path, monkeypatch):
    # container_root 要求会话目录位于 SANDBOX_DATA_ROOT 之下
    monkeypatch.setattr(session_paths, "SANDBOX_DATA_ROOT", tmp_path)


def _session(tmp_path, client) -> BashSession:
    paths = SessionPaths.create("cancel-test", sessions_root=tmp_path)
    paths.scratchpad.mkdir(parents=True)
    return BashSession(paths, docker_client=client)


async def test_normal_command_returns_output(tmp_path):
    client = FakeClient("echo")
    session = _session(tmp_path, client)
    out, err = await session.execute("echo hello")
    assert not err and "hello" in out
    await session._stop()


async def test_cancel_kills_shell_and_next_command_gets_fresh_one(tmp_path):
    client = FakeClient("hang")
    session = _session(tmp_path, client)
    task = asyncio.create_task(session.execute("sleep 999"))
    await asyncio.sleep(0.6)  # 越过 _ensure_started 的 0.2s 等待,进入读输出循环
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.terminated == client.spawned[:1]
    assert session._process is None

    client.behavior = "echo"
    out, err = await session.execute("echo hello")
    assert not err and "hello" in out
    assert len(client.spawned) == 2  # 取消后起了新的 shell,没有复用被杀的那个
    await session._stop()


async def test_cancel_releases_lock(tmp_path):
    client = FakeClient("hang")
    session = _session(tmp_path, client)
    task = asyncio.create_task(session.execute("sleep 999"))
    await asyncio.sleep(0.6)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not session._lock.locked()


async def test_timeout_returns_error(tmp_path):
    client = FakeClient("hang")
    session = _session(tmp_path, client)
    out, err = await session.execute("sleep 999", timeout=1)
    assert err and "timed out" in out
    await session._stop()


async def test_overlong_line_ends_command_as_output_limit(tmp_path):
    # 超长单行会让 readline() 抛 ValueError:按输出超限立即结束,而不是空等到超时
    client = FakeClient("hang")
    session = _session(tmp_path, client)
    task = asyncio.create_task(session.execute("cat min.json", timeout=30))
    await asyncio.sleep(0.6)
    proc = client.spawned[0]
    proc.stdout = asyncio.StreamReader(limit=16)
    session._reader_tasks[0].cancel()
    session._reader_tasks[0] = asyncio.create_task(session._read_stream(proc.stdout, "stdout"))
    proc.stdout.feed_data(b"x" * 100 + b"\n")
    out, err = await asyncio.wait_for(task, timeout=5)
    assert err and "Output limit exceeded" in out
    assert client.terminated == client.spawned[:1]
    await session._stop()


async def test_shell_exit_mid_command_returns_quickly(tmp_path):
    """set -e 后命令失败等情况让 shell 退出:立即报错并重启,不等到超时。"""
    client = FakeClient("hang")
    session = _session(tmp_path, client)
    task = asyncio.create_task(session.execute("set -e; false", timeout=30))
    await asyncio.sleep(0.6)
    proc = client.spawned[0]
    proc.returncode = 1
    proc.stdout.feed_eof()
    proc.stderr.feed_eof()
    out, err = await asyncio.wait_for(task, timeout=5)
    assert err and "shell exited" in out and "exit code 1" in out
    assert len(client.spawned) == 2  # 已重启
    await session._stop()


async def test_nonzero_exit_code_is_reported(tmp_path):
    client = FakeClient("echo")
    session = _session(tmp_path, client)

    async def write(process, data):
        process.writes.append(data)
        if "umask" not in data:
            process.stdout.feed_data(f"{sentinel_of(data)}:1:/workspace/scratchpad\n".encode())

    client.write_stdin = write
    out, err = await session.execute("false")
    assert not err and out == "Exit code: 1"
    await session._stop()


def test_remote_process_accepts_long_lines():
    from app.sandbox.manager_client import STREAM_LINE_LIMIT

    async def run() -> bytes:
        reader = asyncio.StreamReader(limit=STREAM_LINE_LIMIT)
        reader.feed_data(b"x" * 200_000 + b"\n")
        return await reader.readline()

    assert len(asyncio.run(run())) == 200_001


async def test_validation_uses_cwd_after_previous_command_finishes(tmp_path):
    # 排队等锁期间前一条命令可能 cd 走了;校验必须用拿到锁之后的 cwd,不能用排队前的旧值
    client = FakeClient("hang")
    session = _session(tmp_path, client)
    seen: list[str] = []
    real = session.validator.validate

    def spy(command, current_cwd=None, virtual_root=None):
        seen.append(current_cwd)
        return real(command, current_cwd=current_cwd, virtual_root=virtual_root)

    session.validator.validate = spy
    first = asyncio.create_task(session.execute("sleep 1", timeout=5))
    await asyncio.sleep(0.6)  # first 持有锁
    second = asyncio.create_task(session.execute("ls", timeout=5))
    await asyncio.sleep(0.1)  # second 排队
    session._current_cwd = "/workspace/output"  # first 结束时 shell 已 cd 到这里
    client.behavior = "echo"
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    await second
    assert seen[-1] == "/workspace/output"
    await session._stop()


async def test_remote_output_limit_is_not_reported_as_shell_exit(tmp_path):
    """manager 客户端超出缓冲上限时会断开连接(两个流都 EOF),仍应报输出超限。"""
    client = FakeClient("hang")
    session = _session(tmp_path, client)
    task = asyncio.create_task(session.execute("yes", timeout=30))
    await asyncio.sleep(0.6)
    proc = client.spawned[0]
    proc.output_limit_exceeded = True
    proc.returncode = -1
    proc.stdout.feed_eof()
    proc.stderr.feed_eof()
    out, err = await asyncio.wait_for(task, timeout=5)
    assert err and "Output limit exceeded" in out
    await session._stop()
