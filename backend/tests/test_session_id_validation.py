"""session_id 会被拼进文件路径和 rm -rf,必须在入口和路径层都被拦住。"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import app
from app.sandbox.session_paths import SessionPaths
from app.schemas import StreamEventsRequest, validate_session_id

BAD_IDS = ["../x", "a/b", "..", "a b", "", "x" * 65, "a;rm -rf /", "id\n", "中文"]


@pytest.mark.parametrize("good", [str(uuid.uuid4()), "abc_123-XYZ", "a", "x" * 64])
def test_valid_ids(good):
    assert validate_session_id(good) == good


@pytest.mark.parametrize("bad", BAD_IDS)
def test_invalid_ids_rejected(bad):
    with pytest.raises(ValueError):
        validate_session_id(bad)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_session_paths_refuses_bad_ids(bad, tmp_path):
    with pytest.raises(ValueError):
        SessionPaths.create(bad, sessions_root=tmp_path)


@pytest.mark.parametrize("bad", ["../x", "a b", "x" * 65])
def test_stream_events_model_rejects_bad_id(bad):
    with pytest.raises(ValueError):
        StreamEventsRequest(session_id=bad, prompt_id="p")


@pytest.fixture
def client():
    return TestClient(app)


def test_api_rejects_bad_session_id_everywhere(client):
    assert (
        client.post("/send-message", data={"session_id": "a b", "user_query": "hi"}).status_code
        == 422
    )
    assert client.get("/thread-status/a%20b").status_code == 422
    assert client.post("/thread-cancel/a%20b").status_code == 422
    r = client.get(
        "/files/download",
        params={"session_id": "../other", "relative_path": "uploads/x"},
    )
    assert r.status_code == 422
