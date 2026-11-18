"""事件流的 TTL、清理,以及 read_stream 对不存在的流不再无限空转(基线 #8)。"""

import pytest

from app.utils import redis_utils


class FakeRedis:
    async def xread(self, *_args, **_kwargs):
        return []

    async def exists(self, _key):
        return 0


@pytest.fixture
def fake(monkeypatch):
    f = FakeRedis()
    monkeypatch.setattr(redis_utils, "_redis", f)
    return f


async def test_read_stream_gives_up_on_missing_stream(fake, monkeypatch):
    monkeypatch.setattr(redis_utils, "STREAM_MISSING_TIMEOUT_SECONDS", 0.3)
    events = [e async for e in redis_utils.read_stream("nope", block_ms=10)]
    assert events == []


async def test_running_stream_outlives_finished_stream(monkeypatch):
    """运行中的流每次写入续期到长 TTL(覆盖 300s+ 的任务),结束后缩短。"""
    expires = []

    class R:
        async def xadd(self, *_a):
            pass

        async def expire(self, key, ttl):
            expires.append(ttl)

        async def eval(self, *_a):
            return "1-0"

    monkeypatch.setattr(redis_utils, "_redis", R())
    await redis_utils.add_token("p", "x")
    await redis_utils.finish_existing_stream("p")
    assert expires[0] >= 3600 > redis_utils.STREAM_TTL_SECONDS == expires[-1]


async def test_purge_streams_deletes_each_run_stream(monkeypatch):
    deleted = []

    class R:
        async def delete(self, *keys):
            deleted.extend(keys)

    monkeypatch.setattr(redis_utils, "_redis", R())
    await redis_utils.purge_streams([])
    await redis_utils.purge_streams(["a", "b"])
    assert deleted == ["stream:a", "stream:b"]
