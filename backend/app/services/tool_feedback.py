"""工具错误反馈:只有明确写给模型看的错误原文透出,其余异常记日志后换成带 trace id 的通用提示。"""

from app.sandbox.errors import ContainerFileSystemError, SandboxStopUnconfirmed
from app.utils import get_trace_id, logger


class ToolFeedbackError(RuntimeError):
    """信息本身就是给模型看的(文件不存在、格式不支持等),可以原样放进工具结果。"""


def error_feedback(tool: str, exc: Exception) -> str:
    if isinstance(exc, SandboxStopUnconfirmed):
        raise exc
    if isinstance(exc, (ToolFeedbackError, ContainerFileSystemError)):
        logger.warning("%s failed: %s", tool, exc)
        return f"Error: {exc}"
    logger.error("%s failed", tool, exc_info=exc)
    return f"Error: {tool} 执行失败(trace id: {get_trace_id()})"
