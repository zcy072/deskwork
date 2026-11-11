"""sandbox-manager:唯一持有 docker 权限的服务。

后端不再挂载 docker.sock,而是通过这里暴露的少量固定操作访问沙箱容器:
    POST /provision  修正会话目录的属主与权限(root)
    POST /remove     删除会话目录(root)
    POST /exec       以 agent 用户执行一次性脚本
    POST /file       读取容器内文件(agent 用户)
    WS   /spawn      以 agent 用户启动 bwrap 交互进程,双向转发 stdin/stdout/stderr

约束:容器名固定;路径必须落在 /sandbox_data/sessions/ 下;spawn 的 argv[0] 必须是 bwrap;
所有请求需 Bearer Token。后端被攻破时,攻击者拿不到 docker.sock 的完整权限,但仍能在沙箱容器内
以 agent 用户执行任意脚本(/exec)、以 bwrap 参数启动进程(/spawn,argv 只校验首项,未逐项校验)。
这些操作都限制在无网络、cap_drop ALL、只读根的沙箱容器里,不能控制宿主机 docker。
/file 会对路径做 realpath,真实路径必须仍在同一会话目录内(拒绝指向别的会话或 default_skills 的符号链接)。

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
import secrets
import shlex
import time

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel

CONTAINER = os.environ.get("SANDBOX_CONTAINER", "sandbox")
TOKEN = os.environ.get("SANDBOX_MANAGER_TOKEN", "")
SESSIONS_ROOT = "/sandbox_data/sessions/"
AGENT_USER = "agent"
EXEC_TIMEOUT_SEC = 90
MAX_FILE_BYTES = 200 * 1024 * 1024
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
    normalized = posixpath.normpath(path)
    if not normalized.startswith(SESSIONS_ROOT) or normalized == SESSIONS_ROOT.rstrip("/"):
        raise HTTPException(status_code=400, detail="Path must be inside the sessions directory")
    return normalized


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
    p = shlex.quote(path)
    # 只改顶层目录:后端与 agent 同为 UID 1000,目录里的文件属主本来就可读写;
    # 递归改权限会随文件数变慢,还会给历史版本和快照加上可执行位
    script = (
        f"chmod 775 {p} {p}/uploads && "
        f"chown {AGENT_USER}:{AGENT_USER} {p}/output {p}/scratchpad && "
        f"chmod 777 {p}/output {p}/scratchpad && "
        f"mkdir -p {p}/snapshots && chown {AGENT_USER}:{AGENT_USER} {p}/snapshots && chmod 700 {p}/snapshots"
    )
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


@app.post("/file", dependencies=[Depends(require_token)])
async def read_file(body: PathBody) -> Response:
    started = time.monotonic()
    _check_container(body.container)
    raw_path = _check_session_path(body.path)
    path = shlex.quote(raw_path)
    # 符号链接可能指向别的会话或 default_skills:真实路径必须仍在同一会话目录内
    session_dir = SESSIONS_ROOT + session_of(raw_path)
    guard = f'R=$(realpath -e -- {path}) && case "$R" in {shlex.quote(session_dir)}/*) ;; *) exit 77;; esac'
    # 一次 exec 内读取并限制长度:分成 stat + cat 两步时,文件可能在两步之间被换成更大的
    code, data, _ = await _docker_exec(
        AGENT_USER, f'{guard} && test -f "$R" && head -c {MAX_FILE_BYTES + 1} "$R"'
    )
    if code == 77:
        audit("file", "escape_blocked", started, session=session_of(raw_path))
        raise HTTPException(status_code=403, detail="Path resolves outside the session")
    if code != 0:
        audit("file", "not_found", started, session=session_of(raw_path))
        raise HTTPException(status_code=404, detail="File not found")
    if len(data) > MAX_FILE_BYTES:
        audit("file", "too_large", started, session=session_of(raw_path))
        raise HTTPException(status_code=413, detail="File too large")
    audit("file", "ok", started, session=session_of(raw_path), bytes=len(data))
    return Response(content=data, media_type="application/octet-stream")


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
    if not valid:
        audit("spawn", "invalid_spec", started)
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
