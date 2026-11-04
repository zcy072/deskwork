"""认证与限流,以 FastAPI 依赖的形式挂到路由上。

- 认证:Authorization: Bearer <key> 或 X-API-Key,与 API_KEYS 常量时间比较。
- 限流:Redis 固定窗口(1 分钟),调用方标识为 API Key 的前缀哈希或客户端 IP;
  TRUST_PROXY_HEADERS 开启时再叠加 X-Real-IP。
  Redis 异常时放行并记日志,限流不应成为单点故障。
"""

import hashlib
import hmac
import time

from fastapi import HTTPException, Request

from app.config import settings
from app.owner import owner_of_key
from app.utils import incr_with_ttl, logger

WINDOW_SECONDS = 60


def _extract_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.headers.get("x-api-key") or None


def _key_matches(candidate: str) -> bool:
    # 逐个比较且不提前退出,避免通过耗时推断匹配到第几个
    matched = False
    for valid in settings.api_keys:
        if hmac.compare_digest(candidate.encode(), valid.encode()):
            matched = True
    return matched


def get_owner(request: Request) -> str:
    """当前请求的数据归属(见 app/owner.py);须在 require_api_key 通过之后使用。"""
    if not settings.api_keys:
        return ""
    key = _extract_key(request)
    return owner_of_key(key) if key else ""


def warn_if_auth_disabled() -> None:
    if not settings.api_keys:
        logger.warning("API_KEYS 未设置:认证已关闭,仅限本地开发使用")


async def require_api_key(request: Request) -> None:
    if not settings.api_keys:
        return
    key = _extract_key(request)
    if key is None or not _key_matches(key):
        # 认证失败也要计数,否则暴力猜 key 完全不受限流约束
        await _check(request, "authfail", settings.RATE_LIMIT_PER_MINUTE, client_id=_ip_id(request))
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _ip_id(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    if settings.TRUST_PROXY_HEADERS:
        host = request.headers.get("x-real-ip", "").strip() or host
    return "ip:" + host


def _client_id(request: Request) -> str:
    key = _extract_key(request)
    if key:
        digest = "k:" + hashlib.sha256(key.encode()).hexdigest()[:16]
        # 反代给所有人注入同一个 Key,只按 Key 分桶就等于全站共用一个桶
        return f"{digest}|{_ip_id(request)}" if settings.TRUST_PROXY_HEADERS else digest
    return _ip_id(request)


async def _check(request: Request, scope: str, limit: int, client_id: str | None = None) -> None:
    window = int(time.time() // WINDOW_SECONDS)
    key = f"ratelimit:{scope}:{client_id or _client_id(request)}:{window}"
    try:
        count = await incr_with_ttl(key, WINDOW_SECONDS * 2)
    except Exception:
        logger.exception("Rate limiter unavailable, allowing request")
        return
    if count > limit:
        retry_after = WINDOW_SECONDS - int(time.time()) % WINDOW_SECONDS
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(retry_after)},
        )


async def rate_limit_default(request: Request) -> None:
    await _check(request, "all", settings.RATE_LIMIT_PER_MINUTE)


async def rate_limit_send(request: Request) -> None:
    await _check(request, "send", settings.RATE_LIMIT_SEND_PER_MINUTE)
