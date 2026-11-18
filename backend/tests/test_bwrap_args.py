"""bwrap 隔离参数和发往 sandbox-manager 的用户:重构时容易被悄悄改掉的安全约束。"""

import json

import pytest

from app.sandbox import bwrap, manager_client, session_paths
from app.sandbox.session_paths import SessionPaths


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(session_paths, "SANDBOX_DATA_ROOT", tmp_path)
    return SessionPaths.create("s1", sessions_root=tmp_path / "sessions")


def _binds(argv: list[str]) -> dict[str, tuple[str, str]]:
    """挂载点 -> (flag, 源路径)。"""
    out = {}
    for i, a in enumerate(argv):
        if a in ("--bind", "--ro-bind"):
            out[argv[i + 2]] = (a, argv[i + 1])
    return out


@pytest.mark.parametrize("kind", ["shell", "oneshot"])
def test_isolation_flags_and_mounts(paths, kind):
    argv = bwrap.shell_argv(paths) if kind == "shell" else bwrap.oneshot_argv(paths, "true")
    assert argv[0] == "bwrap"
    for flag in ("--unshare-pid", "--die-with-parent", "--new-session", "--clearenv"):
        assert flag in argv
    binds = _binds(argv)
    assert binds["/workspace/uploads"][0] == "--ro-bind"
    assert binds["/workspace/skills"][0] == "--ro-bind"
    assert binds["/workspace/output"] == ("--bind", f"{paths.container_root}/output")
    assert binds["/workspace/scratchpad"] == ("--bind", f"{paths.container_root}/scratchpad")
    assert {"/usr", "/lib", "/lib64", "/etc"} <= {
        m for m, (f, _) in binds.items() if f == "--ro-bind"
    }
    # 只挂本会话目录,不挂整个 sessions/ 或 snapshots/
    session_sources = [src for _, src in binds.values() if "/sessions/" in src]
    assert all(src.startswith(f"{paths.container_root}/") for src in session_sources)
    assert not any(src.endswith("/snapshots") for src in session_sources)


def test_shell_and_oneshot_differ_only_in_home_and_program(paths):
    shell = bwrap.shell_argv(paths)
    oneshot = bwrap.oneshot_argv(paths, "echo hi")
    assert shell[-3:] == ["bash", "--noprofile", "--norc"]
    assert oneshot[-3:] == ["bash", "-c", "echo hi"]
    home = shell.index("HOME") + 1
    assert shell[home] == paths.virtual_scratchpad and oneshot[home] == "/tmp"
    assert shell[:home] == oneshot[:home] and shell[home + 1 : -3] == oneshot[home + 1 : -3]


async def test_spawn_always_requests_agent_user(monkeypatch):
    sent = []

    class Socket:
        async def send(self, data):
            sent.append(data)

        async def close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def connect(*_a, **_k):
        return Socket()

    monkeypatch.setattr(manager_client.websockets, "connect", connect)
    client = manager_client.ManagerClient("http://manager", "t")
    proc = await client.spawn("sandbox", ["bwrap"])
    await proc._closed.wait()
    assert json.loads(sent[0])["user"] == "agent"


def test_cap_timeout():
    cap = manager_client.COMMAND_TIMEOUT_SEC
    assert manager_client.cap_timeout(None) == cap
    assert manager_client.cap_timeout(cap + 100) == cap
    assert manager_client.cap_timeout(5) == 5
