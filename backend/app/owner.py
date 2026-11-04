"""数据归属:每个 API Key 对应一个 owner,会话和长期记忆只对自己的 owner 可见。

owner 是 Key 的 SHA-256 前缀,库里不存明文 Key;未开启认证(API_KEYS 为空)时恒为 ""。
换 Key 等于换了一个用户,旧数据不会跟过去。
"""

import hashlib
from contextvars import ContextVar

# agent 的工具和中间件拿不到 HTTP 请求,由 _run_agent 在启动时设置,子任务自动继承
current_owner: ContextVar[str] = ContextVar("current_owner", default="")


def owner_of_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:32]
