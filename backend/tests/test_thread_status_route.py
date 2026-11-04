"""/thread-status 返回运行中的 prompt_id,供刷新页面后的客户端接回事件流。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from app.api import security
from app.api.routes import stream
from app.config import settings


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "API_KEYS", "")

    async def one(key, ttl):
        return 1

    monkeypatch.setattr(security, "incr_with_ttl", one)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _status(monkeypatch, value, prompt_id=None):
    async def get_status(session_id):
        return value

    monkeypatch.setattr(stream, "get_thread_status", get_status)
    monkeypatch.setattr(stream, "get_active_prompt_id", lambda session_id: prompt_id)


async def test_running_returns_prompt_id(client, monkeypatch):
    _status(monkeypatch, "running", "p1")
    r = await client.get("/thread-status/s1")
    assert r.status_code == 200
    assert r.json() == {"session_id": "s1", "status": "running", "prompt_id": "p1"}


async def test_finished_has_no_prompt_id(client, monkeypatch):
    _status(monkeypatch, "complete", "stale")
    r = await client.get("/thread-status/s1")
    assert r.json()["prompt_id"] is None


async def test_unknown_session_404(client, monkeypatch):
    _status(monkeypatch, None)
    assert (await client.get("/thread-status/s1")).status_code == 404
