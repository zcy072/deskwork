"""prompt caching:Anthropic 模型的请求带 cache_control,其他模型原样放行。"""

from langchain.agents.middleware import ModelRequest
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage

mw = AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore")


async def _seen(model):
    seen = {}

    async def handler(req):
        seen["req"] = req
        return "ok"

    req = ModelRequest(
        model=model,
        messages=[HumanMessage(content="hi")],
        system_message=SystemMessage(content="sys"),
        tools=[],
        model_settings={},
    )
    await mw.awrap_model_call(req, handler)
    return seen["req"]


async def test_anthropic_request_gets_cache_control():
    req = await _seen(ChatAnthropic(model="claude-sonnet-5-5", api_key="x"))
    assert req.model_settings["cache_control"]["type"] == "ephemeral"
    assert any("cache_control" in b for b in req.system_message.content_blocks)


async def test_other_models_untouched():
    req = await _seen(FakeListChatModel(responses=["x"]))
    assert "cache_control" not in req.model_settings
