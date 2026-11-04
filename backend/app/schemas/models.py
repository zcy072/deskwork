import re
from typing import Annotated

from pydantic import BaseModel, Field

# session_id 会被拼进文件路径(sandbox_data/sessions/<id>)和 rm -rf 命令,
# 因此只允许字母、数字、下划线、连字符,且长度受限。
SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)

SessionId = Annotated[
    str,
    Field(pattern=SESSION_ID_PATTERN, description="The session ID of the chat"),
]


def validate_session_id(session_id: str) -> str:
    """校验 session_id,不合法时抛 ValueError。"""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(
            "Invalid session_id: only letters, digits, '_' and '-' are allowed (1-64 chars)"
        )
    return session_id


class StreamEventsRequest(BaseModel):
    session_id: SessionId
    prompt_id: str = Field(
        ..., max_length=64, pattern=r"^[A-Za-z0-9-]+$", description="The prompt ID of the chat"
    )
    # Redis 把 BLOCK 0 当成永久阻塞,所以下限是 1。XREAD 阻塞必须明显小于 Redis 的 socket_timeout(REDIS_SOCKET_TIMEOUT_SECONDS),否则 SSE 会因读超时中断。
    block_ms: int = Field(
        default=100, ge=1, le=4000, description="The block size in milliseconds (1-4000)"
    )
    start_id: str = Field(
        default="0-0", pattern=r"^\d+(-\d+)?$", description="The start ID of the chat"
    )
