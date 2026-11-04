"""错误信息不外泄、host_path 不外泄、running 快照合并、running 状态续期。"""

import json

import pytest

from app.services import chat_operations as ops
from app.services import history
from app.services.budget import BudgetExceeded
from app.services.history import TurnRecorder
from app.utils import redis_utils


class _Agent:
    def __init__(self, exc):
        self.exc = exc

    async def aget_state(self, _config):
        class S:
            values = {"messages": []}
            interrupts = ()
            config = {"configurable": {"checkpoint_id": ""}}

        return S()

    def astream(self, *_a, **_k):
        exc = self.exc

        async def gen():
            raise exc
            yield  # pragma: no cover

        return gen()


@pytest.fixture
def pushed(monkeypatch):
    events: list[tuple[str, dict]] = []

    async def push(_pid, event, data):
        events.append((event, data))

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(ops, "_push", push)
    monkeypatch.setattr(ops, "set_thread_status", noop)
    monkeypatch.setattr(ops, "persist_assistant_turn", noop)
    monkeypatch.setattr(ops, "mark_done", noop)
    monkeypatch.setattr(ops, "refresh_running_ttl", noop)
    return events


async def test_internal_error_is_not_leaked(pushed, monkeypatch):
    monkeypatch.setattr(
        ops, "get_agent", lambda: _Agent(RuntimeError("/srv/secret/path sqlite error"))
    )
    await ops._run_agent("p", "s", "q")
    msg = next(d["message"] for e, d in pushed if e == "error")
    assert "secret" not in msg and "trace id" in msg


async def test_budget_error_is_passed_verbatim(pushed, monkeypatch):
    monkeypatch.setattr(ops, "get_agent", lambda: _Agent(BudgetExceeded("已达到上限,请拆小任务")))
    await ops._run_agent("p", "s", "q")
    assert next(d["message"] for e, d in pushed if e == "error") == "已达到上限,请拆小任务"


async def test_save_failure_event_has_code(pushed, monkeypatch):
    async def not_saved(*_a, **_k):
        return False

    async def noop(*_a, **_k):
        return None

    async def stream(*_a, **_k):
        if False:
            yield None

    class _Ok(_Agent):
        def astream(self, *_a, **_k):
            return stream()

    monkeypatch.setattr(ops, "get_agent", lambda: _Ok(None))
    monkeypatch.setattr(ops, "persist_assistant_turn", not_saved)
    monkeypatch.setattr(ops, "_record_outcome", noop)
    await ops._run_agent("p", "s", "q")
    err = next(d for e, d in pushed if e == "error")
    assert err["code"] == "history_save_failed" and "历史记录" in err["message"]


def test_files_presented_strips_host_path():
    event, data = ops._normalize_custom(
        {
            "kind": "files.presented",
            "files": [{"file_name": "a", "host_path": "/srv/x/a", "container_path": "/w/a"}],
        }
    )
    assert event == "files.presented"
    assert data["files"] == [{"file_name": "a", "container_path": "/w/a"}]


async def test_start_chat_events_exclude_host_path(monkeypatch):
    sent = []

    async def push(_pid, event, data):
        sent.append(json.dumps(data))

    async def noop(*_a, **_k):
        return None

    async def status(_sid):
        return "complete"

    async def upload(_sid, _u):
        return [{"file_name": "a", "container_path": "/w/a", "host_path": "/srv/a"}]

    persisted = []

    async def persist_user(_s, _p, _q, files, _o=""):
        persisted.append(files)

    monkeypatch.setattr(ops, "_push", push)
    monkeypatch.setattr(ops, "get_thread_status", status)
    monkeypatch.setattr(ops, "set_thread_status", noop)
    monkeypatch.setattr(ops, "register_prompt", noop)
    monkeypatch.setattr(ops, "persist_user_turn", persist_user)
    monkeypatch.setattr(ops, "upload_session_files", upload)
    monkeypatch.setattr(ops, "_launch_agent_task", lambda *a, **k: None)
    monkeypatch.setattr(ops.CONTAINER_MANAGER, "snapshot_session", noop)
    ops._starting_sessions.discard("s9")
    await ops.start_chat("s9", "hi", [])
    assert sent and all("host_path" not in s for s in sent)
    assert persisted == [[{"file_name": "a", "container_path": "/w/a"}]]


async def test_running_snapshots_are_coalesced_but_final_always_written(monkeypatch):
    writes = []

    class Msg:
        id = 7

    async def save(*a, **k):
        writes.append((k["status"], k.get("message_id")))
        return Msg()

    monkeypatch.setattr(history, "is_db_ready", lambda: True)
    monkeypatch.setattr(history.repository, "save_assistant_message", save)
    r = TurnRecorder()
    r.record("ai.token", {"text": "x"})
    await history.persist_assistant_turn("s", "p", r, "running")
    await history.persist_assistant_turn("s", "p", r, "running")  # 间隔太短,跳过
    await history.persist_assistant_turn("s", "p", r, "complete")  # 终态必写
    assert writes == [("running", None), ("complete", 7)]


class _Redis:
    def __init__(self, value):
        self.value, self.expired = value, []

    async def get(self, _k):
        return self.value

    def pipeline(self, **_k):
        redis = self

        class _Pipe:
            def expire(self, k, ttl):
                redis.expired.append((k, ttl))

            async def execute(self):
                return []

        return _Pipe()


async def test_refresh_running_ttl_only_extends_running(monkeypatch):
    r = _Redis("running")
    monkeypatch.setattr(redis_utils, "_redis", r)
    await redis_utils.refresh_running_ttl("s")
    ttl = redis_utils.THREAD_RUNNING_TTL_SECONDS
    assert r.expired == [("thread-status:s", ttl), (redis_utils._session_key("s"), ttl)]
    r2 = _Redis("complete")
    monkeypatch.setattr(redis_utils, "_redis", r2)
    await redis_utils.refresh_running_ttl("s")
    assert r2.expired == []
