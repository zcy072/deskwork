import asyncio
import time
from collections.abc import AsyncGenerator

import redis.asyncio as aioredis

from app.config import settings
from app.utils.custom_logger import logger

STREAM_PREFIX = "stream:"
STREAM_TTL_SECONDS = 300
# 运行中的流每次写入都续期到这个时长;结束后缩短到 STREAM_TTL_SECONDS
STREAM_RUNNING_TTL_SECONDS = 3600
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


async def add_token(prompt_id: str, token: str) -> None:
    redis = _client()

    key = _stream_key(prompt_id)
    # 运行中也带过期:进程被杀、来不及结束时流不会永久留在 Redis 里。
    # 每次写入都续期,覆盖超过一小时的长任务;finish_existing_stream 再把它缩短到 STREAM_TTL。
    await redis.xadd(key, {"token": token})
    await redis.expire(key, STREAM_RUNNING_TTL_SECONDS)


async def append_existing(prompt_id: str, token: str) -> None:
    """Do not silently recreate a stream whose earlier events were lost."""
    result = await _client().eval(
        "if redis.call('EXISTS', KEYS[1]) == 0 then return false end "
        "local id = redis.call('XADD', KEYS[1], '*', 'token', ARGV[1]); "
        "redis.call('EXPIRE', KEYS[1], ARGV[2]); return id",
        1,
        _stream_key(prompt_id),
        token,
        STREAM_RUNNING_TTL_SECONDS,
    )
    if not result:
        raise RuntimeError("Run event stream was lost")


async def stream_exists(prompt_id: str) -> bool:
    return bool(await _client().exists(_stream_key(prompt_id)))


async def finish_existing_stream(prompt_id: str) -> None:
    await append_existing(prompt_id, DONE_TOKEN)
    await _client().expire(_stream_key(prompt_id), STREAM_TTL_SECONDS)


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


async def purge_streams(prompt_ids: list[str]) -> None:
    """删除会话各轮的事件流;run id 来自数据库,流本身也带过期,漏删最多多留一小时。"""
    if prompt_ids:
        await _client().delete(*map(_stream_key, prompt_ids))
