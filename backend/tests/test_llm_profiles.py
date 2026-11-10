import pytest

from app.agents import llms
from app.config import settings


def test_default_profile_uses_plain_llm_settings():
    p = llms.load_profile(None)
    assert p.name == "default"
    assert p.model == settings.LLM_MODEL_NAME


def test_named_profile_reads_prefixed_variables(monkeypatch):
    monkeypatch.setenv("LLM_DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("LLM_DEEPSEEK_MODEL_NAME", "ds-model")
    monkeypatch.setenv("LLM_DEEPSEEK_BASE_URL", "")  # 屏蔽本机 ../.env 里的真实配置
    p = llms.load_profile("deepseek")
    assert (p.provider, p.model, p.api_key, p.base_url) == ("deepseek", "ds-model", "k", None)


def test_profile_provider_and_base_url_overridable(monkeypatch):
    monkeypatch.setenv("LLM_RELAYX_API_KEY", "k")
    monkeypatch.setenv("LLM_RELAYX_MODEL_NAME", "m")
    monkeypatch.setenv("LLM_RELAYX_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_RELAYX_BASE_URL", "https://relay.example")
    p = llms.load_profile("relayx")
    assert (p.provider, p.base_url) == ("anthropic", "https://relay.example")


def test_incomplete_profile_is_an_error(monkeypatch):
    monkeypatch.delenv("LLM_NOPE_API_KEY", raising=False)
    monkeypatch.setattr(llms, "_env", lambda n: "")
    with pytest.raises(ValueError, match="LLM_NOPE_API_KEY"):
        llms.load_profile("nope")


def test_invalid_fallbacks_are_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr(settings, "LLM_FALLBACKS", " ghost , ")
    monkeypatch.setattr(llms, "_env", lambda n: "")
    llms.get_fallback_models.cache_clear()
    assert llms.get_fallback_models() == []
    llms.get_fallback_models.cache_clear()


def test_claude5_models_are_built_without_temperature():
    """Claude 5 系列显式传 temperature 会在调用时抛 ValueError(真实验证中发现)。"""
    p = llms.LLMProfile("a", "anthropic", "k", None, "claude-sonnet-5-5")
    assert llms.build_model(p).temperature is None
    old = llms.LLMProfile("b", "anthropic", "k", None, "claude-haiku-4-5-20251001")
    assert llms.build_model(old).temperature == settings.LLM_TEMPERATURE


def test_protocol_mismatch_flags_claude_over_openai_protocol():
    """中转站按协议路由:claude-* 走 OpenAI 协议会被悄悄转给别的模型(真实遇到过 gpt-5.6-sol)。"""
    bad = llms.LLMProfile("default", "openai", "k", None, "claude-sonnet-5-5")
    assert "anthropic" in llms.protocol_mismatch(bad)
    for provider in ("deepseek", "ollama", "vllm"):
        assert llms.protocol_mismatch(llms.LLMProfile("d", provider, "k", None, "Claude-X"))
    assert (
        llms.protocol_mismatch(llms.LLMProfile("a", "anthropic", "k", None, "claude-sonnet-5-5"))
        is None
    )
    assert llms.protocol_mismatch(llms.LLMProfile("o", "openai", "k", None, "gpt-4o")) is None
    assert (
        llms.protocol_mismatch(llms.LLMProfile("s", "deepseek", "k", None, "deepseek-chat")) is None
    )


def test_build_model_logs_error_on_mismatch_but_still_builds(monkeypatch):
    logged = []
    monkeypatch.setattr(llms.logger, "error", lambda msg, *a: logged.append(msg))
    model = llms.build_model(llms.LLMProfile("default", "openai", "k", None, "claude-sonnet-5-5"))
    assert model is not None and logged and "claude-sonnet-5-5" in logged[0]


def test_active_llm_info_reports_warning(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROFILE", "")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(settings, "LLM_MODEL_NAME", "claude-sonnet-5-5")
    info = llms.active_llm_info()
    assert info["model"] == "claude-sonnet-5-5" and info["provider"] == "openai" and info["warning"]
    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    assert llms.active_llm_info()["warning"] is None


def test_base_url_required_for_non_official_openai_protocols():
    """provider 名不能推出地址:官方、中转站、自建网关都可能,不替用户猜默认值。"""
    for provider in ("deepseek", "ollama", "vllm"):
        p = llms.LLMProfile("default", provider, "k", None, "m")
        assert "LLM_BASE_URL" in llms.base_url_missing(p)
        with pytest.raises(ValueError, match="LLM_BASE_URL"):
            llms.build_model(p)
        assert (
            llms.base_url_missing(llms.LLMProfile("default", provider, "k", "http://x/v1", "m"))
            is None
        )
    # openai 留空 = SDK 官方地址,是唯一允许留空的
    assert llms.base_url_missing(llms.LLMProfile("default", "openai", "k", None, "gpt-4o")) is None


def test_named_profile_error_names_its_own_variable():
    p = llms.LLMProfile("deepseek", "deepseek", "k", None, "m")
    assert "LLM_DEEPSEEK_BASE_URL" in llms.base_url_missing(p)


def test_relay_deepseek_uses_given_base_url():
    p = llms.LLMProfile("deepseek", "deepseek", "k", "https://relay.example/v1", "deepseek-chat")
    assert llms.build_model(p).openai_api_base == "https://relay.example/v1"


async def test_check_llm_health_uses_streaming(monkeypatch):
    class Streaming:
        async def astream(self, _prompt):
            yield "chunk"

        async def ainvoke(self, _prompt):
            raise AssertionError("健康检查不能走非流式调用")

    monkeypatch.setattr(llms, "get_model", lambda: Streaming())
    assert await llms.check_llm_health() is True


async def test_check_llm_health_false_on_empty_stream_or_error(monkeypatch):
    class Empty:
        async def astream(self, _prompt):
            return
            yield

    class Broken:
        def astream(self, _prompt):
            raise RuntimeError("boom")

    for model in (Empty(), Broken()):
        monkeypatch.setattr(llms, "get_model", lambda m=model: m)
        assert await llms.check_llm_health() is False


def test_named_model_is_built_once_per_profile(monkeypatch):
    monkeypatch.setattr(
        llms, "_env", lambda n: {"LLM_VIS_API_KEY": "k", "LLM_VIS_MODEL_NAME": "m"}.get(n, "")
    )
    built = []
    monkeypatch.setattr(llms, "build_model", lambda p: built.append(p) or object())
    llms.get_named_model.cache_clear()
    first = llms.get_named_model("vis")
    assert llms.get_named_model("vis") is first
    assert len(built) == 1 and first[0].model == "m"
    llms.get_named_model.cache_clear()


def test_anthropic_max_tokens_comes_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "LLM_MAX_TOKENS", 32000)
    p = llms.LLMProfile("default", "anthropic", "k", "https://relay.example", "claude-sonnet-5-5")
    assert llms.build_model(p).max_tokens == 32000


def test_model_request_timeout_is_explicit_for_all_protocols(monkeypatch):
    monkeypatch.setattr(settings, "LLM_REQUEST_TIMEOUT_SECONDS", 12)
    monkeypatch.setattr(settings, "LLM_API_VERSION", "2024-10-21")
    openai = llms.build_model(llms.LLMProfile("o", "openai", "test", None, "test"))
    anthropic = llms.build_model(
        llms.LLMProfile("a", "anthropic", "test", None, "claude-sonnet-5-5")
    )
    azure = llms.build_model(
        llms.LLMProfile("z", "azure", "test", "https://test.openai.azure.com", "test")
    )
    assert openai.request_timeout == azure.request_timeout == 12
    assert anthropic.default_request_timeout == 12
