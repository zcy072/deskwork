"""FastAPI 应用的冒烟测试:路由是否注册、参数校验是否生效(不碰 Redis/LLM/Docker)。"""

import pytest
from fastapi.testclient import TestClient

from app import app


@pytest.fixture
def client():
    # 不进入 lifespan,避免连接 Redis
    return TestClient(app)


def test_routes_registered():
    paths = {route.path for route in app.routes}
    for expected in (
        "/send-message",
        "/stream-events",
        "/thread-status/{session_id}",
        "/thread-cancel/{session_id}",
        "/files/download",
        "/health",
    ):
        assert expected in paths


def test_send_message_requires_session_id(client):
    assert client.post("/send-message", data={"user_query": "hi"}).status_code == 422


def test_stream_events_validates_body(client):
    assert client.post("/stream-events", json={}).status_code == 422


def test_download_rejects_invalid_prefix(client):
    r = client.get("/files/download", params={"session_id": "s", "relative_path": "scratchpad/x"})
    assert r.status_code == 400


def test_responses_carry_trace_id(client):
    r = client.get("/files/download", params={"session_id": "s", "relative_path": "bad"})
    assert r.headers["X-Trace-ID"].startswith("G-")


def _healthy(monkeypatch):
    from app.api.routes import internals

    async def ok():
        return True

    monkeypatch.setattr(internals, "redis_health", ok)
    monkeypatch.setattr(internals, "db_health", ok)
    internals._llm_cache.clear()
    return internals


def test_public_health_hides_llm_config_and_never_calls_llm(client, monkeypatch):
    internals = _healthy(monkeypatch)

    async def boom():
        raise AssertionError("public /health must not call the LLM")

    monkeypatch.setattr(internals, "check_llm_health", boom)
    body = client.get("/health", params={"deep": "true"}).json()
    assert "llm_config" not in body and "llm" not in body["services"]


def test_deep_health_reports_active_llm_config_and_caches(client, monkeypatch):
    internals = _healthy(monkeypatch)
    calls = []

    async def fake_llm():
        calls.append(1)
        return True

    monkeypatch.setattr(internals, "check_llm_health", fake_llm)
    body = client.get("/health/deep").json()
    assert {"profile", "provider", "model", "warning"} <= set(body["llm_config"])
    assert body["services"]["llm"] is True
    client.get("/health/deep")
    assert len(calls) == 1


def test_deep_health_requires_api_key(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(type(settings), "api_keys", property(lambda self: ["k"]))
    assert client.get("/health/deep").status_code == 401


def test_delivered_endpoint(client, monkeypatch, tmp_path):
    import hashlib

    from app.api.routes import stream
    from app.sandbox.session_paths import SessionPaths

    paths = SessionPaths.create("s", tmp_path)
    (paths.session_root / "versions" / "a.txt").mkdir(parents=True)
    (paths.session_root / "versions" / "a.txt" / "100").write_bytes(b"old")
    (paths.session_root / "output").mkdir(parents=True, exist_ok=True)
    (paths.session_root / "output" / "a.txt").write_bytes(b"new")
    monkeypatch.setattr(stream.SessionPaths, "create", staticmethod(lambda _sid: paths))

    def get(data: bytes):
        sha = hashlib.sha256(data).hexdigest()
        return client.get(
            "/files/delivered", params={"session_id": "s", "file_name": "a.txt", "sha256": sha}
        )

    assert get(b"old").content == b"old"
    assert get(b"new").content == b"new"
    assert get(b"gone").status_code == 410
    bad = client.get(
        "/files/delivered", params={"session_id": "s", "file_name": "a.txt", "sha256": "xyz"}
    )
    assert bad.status_code == 422
