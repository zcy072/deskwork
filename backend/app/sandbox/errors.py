class ContainerFileSystemError(Exception):
    """Raised when a container file operation fails."""


class TransientSandboxError(Exception):
    """沙箱基础设施的瞬时故障(manager 5xx、连接中断等),幂等操作可以重试。"""
