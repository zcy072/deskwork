import asyncio
import json
import time
from collections.abc import AsyncGenerator

import redis.asyncio as aioredis

from app.config import settings
from app.utils.custom_logger import logger

STREAM_PREFIX = "stream:"
SESSION_PREFIX = "session:"
THREAD_STATUS_PREFIX = "thread-status:"
STREAM_TTL_SECONDS = 300
# 终态状态只需保留一小会儿供前端查询;running 必须覆盖任务的最长运行时间,
# 否则过期后会被判定为空闲,同一会话可并发启动第二个任务(基线 #6)。
THREAD_TTL_SECONDS = 400
THREAD_RUNNING_TTL_SECONDS = 3600
# 等待一个尚不存在的流出现的上限(基线 #8)
STREAM_MISSING_TIMEOUT_SECONDS = 30
DONE_TOKEN = "[DONE]"
REDIS_SOCKET_TIMEOUT_SECONDS = 20

_redis: aioredis.Redis | None = None


async def connect_redis() -> None:
    global _redis
    _redis = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        # 必须明显大于 XREAD 的最大阻塞时间(block_ms 上限 4s),否则 WSL/Docker 稍有卡顿就会读超时。
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    )
    await _redis.ping()
    logger.info(f"Connected to Redis at {settings.redis_display_url}")


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        logger.info("Redis connection closed")
    _redis = None


async def redis_health() -> bool:
    global _redis

    if _redis is None:
        return False

    try:
        return await _redis.ping()
    except Exception:
        logger.exception("Redis health check failed")
        return False


def _client() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis is not initialized. Call connect_redis() first.")
    return _redis


def _stream_key(prompt_id: str) -> str:
    return f"{STREAM_PREFIX}{prompt_id}"


def _thread_status_key(session_id: str) -> str:
    return f"{THREAD_STATUS_PREFIX}{session_id}"


def _session_key(session_id: str) -> str:
    return f"{SESSION_PREFIX}{session_id}"


async def set_thread_status(session_id: str, status: str) -> None:
    """Persist the current execution status for a LangGraph thread."""
    redis = _client()

    ttl = THREAD_RUNNING_TTL_SECONDS if status == "running" else THREAD_TTL_SECONDS
    await redis.set(_thread_status_key(session_id), status, ex=ttl)


async def refresh_running_ttl(session_id: str) -> None:
    """长任务运行期间续期 running 状态,避免超过 1 小时后被判定为空闲而允许并发启动第二个任务。

    同时续期该会话的 prompt 归属记录:否则运行超过 1 小时后客户端重连会因归属记录过期而 404。
    只在状态仍是 running 时续期,不会把已结束的状态(短 TTL)重新拉长;任务结束后不再续期,
    归属记录最多再保留一个 TTL。
    """
    redis = _client()
    key = _thread_status_key(session_id)
    if await redis.get(key) == "running":
        pipe = redis.pipeline(transaction=False)
        pipe.expire(key, THREAD_RUNNING_TTL_SECONDS)
        pipe.expire(_session_key(session_id), THREAD_RUNNING_TTL_SECONDS)
        await pipe.execute()


async def get_thread_status(session_id: str) -> str | None:
    """Return the stored execution status, or ``None`` for an unknown thread."""
    redis = _client()

    return await redis.get(_thread_status_key(session_id))


async def get_thread_statuses(session_ids: list[str]) -> list[str | None]:
    """批量读取状态(一次 MGET),顺序与 session_ids 一致。"""
    if not session_ids:
        return []
    return await _client().mget([_thread_status_key(s) for s in session_ids])


async def recover_orphaned_runs() -> int:
    """启动时调用:此刻 Redis 里仍是 running 的会话,其任务属于上一个进程,已经不存在了。

    把状态改成 error 并给它的流补上结束标记,否则客户端会一直重连,
    同一会话在 running 的 1 小时 TTL 内也发不了新消息(409)。返回处理的会话数。
    """
    redis = _client()

    recovered = 0
    async for key in redis.scan_iter(match=f"{THREAD_STATUS_PREFIX}*"):
        if await redis.get(key) != "running":
            continue
        session_id = key[len(THREAD_STATUS_PREFIX) :]
        await set_thread_status(session_id, "error")
        for prompt_id in await redis.smembers(_session_key(session_id)):
            stream_key = _stream_key(prompt_id)
            if not await redis.exists(stream_key):
                continue
            error = json.dumps(
                {"event": "error", "data": {"message": "服务重启,本次生成已中断"}},
            )
            end = json.dumps({"event": "stream.end", "data": {}})
            await redis.xadd(stream_key, {"token": error})
            await redis.xadd(stream_key, {"token": end})
            await mark_done(prompt_id)
        recovered += 1
    return recovered


async def register_prompt(session_id: str, prompt_id: str) -> None:
    """Record that a prompt_id belongs to a session_id."""
    redis = _client()

    key = _session_key(session_id)
    await redis.sadd(key, prompt_id)
    # 归属记录必须覆盖任务的最长运行时间:任务跑过 STREAM_TTL 后客户端重连会校验归属,
    # 记录过期就会 404(评测中 300s+ 的 PPT 任务触发过)。流本身的过期由 mark_done 控制。
    await redis.expire(key, THREAD_RUNNING_TTL_SECONDS)


async def prompt_belongs_to_session(session_id: str, prompt_id: str) -> bool:
    """Return True if prompt_id was registered under session_id."""
    redis = _client()

    return bool(await redis.sismember(_session_key(session_id), prompt_id))


async def add_token(prompt_id: str, token: str) -> None:
    redis = _client()

    key = _stream_key(prompt_id)
    # 运行中也带过期:进程被杀、来不及 mark_done 时流不会永久留在 Redis 里。
    # 每次写入都续期,覆盖超过一小时的长任务;mark_done 再把它缩短到 STREAM_TTL。
    await redis.xadd(key, {"token": token})
    await redis.expire(key, THREAD_RUNNING_TTL_SECONDS)


async def append_existing(prompt_id: str, token: str) -> None:
    """Do not silently recreate a stream whose earlier events were lost."""
    result = await _client().eval(
        "if redis.call('EXISTS', KEYS[1]) == 0 then return false end "
        "local id = redis.call('XADD', KEYS[1], '*', 'token', ARGV[1]); "
        "redis.call('EXPIRE', KEYS[1], ARGV[2]); return id",
        1,
        _stream_key(prompt_id),
        token,
        THREAD_RUNNING_TTL_SECONDS,
    )
    if not result:
        raise RuntimeError("Run event stream was lost")


async def stream_exists(prompt_id: str) -> bool:
    return bool(await _client().exists(_stream_key(prompt_id)))


async def finish_existing_stream(prompt_id: str) -> None:
    await append_existing(prompt_id, DONE_TOKEN)
    await _client().expire(_stream_key(prompt_id), STREAM_TTL_SECONDS)


async def mark_done(prompt_id: str) -> None:
    redis = _client()

    key = _stream_key(prompt_id)
    await redis.xadd(key, {"token": DONE_TOKEN})
    await redis.expire(key, STREAM_TTL_SECONDS)
    logger.debug(f"Marked stream '{key}' as DONE with TTL={STREAM_TTL_SECONDS}s")


async def read_stream(
    prompt_id: str,
    block_ms: int = 100,
    start_id: str = "0-0",
) -> AsyncGenerator[tuple[str, str], None]:
    redis = _client()

    key = _stream_key(prompt_id)
    last_id = start_id
    waiting_since = time.monotonic()

    while True:
        entries = await redis.xread({key: last_id}, count=100, block=block_ms)

        if not entries:
            exists = await redis.exists(key)
            if not exists:
                if time.monotonic() - waiting_since > STREAM_MISSING_TIMEOUT_SECONDS:
                    logger.warning(f"Stream '{key}' does not exist or expired; giving up")
                    return
                await asyncio.sleep(0.2)  # block_ms 很小时避免空转
                continue
            break
        waiting_since = time.monotonic()

        for _stream_name, messages in entries:
            for entry_id, fields in messages:
                last_id = entry_id
                token = fields.get("token", "")

                if token == DONE_TOKEN:
                    logger.debug(f"Received DONE signal for stream '{key}'")
                    return

                yield entry_id, token


async def incr_with_ttl(key: str, ttl_seconds: int) -> int:
    """原子自增计数;首次创建时设置过期,用于固定窗口限流。"""
    redis = _client()

    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, ttl_seconds, nx=True)
    count, _ = await pipe.execute()
    return int(count)


async def purge_session(session_id: str) -> None:
    """删除会话相关的全部 Redis 键:线程状态、prompt 登记集合,以及这些 prompt 的事件流。"""
    redis = _client()

    session_key = _session_key(session_id)
    prompt_ids = await redis.smembers(session_key)
    keys = [_thread_status_key(session_id), session_key, *map(_stream_key, prompt_ids)]
    await redis.delete(*keys)
