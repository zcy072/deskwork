"""sandbox-manager 的客户端。

后端不再接触 docker.sock:所有容器操作经 HTTP/WebSocket 交给 sandbox-manager。
"""

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
import websockets

from app.sandbox.constants import COMMAND_TIMEOUT_SEC, CONTAINER_AGENT_USER, CONTAINER_ROOT
from app.sandbox.errors import TransientSandboxError
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


def _cap_timeout(timeout: int | None) -> int:
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
        self.output_limit_exceeded = False
        self.close_reason: str | None = None
        self._closed = asyncio.Event()
        self._task = asyncio.create_task(self._receive_loop())

    async def _receive_loop(self) -> None:
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    target = self.stdout if message[0] == CH_STDOUT else self.stderr
                    target.feed_data(message[1:])
                    if self._buffered() > MAX_BUFFERED_BYTES:
                        self.output_limit_exceeded = True
                        await self._ws.close()  # manager 看到断连会清掉容器内的 bwrap
                        break
                else:
                    self.returncode = json.loads(message).get("exit", 0)
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
        if self.returncode is None:
            try:
                await self._ws.send("terminate")
            except websockets.ConnectionClosed:
                pass
            try:
                await asyncio.wait_for(self._closed.wait(), timeout=grace_sec)
            except TimeoutError:
                pass
        await self._ws.close()
        self._task.cancel()


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

    async def spawn(
        self, container: str, argv: list[str], user: str = CONTAINER_AGENT_USER
    ) -> RemoteProcess:
        self._require_url()
        ws_url = self.base_url.replace("http", "ws", 1) + "/spawn"
        ws = await websockets.connect(ws_url, additional_headers=self._headers, max_size=None)
        try:
            await ws.send(json.dumps({"container": container, "user": user, "argv": argv}))
        except BaseException:
            await ws.close()
            raise
        return RemoteProcess(ws)

    async def provision_session(self, container: str, container_path: str) -> None:
        await self._post("/provision", {"container": container, "path": container_path})
        logger.info(f"Provisioned {container_path} in {container}")

    async def remove_session(self, container: str, container_path: str) -> None:
        await self._post("/remove", {"container": container, "path": container_path})

    async def run_exec(
        self,
        container: str,
        script: str,
        user: str = CONTAINER_AGENT_USER,
        timeout: int | None = None,
    ) -> ExecResult:
        if user == CONTAINER_ROOT:
            raise PermissionError("root exec is not exposed by sandbox-manager")
        try:
            resp = await self.http.post(
                "/exec",
                json={"container": container, "script": script, "timeout": _cap_timeout(timeout)},
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError from exc
        if resp.status_code == 504:
            raise TimeoutError
        resp.raise_for_status()
        data = resp.json()
        return ExecResult(data["returncode"], data["stdout"], data["stderr"])

    async def copy_from_container(
        self, container: str, container_path: str, host_path: Path
    ) -> None:
        resp = await self.http.post("/file", json={"container": container, "path": container_path})
        if resp.status_code >= 500:
            raise TransientSandboxError(f"sandbox-manager /file failed: {resp.status_code}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to copy {container_path} to {host_path}: {resp.status_code} {resp.text}"
            )
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_bytes(resp.content)

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
