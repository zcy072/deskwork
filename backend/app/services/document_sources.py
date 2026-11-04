"""可预览的检索来源:限定会话文件范围,用内容摘要验证引用仍指向同一份文件。"""

import hashlib

from app.sandbox.session_paths import SessionPaths

MAX_SOURCE_BYTES = 50 * 1024 * 1024


def read_source(paths: SessionPaths, relative_path: str) -> bytes:
    host = paths.resolve_host_path(relative_path)
    root = paths.session_root.resolve()
    if not any(host.is_relative_to(root / name) for name in ("uploads", "output")):
        raise ValueError("Invalid source path")
    data = paths.read_host_file(host, MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("引用文件超过 50 MB,请下载后查看")
    return data


def source_fingerprint(paths: SessionPaths, relative_path: str) -> str:
    return hashlib.sha256(read_source(paths, relative_path)).hexdigest()
