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


@pytest.mark.parametrize("path", ["/provision", "/remove", "/exec", "/file"])
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
    for endpoint in ("/remove", "/provision", "/file"):
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


def _local_exec(monkeypatch, root):
    """把 docker exec 换成本机 sh,并把 SESSIONS_ROOT 指向临时目录,直接测 /file 的脚本。"""
    import asyncio

    monkeypatch.setattr(manager, "SESSIONS_ROOT", f"{root}/sessions/")

    async def fake_exec(user, script, timeout=90):
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out, err

    monkeypatch.setattr(manager, "_docker_exec", fake_exec)


def test_file_reads_regular_file_and_in_session_symlink(tmp_path, monkeypatch):
    _local_exec(monkeypatch, tmp_path)
    out = tmp_path / "sessions" / "a" / "output"
    out.mkdir(parents=True)
    (out / "f.txt").write_text("hello")
    (out / "ok_link").symlink_to(out / "f.txt")
    for name in ("f.txt", "ok_link"):
        resp = client.post(
            "/file",
            json={"container": "sandbox", "path": f"{tmp_path}/sessions/a/output/{name}"},
            headers=AUTH,
        )
        assert resp.status_code == 200 and resp.content == b"hello"


def test_file_blocks_symlink_to_other_session_or_skills(tmp_path, monkeypatch):
    _local_exec(monkeypatch, tmp_path)
    mine = tmp_path / "sessions" / "a" / "output"
    other = tmp_path / "sessions" / "b" / "output"
    skills = tmp_path / "default_skills"
    for d in (mine, other, skills):
        d.mkdir(parents=True)
    (other / "secret.txt").write_text("secret")
    (skills / "s.md").write_text("skill")
    (mine / "to_other").symlink_to(other / "secret.txt")
    (mine / "to_skills").symlink_to(skills / "s.md")
    (mine / "dir_link").symlink_to(other)
    for name in ("to_other", "to_skills", "dir_link/secret.txt"):
        resp = client.post(
            "/file",
            json={"container": "sandbox", "path": f"{tmp_path}/sessions/a/output/{name}"},
            headers=AUTH,
        )
        assert resp.status_code == 403, name
        assert b"secret" not in resp.content


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


def test_file_too_large_is_rejected(tmp_path, monkeypatch):
    _local_exec(monkeypatch, tmp_path)
    monkeypatch.setattr(manager, "MAX_FILE_BYTES", 10)
    out = tmp_path / "sessions" / "a" / "output"
    out.mkdir(parents=True)
    (out / "small.txt").write_bytes(b"x" * 10)
    (out / "big.txt").write_bytes(b"x" * 11)
    for name, status in (("small.txt", 200), ("big.txt", 413), ("missing.txt", 404)):
        resp = client.post(
            "/file",
            json={"container": "sandbox", "path": f"{tmp_path}/sessions/a/output/{name}"},
            headers=AUTH,
        )
        assert resp.status_code == status, name


@pytest.mark.parametrize("disconnect", [False, True])
def test_spawn_kills_only_its_own_bwrap(monkeypatch, disconnect):
    """结束时按本次的唯一标记杀 bwrap,不按会话目录匹配(会误杀同会话并行的 bwrap)。"""
    import asyncio
    from types import SimpleNamespace

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
        return FakeProc() if "-i" in args else SimpleNamespace(wait=lambda: asyncio.sleep(0))

    monkeypatch.setattr(manager.asyncio, "create_subprocess_exec", fake_exec)
    spec = {
        "container": "sandbox",
        "user": "agent",
        "argv": ["bwrap", "--bind", "/sandbox_data/sessions/s1/output", "/w", "bash"],
    }
    with client.websocket_connect("/spawn", headers=AUTH) as ws:
        ws.send_text(json.dumps(spec))
        if disconnect:
            ws.close()
        else:
            ws.send_bytes(b"\x00echo hi\n")  # stdin 已断:应当照常清理而不是抛异常
            assert json.loads(ws.receive_text()) == {"exit": -15}
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


def test_spawn_handshake_timeout_releases_slot(monkeypatch):
    monkeypatch.setattr(manager, "SPAWN_HANDSHAKE_TIMEOUT", 0.01)
    limiter = manager.Limiter(1)
    monkeypatch.setattr(manager, "spawn_limiter", limiter)
    with client.websocket_connect("/spawn", headers=AUTH) as ws:
        with pytest.raises(Exception) as exc:
            ws.receive_text()
        assert getattr(exc.value, "code", None) == 4400
    assert limiter.active == 0
