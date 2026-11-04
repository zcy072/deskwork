import time

from fastapi import APIRouter, Depends, Response

from app.agents import check_llm_health
from app.agents.llms import active_llm_info
from app.api.security import rate_limit_default, require_api_key
from app.db import db_health
from app.utils import redis_health

internals_router = APIRouter(tags=["internals"])

LLM_CACHE_SECONDS = 30
_llm_cache: list = []  # [时间戳, 结果]


async def _status(response: Response, services: dict, start_time: float, **extra) -> dict:
    is_healthy = all(services.values())
    response.status_code = 200 if is_healthy else 503
    return {
        "status": "healthy" if is_healthy else "unhealthy",
        "timestamp": start_time,
        "services": services,
        "response_time_ms": round((time.time() - start_time) * 1000, 2),
        **extra,
    }


@internals_router.get("/health")
async def health_check(response: Response):
    """存活/就绪检查(公开,供探针使用):只查 Redis 和数据库,不暴露模型配置。"""
    start_time = time.time()
    services = {"redis": await redis_health(), "database": await db_health()}
    return await _status(response, services, start_time)


@internals_router.get(
    "/health/deep",
    dependencies=[Depends(require_api_key), Depends(rate_limit_default)],
)
async def deep_health_check(response: Response):
    """需要认证:真实调用一次 LLM(耗时且收费),并返回当前模型配置。结果缓存一小段时间。"""
    start_time = time.time()
    services = {"redis": await redis_health(), "database": await db_health()}
    cached = _llm_cache
    if cached and start_time - cached[0] < LLM_CACHE_SECONDS:
        services["llm"] = cached[1]
    else:
        services["llm"] = await check_llm_health()
        _llm_cache[:] = [start_time, services["llm"]]
    return await _status(response, services, start_time, llm_config=active_llm_info())
