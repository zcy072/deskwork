"""认证、限流、CORS 行为(不依赖真实 Redis)。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from app.api import security
from app.config import settings


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """默认让限流计数永远为 1,具体用例再覆盖。"""

    async def one(key, ttl):
        return 1

    monkeypatch.setattr(security, "incr_with_ttl", one)


def _keys(monkeypatch, value="secret-1,secret-2"):
    monkeypatch.setattr(settings, "API_KEYS", value)


async def test_auth_disabled_when_no_keys(client, monkeypatch):
    _keys(monkeypatch, "")
    try:
        r = await client.get("/thread-status/s1")
    except RuntimeError:
        return  # 越过认证进入业务层(无 Redis)
    assert r.status_code != 401


async def test_missing_key_rejected(client, monkeypatch):
    _keys(monkeypatch)
    r = await client.get("/thread-status/s1")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


async def test_wrong_key_rejected(client, monkeypatch):
    _keys(monkeypatch)
    r = await client.get("/thread-status/s1", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [{"Authorization": "Bearer secret-2"}, {"X-API-Key": "secret-1"}],
)
async def test_valid_key_passes_auth(client, monkeypatch, headers):
    _keys(monkeypatch)
    # 通过认证后进入业务层;无 Redis 时可能 500,但绝不是 401/429
    try:
        r = await client.get(
            "/files/download", params={"session_id": "s", "relative_path": "x"}, headers=headers
        )
    except RuntimeError:
        return
    assert r.status_code not in (401, 429)


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/send-message"),
        ("post", "/stream-events"),
        ("post", "/thread-cancel/s1"),
        ("get", "/thread-status/s1"),
        ("get", "/files/download"),
        ("get", "/conversations"),
        ("delete", "/conversations/s1"),
    ],
)
async def test_protected_routes_require_key(client, monkeypatch, method, path):
    _keys(monkeypatch)
    assert (await getattr(client, method)(path)).status_code == 401


async def test_health_stays_public(client, monkeypatch):
    _keys(monkeypatch)
    from app.api.routes import internals

    async def ok():
        return True

    calls = []

    async def llm():
        calls.append(1)
        return True

    monkeypatch.setattr(internals, "redis_health", ok)
    monkeypatch.setattr(internals, "check_llm_health", llm)
    monkeypatch.setattr(internals, "db_health", ok)
    r = await client.get("/health")
    assert r.status_code == 200
    assert "llm" not in r.json()["services"] and not calls  # 默认不调用 LLM
    # 公开的 /health 即使带 deep=true 也不会调用 LLM
    r = await client.get("/health?deep=true")
    assert "llm" not in r.json()["services"] and not calls
    # 深度检查需要认证
    internals._llm_cache.clear()
    assert (await client.get("/health/deep")).status_code == 401
    r = await client.get("/health/deep", headers={"Authorization": "Bearer secret-1"})
    assert r.json()["services"]["llm"] is True and calls


async def test_rate_limit_returns_429_with_retry_after(client, monkeypatch):
    _keys(monkeypatch, "")

    async def over(key, ttl):
        return settings.RATE_LIMIT_PER_MINUTE + 1

    monkeypatch.setattr(security, "incr_with_ttl", over)
    r = await client.get("/conversations")
    assert r.status_code == 429
    assert 1 <= int(r.headers["retry-after"]) <= 60


async def test_send_message_has_stricter_limit(client, monkeypatch):
    _keys(monkeypatch, "")
    seen = {}

    async def record(key, ttl):
        seen[key.split(":")[1]] = True
        return settings.RATE_LIMIT_SEND_PER_MINUTE + 1 if ":send:" in key else 1

    monkeypatch.setattr(security, "incr_with_ttl", record)
    r = await client.post("/send-message", data={"session_id": "s1", "user_query": "hi"})
    assert r.status_code == 429
    assert "send" in seen


async def test_limiter_fails_open_when_redis_down(client, monkeypatch):
    _keys(monkeypatch, "")

    async def boom(key, ttl):
        raise ConnectionError("redis down")

    monkeypatch.setattr(security, "incr_with_ttl", boom)
    try:
        r = await client.get("/conversations")
    except RuntimeError:
        return  # 数据库未初始化,说明已越过限流
    assert r.status_code != 429


def test_client_id_uses_key_hash_not_plaintext():
    class Req:
        headers = {"authorization": "Bearer abc"}
        client = None

    cid = security._client_id(Req())
    assert cid.startswith("k:") and "abc" not in cid


async def test_cors_allows_configured_origin_only(client):
    ok = await client.options(
        "/conversations",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-credentials" not in ok.headers

    bad = await client.options(
        "/conversations",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in bad.headers


def test_redis_url_encodes_password_and_display_url_hides_it(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "p@ss/word:1")
    assert settings.redis_url == "redis://:p%40ss%2Fword%3A1@localhost:6379/0"
    assert (
        "p@ss" not in settings.redis_display_url
        and "ss" not in settings.redis_display_url.split("//")[1]
    )
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "")
    assert settings.redis_url == settings.redis_display_url


async def _post_files(client, monkeypatch, files):
    _keys(monkeypatch, "")

    async def under_limit(key, ttl):
        return 1

    monkeypatch.setattr(security, "incr_with_ttl", under_limit)
    return await client.post(
        "/send-message", data={"session_id": "s1", "user_query": "hi"}, files=files
    )


async def test_upload_auth_checked_before_body_is_parsed(client, monkeypatch):
    """未认证的上传在解析 multipart 之前就被拒,请求体不会落到临时文件。"""
    _keys(monkeypatch)
    from starlette import formparsers

    def no_parse(*_a, **_k):
        raise AssertionError("multipart body parsed before auth")

    monkeypatch.setattr(formparsers.MultiPartParser, "parse", no_parse)
    files = [("files", ("a.txt", b"x", "text/plain"))]
    for path in ("/send-message", "/conversations/import"):
        r = await client.post(path, files=files, data={"session_id": "s1"})
        assert r.status_code == 401


async def test_upload_too_many_files_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_MAX_FILES", 2)
    files = [("files", (f"f{i}.txt", b"x", "text/plain")) for i in range(3)]
    assert (await _post_files(client, monkeypatch, files)).status_code == 413


async def test_upload_oversized_file_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_MAX_FILE_MB", 1)
    files = [("files", ("big.bin", b"x" * (1024 * 1024 + 1), "application/octet-stream"))]
    assert (await _post_files(client, monkeypatch, files)).status_code == 413


async def test_upload_total_over_limit_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_MAX_FILE_MB", 1)
    monkeypatch.setattr(settings, "UPLOAD_MAX_TOTAL_MB", 1)
    half = b"x" * (700 * 1024)
    files = [("files", (f"f{i}.bin", half, "application/octet-stream")) for i in range(2)]
    assert (await _post_files(client, monkeypatch, files)).status_code == 413


async def test_failed_auth_is_rate_limited(client, monkeypatch):
    _keys(monkeypatch)
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MINUTE", 3)
    seen = []

    async def count(key, ttl):
        seen.append(key)
        return 4

    monkeypatch.setattr(security, "incr_with_ttl", count)
    r = await client.get("/thread-status/s1", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 429
    assert any(k.startswith("ratelimit:authfail:ip:") for k in seen)


async def test_download_sets_nosniff_and_sandbox_csp(client, monkeypatch, tmp_path):
    from app.api.routes import stream

    (tmp_path / "output").mkdir()
    page = tmp_path / "output" / "a.html"
    page.write_text("<script>1</script>")

    class P:
        session_root = tmp_path

        def resolve_host_path(self, _rel):
            return page

    monkeypatch.setattr(stream.SessionPaths, "create", staticmethod(lambda _sid: P()))
    _keys(monkeypatch, "")
    r = await client.get(
        "/files/download", params={"session_id": "s", "relative_path": "output/a.html"}
    )
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "sandbox"


def _req(headers, host="10.0.0.5"):
    from types import SimpleNamespace

    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


def test_shared_key_behind_proxy_gets_per_ip_buckets(monkeypatch):
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
    a = security._client_id(_req({"authorization": "Bearer k", "x-real-ip": "1.1.1.1"}))
    b = security._client_id(_req({"authorization": "Bearer k", "x-real-ip": "2.2.2.2"}))
    assert a != b


def test_proxy_header_ignored_unless_trusted(monkeypatch):
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", False)
    a = security._client_id(_req({"authorization": "Bearer k", "x-real-ip": "1.1.1.1"}))
    b = security._client_id(_req({"authorization": "Bearer k", "x-real-ip": "2.2.2.2"}))
    assert a == b  # 不可信时伪造 X-Real-IP 不能换桶
    assert security._ip_id(_req({"x-real-ip": "9.9.9.9"})) == "ip:10.0.0.5"
