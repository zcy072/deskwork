"""基线 #6(running 状态 TTL)与 #8(read_stream 对不存在的流不再无限空转)。"""

import pytest

from app.utils import redis_utils


class FakeRedis:
    def __init__(self):
        self.set_calls = []

    async def set(self, key, value, ex=None):
        self.set_calls.append((key, value, ex))

    async def xread(self, *_args, **_kwargs):
        return []

    async def exists(self, _key):
        return 0


@pytest.fixture
def fake(monkeypatch):
    f = FakeRedis()
    monkeypatch.setattr(redis_utils, "_redis", f)
    return f


async def test_running_status_outlives_terminal_statuses(fake):
    await redis_utils.set_thread_status("s", "running")
    await redis_utils.set_thread_status("s", "complete")
    running_ttl, complete_ttl = fake.set_calls[0][2], fake.set_calls[1][2]
    assert running_ttl >= 3600 > complete_ttl


async def test_read_stream_gives_up_on_missing_stream(fake, monkeypatch):
    monkeypatch.setattr(redis_utils, "STREAM_MISSING_TIMEOUT_SECONDS", 0.3)
    events = [e async for e in redis_utils.read_stream("nope", block_ms=10)]
    assert events == []


async def test_prompt_registration_outlives_long_runs(monkeypatch):
    """归属记录过期会让 300s+ 任务的客户端重连 404(评测中发现)。"""
    expires = []

    class R:
        async def sadd(self, *_a):
            pass

        async def expire(self, key, ttl):
            expires.append(ttl)

    monkeypatch.setattr(redis_utils, "_redis", R())
    await redis_utils.register_prompt("s", "p")
    assert expires[0] >= 3600 > redis_utils.STREAM_TTL_SECONDS


async def test_recover_orphaned_runs_ends_streams_and_unblocks_session(monkeypatch):
    status = {"thread-status:a": "running", "thread-status:b": "complete"}
    added = []
    ttls = []

    class R:
        async def scan_iter(self, match=None):
            for k in list(status):
                yield k

        async def get(self, key):
            return status[key]

        async def set(self, key, value, ex=None):
            status[key] = value
            ttls.append(ex)

        async def smembers(self, key):
            return {"p1"} if key == "session:a" else set()

        async def exists(self, key):
            return 1

        async def xadd(self, key, fields):
            added.append((key, fields["token"]))

        async def expire(self, *_a):
            pass

    monkeypatch.setattr(redis_utils, "_redis", R())
    assert await redis_utils.recover_orphaned_runs() == 1
    assert status == {"thread-status:a": "error", "thread-status:b": "complete"}
    tokens = [t for k, t in added if k == "stream:p1"]
    assert '"stream.end"' in tokens[1] and tokens[-1] == redis_utils.DONE_TOKEN
    assert ttls[0] < redis_utils.THREAD_RUNNING_TTL_SECONDS  # error 是终态,不能带 running 的长 TTL
