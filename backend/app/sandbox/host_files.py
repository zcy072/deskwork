"""经 bind mount 在宿主侧读写会话文件的底层操作(同步,调用方放进线程)。

调用方先把路径解析到本会话目录内;这里再从可信根(会话根或全局 skills 根)起逐段用
O_NOFOLLOW 打开目录 fd,最后一段也带 O_NOFOLLOW 并核对是普通文件,解析之后才换上的符号链接
(无论在哪一层)都会被拒绝。会话目录对 agent 可写,所有写入都按"目标可能是它预先放好的链接"处理。
"""

import errno
import os
import secrets
import shutil
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
    try:
        relative = host.relative_to(root)
    except ValueError:
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


@contextmanager
def directory_fd(root: Path, host: Path):
    """Open a directory below a trusted root without following any symlink on the way."""
    if host == root.resolve():
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    else:
        with parent_fd(root, host) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def read_regular_file(host: Path, root: Path) -> bytes:
    """root 必须是可信根(会话目录或全局 skills),不能由 host 的可变父目录推导。"""
    try:
        with parent_fd(root, host) as (directory, name):
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


@contextmanager
def open_regular(root: Path, host: Path):
    """只读打开可信根下的普通文件:逐段 O_NOFOLLOW,末段不是普通文件就拒绝。"""
    with parent_fd(root, host) as (directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Not a regular file", str(host))
        fh = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with fh:
        yield fh


def copy_regular_atomic(source: Path, target: Path, source_root: Path, target_root: Path) -> None:
    """复制并原子替换 target,全程持有文件与目录描述符,不按可变路径重新打开。

    临时文件与目标在同一个已打开的目录里创建与重命名;保留源文件权限位和时间戳(同 copy2)。
    """
    with open_regular(source_root, source) as src:
        st = os.fstat(src.fileno())
        with parent_fd(target_root, target) as (directory, name):
            temporary = f".delivery-{secrets.token_hex(12)}"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(fd, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                    dst.flush()
                    os.fchmod(dst.fileno(), stat.S_IMODE(st.st_mode))
                    os.utime(dst.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
                    written = os.fstat(dst.fileno())
                _publish(directory, temporary, name, written)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)


def _same_inode(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _publish(directory: int, temporary: str, name: str, written: os.stat_result) -> None:
    """按名字 rename 前后都核对 inode:临时名被换成别的东西就不发布,发布后被换掉就撤回。"""
    if not _same_inode(os.stat(temporary, dir_fd=directory, follow_symlinks=False), written):
        raise OSError(errno.EBUSY, "Temporary file was replaced during copy", temporary)
    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    published = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if not _same_inode(published, written):
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=directory)
        raise OSError(errno.EBUSY, "Destination was replaced during publish", name)


def list_dir(host: Path, root: Path) -> str:
    """类似 ls -l 的列表;符号链接显示为链接,不展开。目录从可信根逐段打开。"""
    lines = []
    try:
        with directory_fd(root, host) as fd:
            names = sorted(os.listdir(fd))
            for entry in names:
                try:
                    st = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                    name = entry + ("/" if stat.S_ISDIR(st.st_mode) else "")
                    if stat.S_ISLNK(st.st_mode):
                        name += f" -> {os.readlink(entry, dir_fd=fd)}"
                except OSError:
                    continue  # 列目录期间被删掉
                mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                lines.append(f"{stat.filemode(st.st_mode)} {st.st_size:>10} {mtime} {name}")
    except (OSError, ValueError) as exc:
        raise ContainerFileSystemError(f"Failed to list directory: {exc}") from exc
    return "\n".join(lines) or "(empty directory)"


def write_file(host_path: Path, data: bytes, mkdir_parents: bool, root: Path) -> None:
    """覆盖写入;新建的文件和目录放宽权限,agent 用户才能继续改。"""
    try:
        with parent_fd(root, host_path, create=mkdir_parents) as (
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
