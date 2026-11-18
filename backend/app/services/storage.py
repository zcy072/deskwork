"""会话存储:分类别统计占用,删除 uploads/ 或 output/ 下的单个文件。

配额(session_usage)只算 uploads / output / scratchpad;这里额外把版本副本和回退快照
列出来,让用户看清磁盘上实际占了多少,但不改变配额口径。
"""

import errno
import os
import shutil
import stat
from pathlib import Path

from app.sandbox.host_files import parent_fd
from app.sandbox.quota import dir_size
from app.sandbox.session_paths import SessionPaths
from app.services.file_versions import VERSIONS_DIR

SNAPSHOTS_DIR = "snapshots"
DELETABLE_DIRS = ("uploads", "output")
# 计入配额的类别;其余只展示
QUOTA_CATEGORIES = ("uploads", "output", "scratchpad")


def storage_breakdown(paths: SessionPaths) -> dict[str, int]:
    root = paths.session_root
    return {
        name: dir_size(root / name) if (root / name).is_dir() else 0
        for name in (*QUOTA_CATEGORIES, VERSIONS_DIR, SNAPSHOTS_DIR)
    }


MAX_LISTED = 5000


def list_session_files(paths: SessionPaths) -> list[str]:
    """uploads/ 与 output/ 下现有的普通文件(相对会话根的路径),不跟随符号链接。"""
    root = paths.session_root
    found: list[str] = []
    for top in DELETABLE_DIRS:
        stack = [root / top]
        while stack and len(found) < MAX_LISTED:
            try:
                with os.scandir(stack.pop()) as it:
                    for entry in it:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            found.append(
                                f"{top}/{Path(entry.path).relative_to(root / top).as_posix()}"
                            )
            except OSError:
                continue
    return sorted(found)


def delete_session_file(paths: SessionPaths, relative_path: str) -> int:
    """删除 uploads/ 或 output/ 下的一个文件,返回释放的字节数。

    抛 ValueError(路径不合法或不在允许的目录)、FileNotFoundError。
    目标本身是符号链接时只删链接,不会跟随到别处。历史版本不动(它们是独立副本,单独占用)。
    """
    root = paths.session_root.resolve()
    parts = Path(relative_path).parts
    if len(parts) < 2 or parts[0] not in DELETABLE_DIRS or ".." in parts:
        raise ValueError("只能删除上传和产出目录里的文件")
    # 沙箱里的后台进程可能随时把中间目录换成符号链接:逐级用 O_NOFOLLOW 打开目录,
    # 再相对目录 fd 删除,检查和删除之间不会被换到会话外
    try:
        with parent_fd(root, root.joinpath(*parts)) as (fd, name):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                size = 0
            elif stat.S_ISREG(st.st_mode):
                size = st.st_size
            else:
                raise FileNotFoundError(relative_path)
            os.unlink(name, dir_fd=fd)
            return size
    except FileNotFoundError:
        raise FileNotFoundError(relative_path) from None
    except OSError as exc:
        # 中间目录是符号链接(O_NOFOLLOW 报 ELOOP,加 O_DIRECTORY 时报 ENOTDIR)或是普通文件
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError("文件路径无效") from exc
        raise


def clear_history_versions(paths: SessionPaths) -> int:
    """删除所有文件的历史版本,返回释放的字节数。

    保留每个文件的 .current(最近一次交付的副本):它是下次覆盖时的对照基准,
    也是历史消息按内容摘要找回交付版本的依据。
    """
    base = paths.session_root / VERSIONS_DIR
    freed = 0
    try:
        directories = [d for d in base.iterdir() if d.is_dir() and not d.is_symlink()]
    except OSError:
        return 0
    for directory in directories:
        for p in directory.iterdir():
            if p.name.isdigit() and p.is_file() and not p.is_symlink():
                try:
                    size = p.stat().st_size
                    p.unlink()
                except OSError:
                    continue
                freed += size
    return freed


def clear_scratchpad(paths: SessionPaths) -> int:
    """清空 scratchpad/ 下的全部内容(目录本身保留),返回释放的字节数。

    符号链接只删链接本身,不跟随到别处。个别条目删除失败时跳过,其余照常清理。
    """
    root = paths.session_root / "scratchpad"
    if root.is_symlink() or not root.is_dir():
        return 0
    freed = 0
    for entry in list(root.iterdir()):
        try:
            if entry.is_symlink() or entry.is_file():
                size = 0 if entry.is_symlink() else entry.stat().st_size
                entry.unlink()
            elif entry.is_dir():
                size = dir_size(entry)
                shutil.rmtree(entry)
            else:
                continue
        except OSError:
            continue
        freed += size
    return freed
