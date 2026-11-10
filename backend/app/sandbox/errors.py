class ContainerFileSystemError(Exception):
    """Raised when a container file operation fails."""


class TransientSandboxError(Exception):
    """沙箱基础设施的瞬时故障(manager 5xx、连接中断等),幂等操作可以重试。"""


class SandboxStopUnconfirmed(ContainerFileSystemError):
    """沙箱进程没能确认已经停止;在确认之前不能再执行命令或改动文件。"""
