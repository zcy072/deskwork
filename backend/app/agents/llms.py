import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values
from langchain_core.language_models.chat_models import BaseChatModel

from app.config import settings
from app.utils import logger

# Providers that speak the OpenAI-compatible protocol.
_OPENAI_COMPATIBLE = ("openai", "deepseek", "ollama", "vllm")
# Only these may leave the base URL empty. "openai" empty -> the SDK's official endpoint.
# deepseek / ollama / vllm must set it explicitly: the same provider name can be the vendor's
# own endpoint, a relay or a self-hosted gateway, so a guessed default would silently send
# the key and traffic to the wrong place.
_BASE_URL_OPTIONAL = ("openai",)

_ANTHROPIC_FIXED_SAMPLING_PREFIXES = ("claude-fable-5", "claude-sonnet-5-5", "claude-opus-5")


@dataclass(frozen=True)
class LLMProfile:
    name: str
    provider: str
    api_key: str
    base_url: str | None
    model: str


def _env(name: str) -> str:
    """读取 profile 变量。容器里由 compose 的 env_file 注入 os.environ;本地从 ../.env 读取。"""
    if name in os.environ:
        return os.environ[name]
    for path in (Path("../.env"), Path(".env")):
        if path.is_file():
            value = dotenv_values(path).get(name)
            if value is not None:
                return value
    return ""


def load_profile(name: str | None) -> LLMProfile:
    """name 为空 -> 默认的 LLM_* 配置;否则读取 LLM_<NAME>_* 命名配置。

    命名配置的 provider 取 LLM_<NAME>_PROVIDER,缺省时用 name 本身(deepseek / anthropic 等)。
    """
    if not name:
        return LLMProfile(
            name="default",
            provider=settings.LLM_PROVIDER.strip().lower(),
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL.strip() or None,
            model=settings.LLM_MODEL_NAME,
        )
    key = name.strip().upper().replace("-", "_")
    api_key, model = _env(f"LLM_{key}_API_KEY"), _env(f"LLM_{key}_MODEL_NAME")
    if not api_key or not model:
        raise ValueError(f"LLM profile '{name}' 需要设置 LLM_{key}_API_KEY 和 LLM_{key}_MODEL_NAME")
    return LLMProfile(
        name=name.strip().lower(),
        provider=(_env(f"LLM_{key}_PROVIDER") or name).strip().lower(),
        api_key=api_key,
        base_url=_env(f"LLM_{key}_BASE_URL").strip() or None,
        model=model,
    )


def protocol_mismatch(profile: LLMProfile) -> str | None:
    """Claude 模型名搭配 OpenAI 协议的 provider 时返回告警文案,否则 None。

    中转站按协议路由:同一个 claude-* 名字走 Anthropic 协议是 Claude,走 OpenAI 协议
    可能被悄悄转给别的模型(真实遇到过 gpt-5.6-sol)。配置写错时不会有任何报错。
    """
    if profile.provider in _OPENAI_COMPATIBLE and profile.model.lower().startswith("claude"):
        return (
            f"LLM profile '{profile.name}': 模型名 '{profile.model}' 看起来是 Claude,"
            f"但 provider='{profile.provider}' 走 OpenAI 协议,中转站可能把它路由到别的模型。"
            "如需 Claude,请设 provider=anthropic"
        )
    return None


def base_url_missing(profile: LLMProfile) -> str | None:
    """需要显式 base URL 的 provider 没配时返回错误文案,否则 None。"""
    if profile.provider in _OPENAI_COMPATIBLE and profile.provider not in _BASE_URL_OPTIONAL:
        if not profile.base_url:
            var = (
                "LLM_BASE_URL"
                if profile.name == "default"
                else f"LLM_{profile.name.upper()}_BASE_URL"
            )
            return (
                f"LLM profile '{profile.name}': provider='{profile.provider}' 必须显式设置 {var}"
                "(官方、中转站、自建网关的地址不同,不会替你猜默认值)"
            )
    return None


def build_model(profile: LLMProfile | None = None) -> BaseChatModel:
    profile = profile or load_profile(None)
    provider, base_url = profile.provider, profile.base_url
    if problem := base_url_missing(profile):
        raise ValueError(problem)
    if warning := protocol_mismatch(profile):
        # 只告警不拒绝:自建网关用 OpenAI 协议暴露 Claude 是合法配置
        logger.error(warning)

    if provider in _OPENAI_COMPATIBLE:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            api_key=profile.api_key,
            base_url=base_url,
            model=profile.model,
            temperature=settings.LLM_TEMPERATURE,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        # The Anthropic SDK appends /v1/messages itself, so base_url has no /v1.
        kwargs = {}
        # Claude 5 系列只接受默认采样参数,显式传 temperature 会直接报错
        if not profile.model.startswith(_ANTHROPIC_FIXED_SAMPLING_PREFIXES):
            kwargs["temperature"] = settings.LLM_TEMPERATURE
        return ChatAnthropic(
            api_key=profile.api_key,
            base_url=base_url,
            model=profile.model,
            max_tokens=settings.LLM_MAX_TOKENS,
            **kwargs,
        )

    if provider == "azure":
        from langchain_openai import AzureChatOpenAI

        return AzureChatOpenAI(
            api_key=profile.api_key,
            azure_endpoint=base_url,
            api_version=settings.LLM_API_VERSION,
            model=profile.model,
            temperature=settings.LLM_TEMPERATURE,
        )

    raise ValueError(
        f"Unknown LLM provider '{provider}'. "
        "Use one of: openai, anthropic, deepseek, ollama, vllm, azure"
    )


def fallback_profile_names() -> list[str]:
    return [n.strip() for n in settings.LLM_FALLBACKS.split(",") if n.strip()]


@lru_cache(maxsize=1)
def get_model() -> BaseChatModel:
    """首次使用时才构造模型,避免 import 阶段就依赖 LLM 配置。"""
    profile = load_profile(settings.LLM_PROFILE.strip() or None)
    logger.info(
        "LLM profile=%s provider=%s model=%s", profile.name, profile.provider, profile.model
    )
    return build_model(profile)


@lru_cache(maxsize=4)
def get_named_model(name: str) -> tuple[LLMProfile, BaseChatModel]:
    """按命名配置构造并缓存模型(看图、产物自检用),复用同一个 HTTP 连接池。"""
    profile = load_profile(name)
    return profile, build_model(profile)


@lru_cache(maxsize=1)
def get_fallback_models() -> list[BaseChatModel]:
    """LLM_FALLBACKS 里列出的备用模型(按顺序)。配置不完整的 profile 会被跳过并告警。"""
    models = []
    for name in fallback_profile_names():
        try:
            profile = load_profile(name)
            models.append(build_model(profile))
            logger.info("LLM fallback profile=%s model=%s", profile.name, profile.model)
        except Exception as exc:
            logger.warning("忽略无效的备用模型 '%s': %s", name, exc)
    return models


def active_llm_info() -> dict:
    """当前生效的主模型配置(不构造模型、不发请求),供 /health 和评测对照。"""
    try:
        profile = load_profile(settings.LLM_PROFILE.strip() or None)
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "profile": profile.name,
        "provider": profile.provider,
        "model": profile.model,
        "warning": base_url_missing(profile) or protocol_mismatch(profile),
    }


async def check_llm_health():
    try:
        # 流式调用:部分 Anthropic 中转站的非流式 /v1/messages 返回 200 但响应体为空
        stream = get_model().astream("hi")
        try:
            async for _ in stream:
                return True
            return False
        finally:
            await stream.aclose()  # 拿到首个 chunk 就够了,及时关掉底层连接
    except Exception as e:
        logger.error(f"LLM health check failed: {e}")
        return False
