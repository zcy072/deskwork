"""回退快照:快照目录命名、内容指纹,以及在沙箱容器内执行的快照 / 恢复脚本。

脚本由 BashSession.snapshot / restore_snapshot 经 sandbox-manager 的 /exec 以 agent 用户执行。
快照放在会话目录的 snapshots/ 下(不在 bwrap 挂载里,agent 的命令碰不到)。
"""

import hashlib
import os
import re
import shlex
import time
from pathlib import Path

# scratchpad 下的转换中间目录:用完即删,不进回退快照
SNAPSHOT_EXCLUDED = ("_preview", "_rag", "_review")

# 恢复脚本找不到对应快照时的退出码
RESTORE_MISSING = 3


def snapshot_name(prompt_id: str, *, suffix_only: bool = False) -> str:
    """快照目录名:零填充的纳秒时间戳 + prompt_id,按名字排序就是按时间排序。"""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", prompt_id):
        raise ValueError("invalid prompt_id")
    return f"_{prompt_id}" if suffix_only else f"{time.time_ns():020d}_{prompt_id}"


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


def snapshot_script(root: str, prompt_id: str, fingerprint: str, keep: int) -> str:
    """给 output / scratchpad 做一份快照,只保留最近 keep 份。

    内容与最新一份快照的指纹相同时只建硬链接副本,不再复制。
    先写到 .tmp_ 目录再改名,复制失败不会留下残缺的快照。
    """
    if keep < 1:
        # head -n -0 会列出全部条目,刚建好的快照也会被删掉
        raise ValueError("keep must be at least 1")
    name = shlex.quote(snapshot_name(prompt_id))
    tmp = shlex.quote(f".tmp_{snapshot_name(prompt_id, suffix_only=True)}")
    excludes = " ".join(f"--exclude=scratchpad/{d}" for d in SNAPSHOT_EXCLUDED)
    return (
        "set -e; set -o pipefail; export LC_ALL=C; "
        f"mkdir -p {root}/snapshots; cd {root}/snapshots; rm -rf .tmp_*; "
        "L=$(ls -1 | tail -n 1); "
        f'if [ -n "$L" ] && [ "$(cat "$L"/.manifest 2>/dev/null)" = {fingerprint} ]; then '
        f'cp -al "$L" {tmp}; '
        f"else mkdir {tmp}; "
        f"tar -C {root} --anchored {excludes} -cf - output scratchpad | tar -C {tmp} -xpf -; "
        f"echo {fingerprint} > {tmp}/.manifest; fi; "
        f"mv {tmp} {name}; "
        f"ls -1 | head -n -{keep} | xargs -r rm -rf"
    )


def restore_script(root: str, prompt_id: str) -> str:
    """把 output / scratchpad 换成该轮的快照,并删掉这一轮及之后的快照。

    没有对应快照时以 RESTORE_MISSING 退出,不动文件。
    """
    suffix = shlex.quote(snapshot_name(prompt_id, suffix_only=True))
    return (
        "export LC_ALL=C; "
        f"cd {root}/snapshots 2>/dev/null || exit {RESTORE_MISSING}; "
        f'T=$(ls -1 | grep -F -- {suffix} | tail -n 1); [ -n "$T" ] || exit {RESTORE_MISSING}; '
        "set -e; "
        f"find {root}/output -mindepth 1 -delete; find {root}/scratchpad -mindepth 1 -delete; "
        f'cp -a "$T"/output/. {root}/output/; cp -a "$T"/scratchpad/. {root}/scratchpad/; '
        "ls -1 | awk -v t=\"$T\" '$0 >= t' | xargs -r rm -rf"
    )
