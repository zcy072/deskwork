"""/files/download 路径穿越与正常下载、/stream-events 的 404 与空流、send-message 的 500 不外泄。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from app.api import security
from app.api.routes import stream
from app.config import settings
from app.sandbox import session_paths


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "API_KEYS", "")
    monkeypatch.setattr(stream, "is_db_ready", lambda: False)

    async def one(key, ttl):
        return 1

    monkeypatch.setattr(security, "incr_with_ttl", one)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(session_paths, "SESSION_ROOT", tmp_path / "sessions")
    r = tmp_path / "sessions" / "s1"
    for d in ("output", "uploads", "scratchpad"):
        (r / d).mkdir(parents=True)
    (r / "output" / "ok.txt").write_text("hello")
    (r / "scratchpad" / "secret.txt").write_text("secret")
    (tmp_path / "outside.txt").write_text("outside")
    return r


def _get(client, rel):
    return client.get("/files/download", params={"session_id": "s1", "relative_path": rel})


async def test_download_happy_path(client, root):
    r = await _get(client, "output/ok.txt")
    assert r.status_code == 200 and r.content == b"hello"
    assert "attachment" in r.headers["content-disposition"]


@pytest.mark.parametrize(
    "rel",
    [
        "output/../scratchpad/secret.txt",
        "../outside.txt",
        "output/../../outside.txt",
        "/etc/passwd",
        "scratchpad/secret.txt",
    ],
)
async def test_download_rejects_traversal(client, root, rel):
    assert (await _get(client, rel)).status_code == 400


async def test_download_missing_file_is_404(client, root):
    assert (await _get(client, "output/nope.txt")).status_code == 404


async def test_stream_events_unknown_prompt_is_404(client, monkeypatch):
    async def no(_s, _p):
        return False

    monkeypatch.setattr("app.services.chat_operations.prompt_belongs_to_session", no)
    r = await client.post("/stream-events", json={"session_id": "s1", "prompt_id": "p1"})
    assert r.status_code == 404


async def test_stream_events_empty_stream_returns_empty_body(client, monkeypatch):
    async def yes(_s, _p):
        return True

    async def empty(*_a, **_k):
        return
        yield  # pragma: no cover

    monkeypatch.setattr("app.services.chat_operations.prompt_belongs_to_session", yes)
    monkeypatch.setattr("app.services.chat_operations.read_stream", empty)
    r = await client.post("/stream-events", json={"session_id": "s1", "prompt_id": "p1"})
    assert r.status_code == 200 and r.text == ""
    assert r.headers["content-type"].startswith("text/event-stream")


async def test_send_message_500_does_not_leak_exception(client, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("/srv/private/db.sqlite exploded")

    monkeypatch.setattr(stream, "start_chat", boom)
    r = await client.post("/send-message", data={"session_id": "s1", "user_query": "hi"})
    assert r.status_code == 500
    assert "private" not in r.text and "trace id" in r.json()["detail"]


async def test_create_memory_shares_service_validation(client, tmp_path, monkeypatch):
    from app.services.memory import MemoryRejected, save_memory

    with pytest.raises(MemoryRejected):
        await save_memory("   ", None, "")
    with pytest.raises(MemoryRejected):
        await save_memory("长" * 301, None, "")
