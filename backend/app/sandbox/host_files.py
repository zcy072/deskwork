"""经 bind mount 在宿主侧读写会话文件的底层操作(同步,调用方放进线程)。

调用方先把路径解析到本会话目录内;这里再用 O_NOFOLLOW 拒绝解析之后才换上的符号链接,
缩小 TOCTOU 窗口。会话目录对 agent 可写,所有写入都按"目标可能是它预先放好的链接"处理。
"""

import os
import stat
import time
from pathlib import Path

from app.sandbox.errors import ContainerFileSystemError
from app.sandbox.quota import MB

# 文件工具(view_file、str_replace 等)一次读取的上限;更大的文件用 bash 查看
MAX_READ_BYTES = 50 * 1024 * 1024


def read_regular_file(host: Path) -> bytes:
    try:
        fd = os.open(host, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ContainerFileSystemError(f"Failed to read file: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise ContainerFileSystemError("Not a regular file")
        data = fh.read(MAX_READ_BYTES + 1)
    if len(data) > MAX_READ_BYTES:
        raise ContainerFileSystemError(
            f"File is larger than {MAX_READ_BYTES // MB} MB; "
            "use bash_tool (head, grep, sed -n) to inspect it"
        )
    return data


def list_dir(host: Path) -> str:
    """类似 ls -l 的列表;符号链接显示为链接,不展开。"""
    try:
        with os.scandir(host) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as exc:
        raise ContainerFileSystemError(f"Failed to list directory: {exc.strerror}") from exc
    lines = []
    for entry in entries:
        try:
            st = entry.stat(follow_symlinks=False)
            name = entry.name + ("/" if stat.S_ISDIR(st.st_mode) else "")
            if stat.S_ISLNK(st.st_mode):
                name += f" -> {os.readlink(entry.path)}"
        except OSError:
            continue  # 列目录期间被删掉
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
        lines.append(f"{stat.filemode(st.st_mode)} {st.st_size:>10} {mtime} {name}")
    return "\n".join(lines) or "(empty directory)"


def write_file(host_path: Path, data: bytes, mkdir_parents: bool) -> None:
    """覆盖写入;新建的文件和目录放宽权限,agent 用户才能继续改。"""
    try:
        if mkdir_parents:
            missing = []
            parent = host_path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            host_path.parent.mkdir(parents=True, exist_ok=True)
            for d in missing:  # agent 用户需要能在新目录里继续写
                d.chmod(0o777)
        existed = host_path.exists()
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        fd = os.open(host_path, flags, 0o666)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if not existed:
            host_path.chmod(0o666)
    except OSError as exc:
        raise ContainerFileSystemError(f"Failed to write file: {exc}") from exc


def create_upload(host_dest: Path, data: bytes) -> None:
    """只新建(O_EXCL),已存在或是链接就失败。"""
    host_dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(host_dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o665)
    with os.fdopen(fd, "wb") as fh:
        os.fchmod(fd, 0o665)  # 不受 umask 影响
        fh.write(data)
