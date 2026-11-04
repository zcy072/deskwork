"""工具权限分级:按工具名配置 allow / confirm / deny。

纯逻辑(无 IO)。未配置的工具不受影响(仍走各自原有的确认逻辑,如高风险 bash、非只读 MCP)。
TOOL_POLICY 是限制性配置,写错时不能静默当成"不限制":启动时校验(见 lifespan),
运行中若仍解析失败则一律按 deny 处理。
"""

import json
from collections.abc import Iterable
from functools import lru_cache

from app.config import settings

LEVELS = ("allow", "confirm", "deny")


def parse_policy(raw: str) -> dict[str, str]:
    """TOOL_POLICY(JSON)-> {工具名: 级别}。格式不对或级别不合法时抛 ValueError。"""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"TOOL_POLICY 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("TOOL_POLICY 必须是 JSON 对象:工具名 -> allow | confirm | deny")
    bad = {k: v for k, v in data.items() if v not in LEVELS}
    if bad:
        raise ValueError(f"TOOL_POLICY 中的级别不合法(只能是 {' / '.join(LEVELS)}): {bad}")
    return {str(k): v for k, v in data.items()}


@lru_cache(maxsize=4)
def _cached(raw: str) -> dict[str, str] | None:
    try:
        return parse_policy(raw)
    except ValueError:
        return None


def load_policy() -> dict[str, str] | None:
    """当前生效的策略;配置无效时返回 None。每次工具调用都会用到,按原始字符串缓存。"""
    return _cached(settings.TOOL_POLICY)


def policy_for(tool_name: str) -> str | None:
    """返回该工具的配置级别;未配置返回 None。配置无效时按 deny 处理(宁可拦错,不可放过)。"""
    policy = load_policy()
    if policy is None:
        return "deny"
    return policy.get(tool_name)


def unknown_tools(registered: Iterable[str]) -> list[str]:
    """策略里出现、但没有注册的工具名(多半是拼错了)。"""
    names = set(registered)
    return sorted(set(load_policy() or {}) - names)
