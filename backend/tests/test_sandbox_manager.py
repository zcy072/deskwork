"""sandbox-manager 的访问控制:无需 docker,只验证请求在到达 docker 前被拒绝。"""

import importlib.util
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SANDBOX_MANAGER_TOKEN", "t0ken")
_MAIN = Path(__file__).resolve().parents[2] / "sandbox" / "manager" / "main.py"
_spec = importlib.util.spec_from_file_location("sandbox_manager_main", _MAIN)
manager = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(manager)

AUTH = {"Authorization": f"Bearer {manager.TOKEN}"}
client = TestClient(manager.app)


def test_health_is_public():
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("path", ["/provision", "/remove", "/exec"])
def test_requires_token(path):
    body = {"container": "sandbox", "path": "/sandbox_data/sessions/a", "script": "id"}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers={"Authorization": "Bearer x"}).status_code == 401


def test_rejects_other_containers():
    body = {"container": "deskwork-redis", "script": "id"}
    assert client.post("/exec", json=body, headers=AUTH).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/sandbox_data/sessions",
        "/sandbox_data/sessions/../default_skills",
        "/sandbox_data/default_skills",
    ],
)
def test_rejects_paths_outside_sessions(path):
    for endpoint in ("/remove", "/provision"):
        body = {"container": "sandbox", "path": path}
        assert client.post(endpoint, json=body, headers=AUTH).status_code == 400


def test_spawn_requires_token_and_bwrap():
    with pytest.raises(Exception):  # noqa: B017 - 关闭码 4401 会让握手后立即断开
        with client.websocket_connect("/spawn") as ws:
            ws.receive_text()
    headers = AUTH
    with client.websocket_connect("/spawn", headers=headers) as ws:
        ws.send_text('{"container":"sandbox","user":"agent","argv":["bash","-c","id"]}')
        with pytest.raises(Exception):  # noqa: B017
            ws.receive_text()


@pytest.mark.parametrize("frame", ["not json", "[]", "null"])
def test_spawn_rejects_malformed_first_frame(frame):
    with client.websocket_connect("/spawn", headers=AUTH) as ws:
        ws.send_text(frame)
        with pytest.raises(Exception):  # noqa: B017 - 关闭码 4400
            ws.receive_text()


# ---------- 限流与审计 ----------


def test_limiter_rejects_over_limit_and_recovers():
    lim = manager.Limiter(2)
    assert lim.acquire() and lim.acquire()
    assert not lim.acquire()
    lim.release()
    assert lim.acquire()
    for _ in range(5):  # 多释放不会变成负数
        lim.release()
    assert lim.active == 0


def test_exec_over_limit_returns_429_without_running_docker(monkeypatch):
    called = []

    async def boom(*a, **kw):
        called.append(1)
        raise AssertionError("docker 不应被调用")

    monkeypatch.setattr(manager.asyncio, "create_subprocess_exec", boom)
    monkeypatch.setattr(manager, "exec_limiter", manager.Limiter(0))
    r = client.post("/exec", json={"container": "sandbox", "script": "id"}, headers=AUTH)
    assert r.status_code == 429 and not called


def test_exec_releases_slot_after_success_and_failure(monkeypatch):
    lim = manager.Limiter(1)
    monkeypatch.setattr(manager, "exec_limiter", lim)

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def fake(*a, **kw):
        return Proc()

    monkeypatch.setattr(manager.asyncio, "create_subprocess_exec", fake)
    body = {"container": "sandbox", "script": "id"}
    assert client.post("/exec", json=body, headers=AUTH).status_code == 200
    assert lim.active == 0

    async def broken(*a, **kw):
        raise OSError("no docker")

    monkeypatch.setattr(manager.asyncio, "create_subprocess_exec", broken)
    with pytest.raises(OSError):
        client.post("/exec", json=body, headers=AUTH)
    assert lim.active == 0  # 异常路径也释放


def test_exec_audit_records_hash_not_script(monkeypatch, caplog):
    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake(*a, **kw):
        return Proc()

    monkeypatch.setattr(manager.asyncio, "create_subprocess_exec", fake)
    secret = "echo TOP-SECRET-VALUE"
    caplog.set_level("INFO", logger="sandbox_manager.audit")
    manager.audit_log.propagate = True
    try:
        r = client.post("/exec", json={"container": "sandbox", "script": secret}, headers=AUTH)
    finally:
        manager.audit_log.propagate = False
    assert r.status_code == 200
    text = " ".join(rec.getMessage() for rec in caplog.records)
    assert '"op": "exec"' in text and manager.digest(secret) in text
    assert "TOP-SECRET-VALUE" not in text


def test_session_of():
    assert manager.session_of("/sandbox_data/sessions/abc/output/a.docx") == "abc"
    assert manager.session_of("/etc/passwd") == ""


def test_spawn_over_limit_closes_with_4429(monkeypatch):
    monkeypatch.setattr(manager, "spawn_limiter", manager.Limiter(0))
    with client.websocket_connect("/spawn", headers=AUTH) as ws:
        with pytest.raises(Exception) as exc:
            ws.receive_text()
    assert "4429" in repr(exc.value) or getattr(exc.value, "code", None) == 4429


def test_exec_script_is_wrapped_in_container_timeout(monkeypatch):
    import asyncio

    seen = {}

    async def fake_create(*args, **kwargs):
        seen["args"] = args

        class P:
            returncode = 0

            async def communicate(self):
                return b"", b""

        return P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    resp = client.post(
        "/exec", json={"container": "sandbox", "script": "id", "timeout": -5}, headers=AUTH
    )
    assert resp.status_code == 200
    args = seen["args"]
    i = args.index("timeout")
    assert args[i : i + 3] == ("timeout", "-s", "KILL") and args[i + 3] == "1"


def _exit_code(code):
    from types import SimpleNamespace

    async def wait():
        return code

    return SimpleNamespace(wait=wait)


@pytest.mark.parametrize("disconnect", [False, True])
def test_spawn_kills_only_its_own_bwrap(monkeypatch, disconnect):
    """结束时按本次的唯一标记杀 bwrap,不按会话目录匹配(会误杀同会话并行的 bwrap)。"""
    import asyncio

    calls = []

    class FakeStream:
        async def read(self, _n):
            return b""

    class FakeStdin:
        def write(self, _data):
            raise BrokenPipeError

        async def drain(self):
            pass

    class FakeProc:
        def __init__(self):
            self.returncode = None
            self.stdin, self.stdout, self.stderr = FakeStdin(), FakeStream(), FakeStream()
            self._done = asyncio.Event()

        async def wait(self):
            await self._done.wait()
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self._done.set()

        def kill(self):
            self.terminate()

    async def fake_exec(*args, **_kw):
        calls.append(args)
        if "-i" in args:
            return FakeProc()
        # pkill 杀掉了(0);pgrep 找不到(1)表示确认已停止
        return _exit_code(0 if "pkill" in args else 1)

    monkeypatch.setattr(manager.asyncio, "create_subprocess_exec", fake_exec)
    spec = {
        "container": "sandbox",
        "user": "agent",
        "argv": _shell_argv("s1"),
    }
    with client.websocket_connect("/spawn", headers=AUTH) as ws:
        ws.send_text(json.dumps(spec))
        if disconnect:
            ws.close()
        else:
            ws.send_bytes(b"\x00echo hi\n")  # stdin 已断:应当照常清理而不是抛异常
            assert json.loads(ws.receive_text()) == {"exit": -15, "stopped": True}
    spawned, killer = calls[0], calls[1]
    # bwrap 由一层 sh 托着(不直接当 docker exec 的进程,见 manager 注释)
    i = spawned.index("bwrap")
    assert spawned[:i] == (
        "docker",
        "exec",
        "-i",
        "-u",
        manager.AGENT_USER,
        manager.CONTAINER,
        "sh",
        "-c",
        manager.SPAWN_WRAPPER,
        "sh",
    )
    assert "exec" not in manager.SPAWN_WRAPPER
    bwrap = spawned[i:]
    assert bwrap[1:3] == ("--setenv", "DESKWORK_SPAWN_ID")
    assert bwrap[4:] == tuple(spec["argv"][1:])
    assert killer[-1] == f"^bwrap --setenv DESKWORK_SPAWN_ID {bwrap[3]} "
    assert "s1" not in killer[-1]


@pytest.mark.parametrize(
    ("pkill", "pgrep", "stopped"),
    [(0, 1, True), (1, 1, True), (2, 1, False), (0, 0, False)],
)
def test_spawn_receipt_reports_confirmed_stop(monkeypatch, pkill, pgrep, stopped):
    """R12:pkill 失败或 pgrep 仍找得到进程时,回执不能声称已停止。"""
    import asyncio

    monkeypatch.setattr(manager, "STOP_CONFIRM_POLLS", 2)
    monkeypatch.setattr(manager, "STOP_CONFIRM_INTERVAL", 0)

    class FakeStream:
        async def read(self, _n):
            return b""

    class FakeProc:
        def __init__(self):
            self.returncode = None
            self.stdin = None
            self.stdout, self.stderr = FakeStream(), FakeStream()
            self._done = asyncio.Event()

        async def wait(self):
            await self._done.wait()
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self._done.set()

        kill = terminate

    async def fake_exec(*args, **_kw):
        if "-i" in args:
            return FakeProc()
        return _exit_code(pkill if "pkill" in args else pgrep)

    monkeypatch.setattr(manager.asyncio, "create_subprocess_exec", fake_exec)
    spec = {
        "container": "sandbox",
        "user": "agent",
        "argv": _shell_argv("s1"),
    }
    with client.websocket_connect("/spawn", headers=AUTH) as ws:
        ws.send_text(json.dumps(spec))
        ws.send_text("terminate")
        assert json.loads(ws.receive_text()) == {"exit": -15, "stopped": stopped}


def test_spawn_handshake_timeout_releases_slot(monkeypatch):
    monkeypatch.setattr(manager, "SPAWN_HANDSHAKE_TIMEOUT", 0.01)
    limiter = manager.Limiter(1)
    monkeypatch.setattr(manager, "spawn_limiter", limiter)
    with client.websocket_connect("/spawn", headers=AUTH) as ws:
        with pytest.raises(Exception) as exc:
            ws.receive_text()
        assert getattr(exc.value, "code", None) == 4400
    assert limiter.active == 0


def test_file_endpoint_removed():
    """/file 先 realpath 再读,中间可被换成指向其他会话的链接;交付只走宿主侧 O_NOFOLLOW。"""
    body = {"container": "sandbox", "path": "/sandbox_data/sessions/a/output/f"}
    assert client.post("/file", json=body, headers=AUTH).status_code in (404, 405)


def _shell_argv(session="abc", readonly=False):
    from app.sandbox import bwrap
    from app.sandbox.constants import SESSION_ROOT
    from app.sandbox.session_paths import SessionPaths

    return bwrap.shell_argv(
        SessionPaths(session, SESSION_ROOT / session, "/workspace"), readonly=readonly
    )


@pytest.mark.parametrize("readonly", [False, True])
def test_spawn_accepts_backend_shell_argv(readonly):
    """后端 bwrap.shell_argv 改了而 manager 白名单没跟上,这里会先失败。"""
    assert manager.spawn_argv_problem(_shell_argv(readonly=readonly)) == ""


def _replace(argv, old, new):
    i = next(i for i in range(len(argv)) if argv[i : i + len(old)] == old)
    return argv[:i] + new + argv[i + len(old) :]


_S = "/sandbox_data/sessions"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: _replace(a, ["--ro-bind", "/etc", "/etc"], ["--bind", "/", "/etc"]),
        lambda a: _replace(a, ["--unshare-pid"], []),
        lambda a: _replace(a, ["--clearenv"], []),
        lambda a: _replace(a, ["--bind", f"{_S}/abc/output"], ["--bind", f"{_S}/other/output"]),
        lambda a: _replace(a, ["--ro-bind", f"{_S}/abc/uploads"], ["--bind", f"{_S}/abc/uploads"]),
        lambda a: _replace(a, ["--bind", f"{_S}/abc/output"], ["--bind", f"{_S}/abc/.."]),
        lambda a: _replace(a, ["--setenv", "HOME"], ["--setenv", "LD_PRELOAD"]),
        lambda a: _replace(a, ["--proc", "/proc"], ["--share-net"]),
        lambda a: a[:-3] + ["sh", "-c", "id"],
        lambda a: a[:-3],
    ],
)
def test_spawn_rejects_altered_argv(mutate):
    assert manager.spawn_argv_problem(mutate(_shell_argv())) != ""


def test_spawn_rejects_altered_argv_over_websocket():
    argv = _replace(_shell_argv(), ["--unshare-pid"], [])
    with client.websocket_connect("/spawn", headers=AUTH) as ws:
        ws.send_text(json.dumps({"container": "sandbox", "user": "agent", "argv": argv}))
        with pytest.raises(Exception):  # noqa: B017 - 4400 关闭
            ws.receive_text()


@pytest.mark.parametrize(
    "path", ["/sandbox_data/sessions/abc/output", "/sandbox_data/sessions/a b"]
)
def test_root_ops_only_accept_session_dir(path):
    for endpoint in ("/remove", "/provision"):
        body = {"container": "sandbox", "path": path}
        assert client.post(endpoint, json=body, headers=AUTH).status_code == 400


def test_provision_uses_nofollow_script(monkeypatch):
    seen = []

    async def fake_exec(user, script, timeout=90):
        seen.append((user, script))
        return 0, b"", b""

    monkeypatch.setattr(manager, "_docker_exec", fake_exec)
    body = {"container": "sandbox", "path": "/sandbox_data/sessions/abc"}
    assert client.post("/provision", json=body, headers=AUTH).status_code == 200
    user, script = seen[0]
    assert user == "root" and "O_NOFOLLOW" in script and "fchown" in script
    assert not any(w in script.split() for w in ("chown", "chmod"))
    assert script.endswith(" abc")
