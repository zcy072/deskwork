"""R12/R14 回归:沙箱停止必须被确认,否则不放行工作区锁和会话占用。"""

import asyncio
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage

from app.agents.context import RunContext
from app.agents.state import DeepAgentState
from app.db import runs
from app.runtime.sessions import sessions
from app.sandbox import bash_handler, session_paths
from app.sandbox.bash_handler import BashSession
from app.sandbox.errors import SandboxStopUnconfirmed
from app.sandbox.manager_client import RemoteProcess
from app.sandbox.session_paths import SessionPaths
from app.services import chat_operations as ops
from app.tools import file_tools
from tests.test_ask_user import ScriptedModel
from tests.test_bash_cancel import FakeClient
from tests.test_run_recovery import runtime  # noqa: F401


def _paths(tmp_path, monkeypatch, sid="s"):
    monkeypatch.setattr(session_paths, "SANDBOX_DATA_ROOT", tmp_path)
    paths = SessionPaths.create(sid, tmp_path / "sessions")
    paths.scratchpad.mkdir(parents=True, exist_ok=True)
    return paths


async def test_second_bash_cancel_keeps_lock_until_process_stops(tmp_path, monkeypatch):
    command, stopping, release, stopped = (asyncio.Event() for _ in range(4))

    class Client(FakeClient):
        async def write_stdin(self, process, data):
            await super().write_stdin(process, data)
            if "sleep 10" in data:
                command.set()

        async def terminate(self, process, grace_sec=5):
            stopping.set()
            await release.wait()
            await super().terminate(process, grace_sec)
            stopped.set()

    client = Client("hang")
    session = BashSession(_paths(tmp_path, monkeypatch), manager_client=client)
    task = asyncio.create_task(session.execute("sleep 10"))
    await asyncio.wait_for(command.wait(), 2)
    task.cancel()
    await asyncio.wait_for(stopping.wait(), 2)
    task.cancel()
    await asyncio.sleep(0.05)
    # 第二次取消不能让命令提前退出:进程还活着,锁必须仍被持有
    assert not task.done()
    assert session.workspace_lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.spawned[0].returncode is not None
    assert not session.workspace_lock.locked()


class _Socket:
    close_code = None

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.incoming.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def send(self, frame):
        self.sent.append(frame)

    async def close(self):
        await self.incoming.put(None)


async def test_remote_termination_without_exit_ack_is_not_success():
    socket = _Socket()
    process = RemoteProcess(socket)
    with pytest.raises(SandboxStopUnconfirmed):
        await process.terminate(grace_sec=0.01)
    assert socket.sent == ["terminate"]
    await socket.close()
    await process._task
    assert not process.stopped


@pytest.mark.parametrize(
    ("receipt", "confirmed"),
    [
        ('{"exit": 0, "stopped": true}', True),
        ('{"exit": -1, "stopped": false}', False),
        ('{"exit": 0}', False),
    ],
)
async def test_remote_termination_requires_stopped_receipt(receipt, confirmed):
    socket = _Socket()
    process = RemoteProcess(socket)
    waiter = asyncio.create_task(process.terminate(grace_sec=1))
    await asyncio.sleep(0)
    await socket.incoming.put(receipt)
    await socket.incoming.put(None)
    if confirmed:
        await waiter
    else:
        with pytest.raises(SandboxStopUnconfirmed):
            await waiter


async def test_unconfirmed_stop_blocks_further_commands(tmp_path, monkeypatch):
    class Client(FakeClient):
        async def terminate(self, process, grace_sec=5):
            raise SandboxStopUnconfirmed("no receipt")

    session = BashSession(_paths(tmp_path, monkeypatch), manager_client=Client("hang"))
    with pytest.raises(SandboxStopUnconfirmed):
        await session.execute("sleep 10", 0.05)
    assert session.stop_state.unconfirmed
    assert not session.workspace_lock.locked()
    with pytest.raises(SandboxStopUnconfirmed):
        await session.execute("echo hi")


async def test_run_retains_gate_when_remote_cleanup_fails(runtime, monkeypatch):  # noqa: F811
    monkeypatch.setattr(session_paths, "SANDBOX_DATA_ROOT", runtime.root)
    paths = SessionPaths.create("s")
    paths.scratchpad.mkdir(parents=True, exist_ok=True)
    began = asyncio.Event()

    class Client(FakeClient):
        async def write_stdin(self, process, data):
            await super().write_stdin(process, data)
            if "sleep 10" in data:
                began.set()

        async def terminate(self, process, grace_sec=5):
            raise RuntimeError("stop not confirmed")

    client = Client("hang")
    shell = BashSession(paths, manager_client=client)
    ops.CONTAINER_MANAGER.sessions["s"] = shell

    class Manager:
        async def bash_tool_helper(self, sid, command, timeout, restart):
            return await shell.execute(command, timeout)

    monkeypatch.setattr(file_tools, "CONTAINER_MANAGER", Manager())
    model = ScriptedModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "shell",
                        "name": "bash_tool",
                        "args": {"description": "work", "command": "sleep 10"},
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = create_agent(
        model=model,
        tools=[file_tools.bash_tool],
        checkpointer=runtime.saver,
        state_schema=DeepAgentState,
        context_schema=RunContext,
    )
    monkeypatch.setattr(ops, "get_agent", lambda: graph)
    pid = await ops.start_chat("s", "work", [])
    await asyncio.wait_for(began.wait(), 3)
    status, _ = await ops.cancel_chat("s")
    handle = sessions.get("s")
    assert handle is not None and handle.phase == "recovery_required"
    assert status == "error"
    assert (await runs.get(pid)).status == "error"
    with pytest.raises(ops.ActiveRunError):
        await ops.start_chat("s", "again", [])


async def test_cancel_before_entry_finalization_survives_cancelled_waiter(
    runtime,  # noqa: F811
    monkeypatch,
):
    entered, finish = asyncio.Event(), asyncio.Event()
    real_save = runs.save

    async def delayed_save(*args, **kwargs):
        entered.set()
        await finish.wait()
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(runs, "save", delayed_save)
    pid = await ops.start_chat("s", "q", [])
    handle = sessions.get("s")
    handle.task.cancel()
    waiter = asyncio.create_task(ops.cancel_chat("s"))
    await asyncio.wait_for(entered.wait(), 2)
    waiter.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert (await runs.get(pid)).status == "cancelled"
    assert sessions.get("s") is None


async def test_shell_deadline_crossed_before_queue_wait_is_still_timeout(tmp_path, monkeypatch):
    values = iter([0.0, 0.5, 1.1])
    session = BashSession(_paths(tmp_path, monkeypatch, "deadline"), manager_client=FakeClient())
    monkeypatch.setattr(bash_handler, "time", SimpleNamespace(monotonic=lambda: next(values, 2.0)))
    result = await session._read_until_sentinel(1)
    assert result[1] is True
