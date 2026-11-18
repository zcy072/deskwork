"""sandbox-manager:唯一持有 docker 权限的服务。

后端不再挂载 docker.sock,而是通过这里暴露的少量固定操作访问沙箱容器:
    POST /provision  修正会话目录的属主与权限(root)
    POST /remove     删除会话目录(root)
    POST /exec       以 agent 用户执行一次性脚本
    WS   /spawn      以 agent 用户启动 bwrap 交互进程,双向转发 stdin/stdout/stderr

约束:容器名固定;root 操作的路径只能是 /sandbox_data/sessions/<id>;spawn 的 bwrap 参数逐项按白名单校验;
所有请求需 Bearer Token。后端被攻破时,攻击者拿不到 docker.sock 的完整权限,但仍能在沙箱容器内
以 agent 用户执行任意脚本(/exec,不在 bwrap 内,能看到所有会话目录)。/spawn 只能启动会话 shell 这一种形状。
这些操作都限制在无网络、cap_drop ALL、只读根的沙箱容器里,不能控制宿主机 docker。

限流:同时在跑的 docker exec 数和同时在线的 spawn 会话数各有上限,超限直接拒绝(HTTP 429 /
WebSocket 4429),不排队。审计:每个操作写一行 JSON 日志(操作、会话、结果、耗时),脚本只记
长度和哈希,不记内容。
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import posixpath
import re
import secrets
import shlex
import time

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel

CONTAINER = os.environ.get("SANDBOX_CONTAINER", "sandbox")
TOKEN = os.environ.get("SANDBOX_MANAGER_TOKEN", "")
SESSIONS_ROOT = "/sandbox_data/sessions/"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
AGENT_USER = "agent"
EXEC_TIMEOUT_SEC = 90
MAX_CONCURRENT_EXEC = int(os.environ.get("MANAGER_MAX_EXEC", "16"))
MAX_CONCURRENT_SPAWN = int(os.environ.get("MANAGER_MAX_SPAWN", "32"))
SPAWN_HANDSHAKE_TIMEOUT = 10.0

# /spawn 用 sh 托着 bwrap,参数经 "$@" 原样转交。sh 自己不能 exec 成 bwrap
# (那样 --die-with-parent 的父进程又跑到容器外了),末尾的 exit $? 也防止
# 某些 sh 把最后一条命令 exec 掉。
SPAWN_WRAPPER = '"$@"; exit $?'
# 杀掉 bwrap 后轮询确认进程消失:SIGKILL 是异步的
STOP_CONFIRM_POLLS = 20
STOP_CONFIRM_INTERVAL = 0.1

# /provision 的容器内脚本。只改顶层目录:后端与 agent 同为 UID 1000,目录里的文件属主本来就可读写;
# 递归改权限会随文件数变慢,还会给历史版本和快照加上可执行位
PROVISION_PY = f"""
import os, pwd, sys
NOFOLLOW = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
agent = pwd.getpwnam({AGENT_USER!r})
sessions = os.open({SESSIONS_ROOT!r}, NOFOLLOW)
root = os.open(sys.argv[1], NOFOLLOW, dir_fd=sessions)
try:
    os.mkdir("snapshots", 0o700, dir_fd=root)
except FileExistsError:
    pass
for name, mode, give in (
    (None, 0o775, False), ("uploads", 0o775, False), ("output", 0o777, True),
    ("scratchpad", 0o777, True), ("snapshots", 0o700, True),
):
    fd = root if name is None else os.open(name, NOFOLLOW, dir_fd=root)
    if give:
        os.fchown(fd, agent.pw_uid, agent.pw_gid)
    os.fchmod(fd, mode)
"""

# 通道前缀:二进制帧首字节标识流
CH_STDIN, CH_STDOUT, CH_STDERR = 0, 1, 2

app = FastAPI(title="deskwork-sandbox-manager")

audit_log = logging.getLogger("sandbox_manager.audit")
if not audit_log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    audit_log.addHandler(_handler)
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False


class Limiter:
    """并发上限计数器:不排队,超限立即拒绝。单进程事件循环内使用,不需要锁。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0

    def acquire(self) -> bool:
        if self.active >= self.limit:
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active = max(0, self.active - 1)


exec_limiter = Limiter(MAX_CONCURRENT_EXEC)
spawn_limiter = Limiter(MAX_CONCURRENT_SPAWN)


def audit(op: str, result: str, started: float, **fields: object) -> None:
    """一行 JSON。只放标识和结果,不放脚本 / 文件内容。"""
    record = {
        "ts": round(time.time(), 3),
        "op": op,
        "result": result,
        "ms": round((time.monotonic() - started) * 1000),
        **fields,
    }
    audit_log.info(json.dumps(record, ensure_ascii=False))


def session_of(path: str) -> str:
    """/sandbox_data/sessions/<id>/... -> <id>;取不到返回空串。"""
    if not path.startswith(SESSIONS_ROOT):
        return ""
    return path[len(SESSIONS_ROOT) :].split("/", 1)[0]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _token_ok(value: str | None) -> bool:
    return bool(TOKEN) and value is not None and hmac.compare_digest(value, f"Bearer {TOKEN}")


def require_token(authorization: str | None = Header(default=None)) -> None:
    if not _token_ok(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _check_container(name: str) -> None:
    if name != CONTAINER:
        raise HTTPException(status_code=403, detail="Unknown container")


def _check_session_path(path: str) -> str:
    """只接受 /sandbox_data/sessions/<会话 id> 这一层:root 操作不需要更深的路径。"""
    normalized = posixpath.normpath(path)
    if not normalized.startswith(SESSIONS_ROOT) or not SESSION_ID.fullmatch(
        normalized[len(SESSIONS_ROOT) :]
    ):
        raise HTTPException(status_code=400, detail="Path must be a session directory")
    return normalized


# /spawn 的 bwrap 参数白名单,和 backend/app/sandbox/bwrap.py::shell_argv 对应。
# 不逐项校验的话,拿到 token 的人可以 --bind / /x 把整个容器挂进来,或省掉 --unshare-pid 等隔离项。
_SYSTEM_RO = {"/usr": "/usr", "/lib": "/lib", "/lib64": "/lib64", "/etc": "/etc"}
_SYMLINKS = {("usr/bin", "/bin"), ("usr/sbin", "/sbin")}
_SESSION_SUBDIRS = ("uploads", "output", "scratchpad")
_SKILLS = "/sandbox_data/default_skills"
_WORKSPACE = "/workspace"
_ENV_NAMES = {"HOME", "PATH", "NODE_PATH", "TMPDIR", "TERM", "LANG"}
_REQUIRED_FLAGS = {"--unshare-pid", "--die-with-parent", "--clearenv", "--new-session"}
_ARITY = {
    "--ro-bind": 2, "--bind": 2, "--symlink": 2, "--setenv": 2,
    "--dev": 1, "--proc": 1, "--tmpfs": 1, "--dir": 1, "--chdir": 1, "--size": 1,
    "--unshare-pid": 0, "--die-with-parent": 0, "--clearenv": 0, "--new-session": 0,
}  # fmt: skip
_PROGRAM = ["bash", "--noprofile", "--norc"]


def spawn_argv_problem(argv: list[str]) -> str:
    """不合规时返回原因,合规返回空串。只允许会话 shell 这一种形状。"""
    if argv[-len(_PROGRAM) :] != _PROGRAM:
        return "program"
    i, end, flags, session = 1, len(argv) - len(_PROGRAM), set(), None
    while i < end:
        opt = argv[i]
        if opt not in _ARITY or i + _ARITY[opt] >= end + (_ARITY[opt] == 0):
            return f"option {opt[:32]}"
        args = argv[i + 1 : i + 1 + _ARITY[opt]]
        i += 1 + _ARITY[opt]
        flags.add(opt)
        if opt in ("--ro-bind", "--bind"):
            src, dest = args
            if opt == "--ro-bind" and (
                _SYSTEM_RO.get(src) == dest or (src, dest) == (_SKILLS, _WORKSPACE + "/skills")
            ):
                continue
            parts = src[len(SESSIONS_ROOT) :].split("/") if src.startswith(SESSIONS_ROOT) else []
            if not (
                len(parts) == 2
                and SESSION_ID.fullmatch(parts[0])
                and parts[1] in _SESSION_SUBDIRS
                and session in (None, parts[0])
                and dest == f"{_WORKSPACE}/{parts[1]}"
                and (opt == "--ro-bind" or parts[1] != "uploads")
            ):
                return f"bind {src[:64]}"
            session = parts[0]
        elif opt == "--symlink" and tuple(args) not in _SYMLINKS:
            return "symlink"
        elif opt == "--setenv" and args[0] not in _ENV_NAMES:
            return f"env {args[0][:32]}"
        elif (
            opt in ("--dev", "--proc", "--tmpfs")
            and args[0] != {"--dev": "/dev", "--proc": "/proc", "--tmpfs": "/tmp"}[opt]
        ):
            return opt
        elif opt == "--dir" and args[0] != _WORKSPACE:
            return opt
        elif opt == "--chdir" and not (
            args[0] == _WORKSPACE or args[0].startswith(_WORKSPACE + "/")
        ):
            return opt
        elif opt == "--size" and not args[0].isdigit():
            return opt
    if session is None or not _REQUIRED_FLAGS <= flags:
        return "missing isolation flags or session binds"
    return ""


async def _docker_exec(user: str, script: str, timeout: int = EXEC_TIMEOUT_SEC):
    timeout = max(1, timeout)
    if not exec_limiter.acquire():
        raise HTTPException(status_code=429, detail="Too many concurrent operations")
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-u", user, CONTAINER,
            # 容器内也要有超时:只杀 docker exec 客户端不会终止容器里的进程
            "timeout", "-s", "KILL", str(timeout), "sh", "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()  # 容器内的进程由 `timeout -s KILL` 兜底
            await proc.wait()
            raise HTTPException(status_code=504, detail="Command timed out") from None
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        return proc.returncode or 0, out, err
    finally:
        exec_limiter.release()


class PathBody(BaseModel):
    container: str
    path: str


class ExecBody(BaseModel):
    container: str
    script: str
    timeout: int | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/provision", dependencies=[Depends(require_token)])
async def provision(body: PathBody) -> dict[str, int]:
    started = time.monotonic()
    _check_container(body.container)
    path = _check_session_path(body.path)
    # 以 root 运行:从 sessions 根逐段 O_NOFOLLOW 打开,再对描述符 fchown/fchmod。
    # chown/chmod 按路径操作会跟随符号链接,子目录被换成指向 /etc 的链接时会改到容器系统文件
    script = f"exec python3 -I -c {shlex.quote(PROVISION_PY)} {shlex.quote(session_of(path))}"
    code, _, err = await _docker_exec("root", script)
    if code != 0:
        audit("provision", "error", started, session=session_of(path), code=code)
        raise HTTPException(status_code=500, detail=err.decode(errors="replace").strip())
    audit("provision", "ok", started, session=session_of(path))
    return {"returncode": 0}


@app.post("/remove", dependencies=[Depends(require_token)])
async def remove(body: PathBody) -> dict[str, int]:
    started = time.monotonic()
    _check_container(body.container)
    path = _check_session_path(body.path)
    code, _, err = await _docker_exec("root", f"rm -rf {shlex.quote(path)}")
    if code != 0:
        audit("remove", "error", started, session=session_of(path), code=code)
        raise HTTPException(status_code=500, detail=err.decode(errors="replace").strip())
    audit("remove", "ok", started, session=session_of(path))
    return {"returncode": 0}


@app.post("/exec", dependencies=[Depends(require_token)])
async def exec_script(body: ExecBody) -> dict[str, object]:
    started = time.monotonic()
    _check_container(body.container)
    timeout = max(1, min(body.timeout or EXEC_TIMEOUT_SEC, EXEC_TIMEOUT_SEC))
    info = {"script_len": len(body.script), "script_sha": digest(body.script)}
    try:
        code, out, err = await _docker_exec(AGENT_USER, body.script, timeout)
    except HTTPException as exc:
        audit("exec", f"http_{exc.status_code}", started, **info)
        raise
    audit("exec", "ok" if code == 0 else "nonzero", started, code=code, **info)
    return {
        "returncode": code,
        "stdout": out.decode(errors="replace"),
        "stderr": err.decode(errors="replace"),
    }


@app.websocket("/spawn")
async def spawn(ws: WebSocket) -> None:
    started = time.monotonic()
    if not _token_ok(ws.headers.get("authorization")):
        audit("spawn", "unauthorized", started)
        await ws.close(code=4401)
        return
    await ws.accept()
    if not spawn_limiter.acquire():
        audit("spawn", "rejected_limit", started)
        await ws.close(code=4429)
        return
    try:
        await _run_spawn(ws, started)
    finally:
        spawn_limiter.release()


async def _run_spawn(ws: WebSocket, started: float) -> None:

    try:
        spec = json.loads(await asyncio.wait_for(ws.receive_text(), SPAWN_HANDSHAKE_TIMEOUT))
    except WebSocketDisconnect:
        return
    except (ValueError, TimeoutError):
        spec = None
    if not isinstance(spec, dict):
        audit("spawn", "invalid_spec", started)
        await ws.close(code=4400)
        return
    argv = spec.get("argv")
    valid = (
        spec.get("container") == CONTAINER
        and spec.get("user") == AGENT_USER
        and isinstance(argv, list)
        and argv
        and all(isinstance(a, str) for a in argv)
        and argv[0] == "bwrap"
    )
    problem = spawn_argv_problem(argv) if valid else "spec"
    if problem:
        audit("spawn", "invalid_spec", started, problem=problem)
        await ws.close(code=4400)
        return
    needle = next((a for a in argv if a.startswith(SESSIONS_ROOT)), "")
    session = session_of(needle)
    audit("spawn", "start", started, session=session)
    # 给这个 bwrap 打上唯一标记,结束时只杀它:按会话目录匹配会连同一会话里并行的
    # 一次性 bwrap(文件预览转换等)和刚重启的新 shell 一起杀掉。
    # 标记在 --clearenv 之前设置,随即被清掉,不会出现在 agent 的环境变量里。
    spawn_id = secrets.token_hex(8)
    argv = ["bwrap", "--setenv", "DESKWORK_SPAWN_ID", spawn_id, *argv[1:]]

    # bwrap 不能直接当 docker exec 的进程:它的父进程在容器外,并发启动时偶尔先退出,
    # --die-with-parent 随即 SIGKILL 掉刚起的 shell(退出码 137)。垫一层 sh 当父进程,
    # 不用 exec,保证 sh 一直活到 bwrap 结束。
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "-i", "-u", AGENT_USER, CONTAINER,
        "sh", "-c", SPAWN_WRAPPER, "sh", *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip

    pattern = f"^bwrap --setenv DESKWORK_SPAWN_ID {spawn_id} "

    async def in_container(*cmd: str) -> int:
        child = await asyncio.create_subprocess_exec(
            "docker", "exec", "-u", AGENT_USER, CONTAINER, *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        return await child.wait()

    async def kill_in_container() -> bool:
        """杀掉容器内这次启动的 bwrap(--unshare-pid 保证其下所有进程一并消失),返回是否确认已停止。

        `docker exec` 的客户端退出并不会终止容器里的进程,所以必须显式清理。pkill 返回 0(已杀)
        或 1(没有匹配,早已退出)之外的码说明没执行成功;之后用 pgrep 确认进程确实不在了。
        """
        try:
            if await in_container("pkill", "-KILL", "-f", "--", pattern) not in (0, 1):
                return False
            for _ in range(STOP_CONFIRM_POLLS):
                if await in_container("pgrep", "-f", "--", pattern) == 1:
                    return True
                await asyncio.sleep(STOP_CONFIRM_INTERVAL)
        except Exception:
            audit_log.exception("kill_in_container failed")
        return False

    stop_state: dict[str, bool | None] = {"stopped": None}

    async def pump(stream: asyncio.StreamReader, channel: int) -> None:
        while chunk := await stream.read(65536):
            await ws.send_bytes(bytes([channel]) + chunk)

    async def inbound() -> None:
        """客户端 -> 进程:二进制帧写 stdin,文本帧 terminate 结束进程。"""
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes"):
                assert proc.stdin is not None
                try:
                    proc.stdin.write(message["bytes"][1:])
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    break  # docker exec 已退出;照常清理容器里可能残留的 bwrap
            elif message.get("text") == "terminate":
                break
        stop_state["stopped"] = await kill_in_container()
        if proc.returncode is None:
            proc.terminate()

    readers = [
        asyncio.create_task(pump(proc.stdout, CH_STDOUT)),
        asyncio.create_task(pump(proc.stderr, CH_STDERR)),
    ]
    reader_in = asyncio.create_task(inbound())
    try:
        await proc.wait()
        await asyncio.gather(*readers, return_exceptions=True)
        # docker exec 客户端退出不等于容器内的 bwrap 已结束:没经 terminate 确认过就再确认一次
        stopped = stop_state["stopped"]
        if not stopped:
            stopped = await kill_in_container()
        await ws.send_text(json.dumps({"exit": proc.returncode, "stopped": stopped}))
        await ws.close()
    except Exception:
        pass
    finally:
        reader_in.cancel()
        for task in readers:
            task.cancel()
        await asyncio.gather(reader_in, *readers, return_exceptions=True)
        audit(
            "spawn",
            "end",
            started,
            session=session,
            exit=proc.returncode,
            stopped=stop_state["stopped"],
        )
        if proc.returncode is None:
            # 仅在进程仍在运行(异常断连)时清理;正常退出后命名空间已消失
            await kill_in_container()
            proc.kill()
            await proc.wait()
