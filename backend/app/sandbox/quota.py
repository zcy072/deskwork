"""会话沙箱的存储配额:统计会话目录占用,判断超限后哪些命令仍可执行。"""

import hashlib
import os
import re
import shlex
import time
from pathlib import Path

MB = 1024 * 1024

# 超限后仍允许执行的命令(用来腾空间或查看占用);其余一律拒绝
_CLEANUP_COMMANDS = frozenset(
    {"rm", "rmdir", "unlink", "ls", "du", "find", "cd", "pwd", "echo", "true", "wc"}
)
_SEPARATORS = re.compile(r"&&|\|\||;|\n|\|")


class QuotaExceededError(ValueError):
    """会话存储配额不足。"""


def dir_size(root: Path) -> int:
    """目录下所有普通文件的字节数之和;不跟随符号链接,统计过程中文件消失就忽略。

    硬链接只算一次(未变的回退快照是上一份的硬链接,不额外占盘)。
    """
    total = 0
    linked: set[tuple[int, int]] = set()
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            st = entry.stat(follow_symlinks=False)
                            if st.st_nlink > 1:
                                if (st.st_dev, st.st_ino) in linked:
                                    continue
                                linked.add((st.st_dev, st.st_ino))
                            total += st.st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


# scratchpad 下的转换中间目录:用完即删,不进回退快照
SNAPSHOT_EXCLUDED = ("_preview", "_rag", "_review")


def snapshot_manifest(session_root: Path) -> tuple[int, str]:
    """output / scratchpad 的 (总字节数, 内容指纹),不含 SNAPSHOT_EXCLUDED。

    指纹由每个条目的路径、类型、权限、大小、mtime(符号链接取目标)算出;
    目录的 mtime 不算在内(中间目录的增删会改动它)。不跟随符号链接。
    """
    total = 0
    entries: list[str] = []
    for top in ("output", "scratchpad"):
        stack = [(session_root / top, top)]
        while stack:
            current, rel = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        path = f"{rel}/{entry.name}"
                        if rel == "scratchpad" and entry.name in SNAPSHOT_EXCLUDED:
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                            if entry.is_symlink():
                                entries.append(f"l {path} {os.readlink(entry.path)}")
                            elif entry.is_dir(follow_symlinks=False):
                                entries.append(f"d {path} {st.st_mode:o}")
                                stack.append((Path(entry.path), path))
                            else:
                                total += st.st_size
                                entries.append(
                                    f"f {path} {st.st_mode:o} {st.st_size} {st.st_mtime_ns}"
                                )
                        except OSError:
                            continue
            except OSError:
                continue
    entries.sort()
    return total, hashlib.sha256("\0".join(entries).encode(errors="surrogateescape")).hexdigest()


def session_usage(session_root: Path) -> int:
    """会话占用:uploads / output / scratchpad 三个目录之和(回退快照单独限额,不计入)。"""
    return sum(dir_size(session_root / name) for name in ("uploads", "output", "scratchpad"))


def is_cleanup_command(command: str) -> bool:
    """每一段子命令的第一个词都在清理白名单里才算。含重定向、命令替换一律不算。"""
    if any(token in command for token in (">", "<", "$(", "`")):
        return False
    segments = [seg.strip() for seg in _SEPARATORS.split(command) if seg.strip()]
    if not segments:
        return False
    for seg in segments:
        try:
            words = shlex.split(seg)
        except ValueError:
            return False
        if not words or os.path.basename(words[0]) not in _CLEANUP_COMMANDS:
            return False
        # find 的 -exec / -delete 之外的写操作很少见,但 -fprint 等会写文件
        if words[0] == "find" and any(w.startswith("-f") or w == "-exec" for w in words):
            return False
    return True


def over_quota_message(used: int, limit_mb: int) -> str:
    return (
        f"Session storage quota exceeded ({used // MB} MB used, limit {limit_mb} MB). "
        "Delete files you no longer need under /workspace/output or /workspace/scratchpad "
        "(e.g. rm, du -sh) before running other commands."
    )


def snapshot_name(prompt_id: str, *, suffix_only: bool = False) -> str:
    """快照目录名:零填充的纳秒时间戳 + prompt_id,按名字排序就是按时间排序。"""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", prompt_id):
        raise ValueError("invalid prompt_id")
    return f"_{prompt_id}" if suffix_only else f"{time.time_ns():020d}_{prompt_id}"
