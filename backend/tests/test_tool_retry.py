"""工具错误分级:瞬时故障重试,逻辑错误不重试,耗尽后给出明确提示。"""

import httpx
import pytest

from app.sandbox.errors import ContainerFileSystemError, TransientSandboxError
from app.tools.retry import is_transient, retry_transient


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://m/exec")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(status, request=req))


def test_classification():
    assert is_transient(TransientSandboxError("x"))
    assert is_transient(httpx.ConnectError("refused"))
    assert is_transient(TimeoutError())
    assert is_transient(_http_error(502))
    assert not is_transient(_http_error(400))
    assert not is_transient(ContainerFileSystemError("Path not found"))
    assert not is_transient(ValueError("bad"))


async def test_retries_then_succeeds():
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("refused")
        return "ok"

    assert await retry_transient(fn, base_delay=0) == "ok"
    assert len(calls) == 3


async def test_logic_error_not_retried():
    calls = []

    async def fn():
        calls.append(1)
        raise ContainerFileSystemError("Path not found: a.txt")

    with pytest.raises(ContainerFileSystemError):
        await retry_transient(fn, base_delay=0)
    assert len(calls) == 1


async def test_exhausted_gives_clear_message():
    async def fn():
        raise _http_error(503)

    with pytest.raises(TransientSandboxError, match="不是参数错误"):
        await retry_transient(fn, attempts=2, base_delay=0)
