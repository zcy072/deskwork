from types import SimpleNamespace

import pytest

from app.agents.middlewares import LoggingModelFallbackMiddleware


def _m(name):
    return SimpleNamespace(name=name)


class _Req:
    def __init__(self, model):
        self.model = model

    def override(self, model):
        return _Req(model)


async def test_switches_to_first_working_fallback():
    seen = []

    async def handler(req):
        seen.append(req.model.name)
        if req.model.name in ("primary", "bad"):
            raise RuntimeError("503")
        return SimpleNamespace(ok=req.model.name)

    mw = LoggingModelFallbackMiddleware(_m("bad"), _m("good"), _m("never"))
    out = await mw.awrap_model_call(_Req(_m("primary")), handler)
    assert out.ok == "good"
    assert seen == ["primary", "bad", "good"]


async def test_raises_last_error_when_all_fail():
    async def handler(_req):
        raise RuntimeError("down")

    mw = LoggingModelFallbackMiddleware(_m("a"))
    with pytest.raises(RuntimeError, match="down"):
        await mw.awrap_model_call(_Req(_m("p")), handler)


async def test_primary_success_does_not_touch_fallbacks():
    calls = []

    async def handler(req):
        calls.append(req.model.name)
        return SimpleNamespace(ok=True)

    await LoggingModelFallbackMiddleware(_m("a")).awrap_model_call(_Req(_m("p")), handler)
    assert calls == ["p"]
