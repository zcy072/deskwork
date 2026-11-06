"""会话沙箱的存储配额:统计会话目录占用,判断超限后哪些命令仍可执行。"""

import os
import re
import shlex
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
