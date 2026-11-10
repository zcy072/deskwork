"""经 bind mount 在宿主侧读写会话文件的底层操作(同步,调用方放进线程)。

调用方先把路径解析到本会话目录内;这里再用 O_NOFOLLOW 拒绝解析之后才换上的符号链接,
缩小 TOCTOU 窗口。会话目录对 agent 可写,所有写入都按"目标可能是它预先放好的链接"处理。
"""

import os
import secrets
import stat
import time
from contextlib import contextmanager, suppress
from pathlib import Path

from app.sandbox.errors import ContainerFileSystemError
from app.sandbox.quota import MB

# 文件工具(view_file、str_replace 等)一次读取的上限;更大的文件用 bash 查看
MAX_READ_BYTES = 50 * 1024 * 1024


@contextmanager
def parent_fd(root: Path, host: Path, *, create: bool = False):
    """Walk from a trusted root without following any mutable directory symlink."""
    relative = host.relative_to(root.resolve())
    if not relative.parts or ".." in relative.parts:
        raise ValueError("Invalid file path")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, 0o777, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, relative.name
    finally:
        os.close(fd)


def read_regular_file(host: Path, root: Path | None = None) -> bytes:
    try:
        with parent_fd(root or host.parent, host) as (directory, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
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


def write_file(host_path: Path, data: bytes, mkdir_parents: bool, root: Path | None = None) -> None:
    """覆盖写入;新建的文件和目录放宽权限,agent 用户才能继续改。"""
    try:
        with parent_fd(root or host_path.parent, host_path, create=mkdir_parents) as (
            directory,
            name,
        ):
            mode = 0o666
            try:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode):
                    raise ContainerFileSystemError("Not a regular file")
                mode = stat.S_IMODE(current.st_mode)
            except FileNotFoundError:
                pass
            temporary = f".write-{secrets.token_hex(12)}"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                mode,
                dir_fd=directory,
            )
            try:
                with os.fdopen(fd, "wb") as fh:
                    os.fchmod(fh.fileno(), mode)
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
    except OSError as exc:
        raise ContainerFileSystemError(f"Failed to write file: {exc}") from exc


def create_upload(host_dest: Path, data: bytes) -> None:
    """只新建(O_EXCL),已存在或是链接就失败。"""
    host_dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(host_dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o665)
    with os.fdopen(fd, "wb") as fh:
        os.fchmod(fd, 0o665)  # 不受 umask 影响
        fh.write(data)
