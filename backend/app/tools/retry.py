"""工具错误分级处理中的"瞬时故障自动重试"。

分三级:
1. 瞬时基础设施故障(sandbox-manager 连接失败、5xx、超时):对幂等工具自动退避重试,模型无感知。
2. 逻辑错误(文件不存在、字符串无匹配等):不重试,原样交给模型自行修正。
3. 重试耗尽:返回明确的"环境暂时不可用"提示,避免模型把环境故障当成自己的参数错误而反复改参数。

只有幂等工具(读文件、展示文件)才会被重试;bash / 写文件重放可能产生副作用,不重试。
"""

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from app.sandbox.errors import TransientSandboxError
from app.utils import logger

MAX_ATTEMPTS = 3
BASE_DELAY_SECONDS = 0.5


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, TransientSandboxError | httpx.TransportError | ConnectionError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, TimeoutError)


async def retry_transient[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = MAX_ATTEMPTS,
    base_delay: float = BASE_DELAY_SECONDS,
) -> T:
    """执行 fn;遇到瞬时故障按 0.5s、1s... 退避重试,其余异常立即抛出。"""
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except Exception as exc:
            if not is_transient(exc):
                raise
            if attempt == attempts:
                raise TransientSandboxError(
                    f"沙箱环境暂时不可用(已自动重试 {attempts} 次): {type(exc).__name__}。"
                    "这不是参数错误,不要修改参数反复尝试;可稍后重试一次,仍失败请告知用户。"
                ) from exc
            delay = base_delay * 2 ** (attempt - 1)
            logger.warning(
                "瞬时故障 %s: %s,%.1fs 后重试(%d/%d)",
                type(exc).__name__,
                exc,
                delay,
                attempt,
                attempts - 1,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")
