"""会话存储配额、清理命令白名单、并发运行上限。"""

import asyncio
import re

import pytest

from app.config import settings
from app.sandbox import session_paths
from app.sandbox.bash_handler import BashSession
from app.sandbox.constants import BASH_SENTINEL
from app.sandbox.quota import MB, QuotaExceededError, dir_size, is_cleanup_command
from app.sandbox.session_paths import SessionPaths
from app.services import chat_operations as ops
from app.services import file_operations
from app.services.file_operations import InMemoryUpload


def test_dir_size_counts_files_and_skips_symlinks(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 100)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"y" * 50)
    (tmp_path / "link").symlink_to(tmp_path / "a")
    assert dir_size(tmp_path) == 150


def test_dir_size_missing_dir_is_zero(tmp_path):
    assert dir_size(tmp_path / "nope") == 0


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /workspace/output/big",
        "rm a && echo done",
        "du -sh *",
        "ls -la && du -sh .",
        "cd output; rm a b",
    ],
)
def test_cleanup_commands_allowed(command):
    assert is_cleanup_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "python gen.py",
        "rm a && python gen.py",
        "ls > out.txt",
        "rm $(echo a)",
        "cat big | tee more",
        r"find . -exec cp {} x \;",
        "find . -fprint out",
        "",
    ],
)
def test_other_commands_rejected_when_over_quota(command):
    assert not is_cleanup_command(command)


def sentinel_of(script: str) -> str:
    """包装脚本里本条命令的结束标记(带随机 nonce)。"""
    return re.search(rf"{BASH_SENTINEL}[0-9a-f]+", script).group()


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = object()
        self.writes = []


class FakeClient:
    def __init__(self):
        self.proc = None
        self.scripts = []

    async def spawn(self, container, argv, user=None):
        self.proc = FakeProcess()
        return self.proc

    async def write_stdin(self, process, data):
        self.scripts.append(data)
        if "umask" in data:
            return
        process.stdout.feed_data(b"ok\n")
        process.stdout.feed_data(f"{sentinel_of(data)}:0:/workspace/scratchpad\n".encode())

    async def terminate(self, process, grace_sec=5.0):
        process.returncode = -15
        process.stdout.feed_eof()
        process.stderr.feed_eof()


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(session_paths, "SANDBOX_DATA_ROOT", tmp_path)
    paths = SessionPaths.create("quota-test", sessions_root=tmp_path)
    paths.scratchpad.mkdir(parents=True)
    client = FakeClient()
    return BashSession(paths, docker_client=client), paths, client


async def test_over_quota_blocks_normal_commands_but_allows_cleanup(session, monkeypatch):
    sess, paths, _ = session
    monkeypatch.setattr(settings, "SESSION_MAX_STORAGE_MB", 1)
    (paths.scratchpad / "big.bin").write_bytes(b"0" * (2 * MB))

    out, is_error = await sess.execute("python gen.py")
    assert is_error and "quota exceeded" in out

    out, is_error = await sess.execute("rm big.bin")
    assert not is_error
    assert "Warning" in out  # 命令执行时仍超限,执行后提示


async def test_under_quota_runs_normally(session, monkeypatch):
    sess, paths, _ = session
    monkeypatch.setattr(settings, "SESSION_MAX_STORAGE_MB", 1)
    (paths.scratchpad / "small").write_bytes(b"0" * 10)
    out, is_error = await sess.execute("echo hi")
    assert not is_error and "Warning" not in out


async def test_zero_means_unlimited(session, monkeypatch):
    sess, paths, _ = session
    monkeypatch.setattr(settings, "SESSION_MAX_STORAGE_MB", 0)
    (paths.scratchpad / "big.bin").write_bytes(b"0" * (2 * MB))
    _, is_error = await sess.execute("python gen.py")
    assert not is_error


async def test_file_size_ulimit_set_at_shell_start(session, monkeypatch):
    sess, _, client = session
    monkeypatch.setattr(settings, "SESSION_MAX_FILE_MB", 7)
    await sess.execute("echo hi")
    assert any("ulimit -f 7168" in s for s in client.scripts)


async def test_upload_rejected_when_it_would_exceed_quota(tmp_path, monkeypatch):
    class Sess:
        class paths:
            session_root = tmp_path

    class Mgr:
        async def get_or_create(self, sid):
            return Sess()

    monkeypatch.setattr(file_operations, "CONTAINER_MANAGER", Mgr())
    monkeypatch.setattr(settings, "SESSION_MAX_STORAGE_MB", 1)
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "existing").write_bytes(b"0" * (MB // 2))
    with pytest.raises(QuotaExceededError):
        await file_operations.upload_session_files(
            "s", [InMemoryUpload("a.bin", b"1" * (MB // 2 + 1))]
        )


async def test_too_many_concurrent_runs_rejected(monkeypatch):
    monkeypatch.setattr(settings, "MAX_CONCURRENT_RUNS", 2)
    monkeypatch.setitem(ops._active_agent_tasks, "a", object())
    monkeypatch.setitem(ops._active_agent_tasks, "b", object())
    with pytest.raises(ops.TooManyRunsError):
        await ops.start_chat("c", "hi", [])


async def test_run_limit_does_not_block_session_already_counted(monkeypatch):
    """已占用名额的会话再发消息应走原来的 409 逻辑,而不是 429。"""
    monkeypatch.setattr(settings, "MAX_CONCURRENT_RUNS", 1)
    monkeypatch.setitem(ops._active_agent_tasks, "a", object())

    async def running(_sid):
        return "running"

    monkeypatch.setattr(ops, "get_thread_status", running)
    with pytest.raises(ops.ActiveRunError):
        await ops.start_chat("a", "hi", [])


def test_snapshots_dir_not_counted_in_usage(tmp_path):
    from app.sandbox.quota import session_usage

    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "a").write_bytes(b"0" * 10)
    (tmp_path / "snapshots" / "x").mkdir(parents=True)
    (tmp_path / "snapshots" / "x" / "big").write_bytes(b"0" * 1000)
    assert session_usage(tmp_path) == 10


async def test_usage_measured_after_command_is_reused_before_next(session, monkeypatch):
    """连续执行命令时,上一条命令结束时的测量结果直接复用,不再每条遍历两次目录。"""
    sess, _, _ = session
    monkeypatch.setattr(settings, "SESSION_MAX_STORAGE_MB", 1)
    from app.sandbox import bash_handler

    calls = []
    real = bash_handler.session_usage
    monkeypatch.setattr(bash_handler, "session_usage", lambda root: calls.append(1) or real(root))
    await sess.execute("echo a")
    await sess.execute("echo b")
    assert len(calls) == 3  # 首条命令前 1 次 + 每条命令后各 1 次
    sess._usage = (sess._usage[0] - bash_handler.USAGE_CACHE_SEC - 1, sess._usage[1])
    await sess.execute("echo c")
    assert len(calls) == 5  # 过期后命令前重新测量
