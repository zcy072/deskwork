"""sandbox-manager 的客户端:只负责通信,不含会话逻辑。

后端不接触 docker.sock,所有容器操作经 HTTP / WebSocket 交给 sandbox-manager:
    WS   /spawn      启动常驻 bwrap shell,帧首字节区分 stdin / stdout / stderr  -> spawn()
    POST /exec       以 agent 用户跑一次性脚本(快照、恢复、隔离命令)        -> run_exec()
    POST /file       读会话内文件(宿主侧读不到时的兜底)                      -> read_from_container()
    POST /provision  以 root 修正会话目录权限                                  -> provision_session()
    POST /remove     以 root 删除会话目录                                      -> remove_session()
容器内的用户固定为 agent,root 操作只有 provision / remove 两个固定动作。
"""

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass

import httpx
import websockets

from app.sandbox.constants import COMMAND_TIMEOUT_SEC, CONTAINER_AGENT_USER
from app.sandbox.errors import SandboxStopUnconfirmed, TransientSandboxError
from app.utils import logger

CH_STDIN, CH_STDOUT, CH_STDERR = 0, 1, 2
STREAM_LINE_LIMIT = 16 * 1024 * 1024
# 未被消费的缓冲上限:读取方跟不上(或命令疯狂输出)时关连接,而不是无限占内存
MAX_BUFFERED_BYTES = 20 * 1024 * 1024
# sandbox-manager 在 /spawn 上拒绝请求时用的关闭码(见 sandbox/manager/main.py)
SPAWN_CLOSE_REASONS = {
    4401: "sandbox-manager rejected the token (check SANDBOX_MANAGER_TOKEN)",
    4429: "sandbox-manager is at its concurrent shell limit (MANAGER_MAX_SPAWN); try again shortly",
    4400: "sandbox-manager rejected the spawn request",
}


def cap_timeout(timeout: int | None) -> int:
    """命令超时不超过 COMMAND_TIMEOUT_SEC;None 取上限。"""
    if timeout is None:
        return COMMAND_TIMEOUT_SEC
    return min(int(timeout), COMMAND_TIMEOUT_SEC)


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str


class RemoteProcess:
    """用 WebSocket 模拟 asyncio.subprocess.Process 中 BashSession 用到的部分。"""

    def __init__(self, ws) -> None:
        self._ws = ws
        # 默认 64KB 上限会让 readline() 在超长单行(压缩 JSON、base64)上抛 ValueError
        self.stdout = asyncio.StreamReader(limit=STREAM_LINE_LIMIT)
        self.stderr = asyncio.StreamReader(limit=STREAM_LINE_LIMIT)
        self.returncode: int | None = None
        # manager 的结束回执里确认容器内的 bwrap 已经消失才为 True;连接断开而没有回执时保持 False
        self.stopped = False
        self.output_limit_exceeded = False
        self.close_reason: str | None = None
        self._closed = asyncio.Event()
        self._task = asyncio.create_task(self._receive_loop())

    async def _receive_loop(self) -> None:
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    if self.output_limit_exceeded:
                        continue  # 已请求终止,丢弃剩余输出,继续等结束回执
                    target = self.stdout if message[0] == CH_STDOUT else self.stderr
                    target.feed_data(message[1:])
                    if self._buffered() > MAX_BUFFERED_BYTES:
                        self.output_limit_exceeded = True
                        # 让 manager 杀掉进程并回执,而不是直接断开:断开就拿不到"已停止"的确认
                        with contextlib.suppress(websockets.ConnectionClosed):
                            await self._ws.send("terminate")
                else:
                    receipt = json.loads(message)
                    self.returncode = receipt.get("exit", 0)
                    self.stopped = receipt.get("stopped") is True
        except websockets.ConnectionClosed:
            pass
        finally:
            self.close_reason = SPAWN_CLOSE_REASONS.get(getattr(self._ws, "close_code", None))
            if self.returncode is None:
                self.returncode = -1
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._closed.set()

    def _buffered(self) -> int:
        # StreamReader 没有公开的已缓冲字节数;_buffer 是 bytearray
        return len(self.stdout._buffer) + len(self.stderr._buffer)  # type: ignore[attr-defined]

    async def send_stdin(self, data: bytes) -> None:
        if self.returncode is not None:
            raise RuntimeError("Process stdin is not available")
        await self._ws.send(bytes([CH_STDIN]) + data)

    async def terminate(self, grace_sec: float = 5.0) -> None:
        """请求结束并等待 manager 的回执;回执没确认已停止就抛 SandboxStopUnconfirmed。"""
        if not self._closed.is_set():
            with contextlib.suppress(websockets.ConnectionClosed):
                await self._ws.send("terminate")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._closed.wait()), timeout=grace_sec)
        if not self._closed.is_set():
            # 没等到回执:保留连接和接收任务,之后还能再试一次,不当作已停止
            raise SandboxStopUnconfirmed("sandbox process did not confirm termination")
        with contextlib.suppress(Exception):
            await self._ws.close()
        if not self.stopped:
            raise SandboxStopUnconfirmed("sandbox-manager could not confirm the process stopped")


class ManagerClient:
    """容器操作(spawn、exec、provision、remove、取文件)都经 sandbox-manager 完成。"""

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        # 模块导入时就会建客户端;没配置时到第一次使用才报错,测试不用连 manager
        self.base_url = (base_url or os.environ.get("SANDBOX_MANAGER_URL", "")).rstrip("/")
        token = token or os.environ.get("SANDBOX_MANAGER_TOKEN", "")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._http: httpx.AsyncClient | None = None

    def _require_url(self) -> None:
        if not self.base_url:
            raise RuntimeError("SANDBOX_MANAGER_URL is not set; the sandbox needs sandbox-manager")

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._require_url()
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._headers,
                timeout=COMMAND_TIMEOUT_SEC + 10,
            )
        return self._http

    async def spawn(self, container: str, argv: list[str]) -> RemoteProcess:
        """以 agent 用户启动 argv(argv[0] 须为 bwrap)。被拒绝时连接随即关闭,原因见 close_reason。"""
        self._require_url()
        ws_url = self.base_url.replace("http", "ws", 1) + "/spawn"
        ws = await websockets.connect(ws_url, additional_headers=self._headers, max_size=None)
        try:
            await ws.send(
                json.dumps({"container": container, "user": CONTAINER_AGENT_USER, "argv": argv})
            )
        except BaseException:
            await ws.close()
            raise
        return RemoteProcess(ws)

    async def provision_session(self, container: str, container_path: str) -> None:
        await self._post("/provision", {"container": container, "path": container_path})
        logger.info(f"Provisioned {container_path} in {container}")

    async def remove_session(self, container: str, container_path: str) -> None:
        await self._post("/remove", {"container": container, "path": container_path})

    async def run_exec(self, container: str, script: str, timeout: int | None = None) -> ExecResult:
        """以 agent 用户执行 sh 脚本。超时抛 TimeoutError,其他非 200 抛 httpx.HTTPStatusError。"""
        try:
            resp = await self.http.post(
                "/exec",
                json={"container": container, "script": script, "timeout": cap_timeout(timeout)},
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError from exc
        if resp.status_code == 504:
            raise TimeoutError
        resp.raise_for_status()
        data = resp.json()
        return ExecResult(data["returncode"], data["stdout"], data["stderr"])

    async def read_from_container(self, container: str, container_path: str) -> bytes:
        resp = await self.http.post("/file", json={"container": container, "path": container_path})
        if resp.status_code >= 500:
            raise TransientSandboxError(f"sandbox-manager /file failed: {resp.status_code}")
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to read {container_path}: {resp.status_code} {resp.text}")
        return resp.content

    @staticmethod
    async def write_stdin(process: RemoteProcess, data: str) -> None:
        await process.send_stdin(data.encode())

    @staticmethod
    async def terminate(process: RemoteProcess, grace_sec: float = 5.0) -> None:
        await process.terminate(grace_sec)

    async def _post(self, path: str, payload: dict) -> None:
        resp = await self.http.post(path, json=payload)
        if resp.status_code >= 500:
            raise TransientSandboxError(f"sandbox-manager {path} failed: {resp.status_code}")
        if resp.status_code != 200:
            raise RuntimeError(f"sandbox-manager {path} failed: {resp.status_code} {resp.text}")
