"""按会话限制内存:沙箱容器里常驻的看门狗(以 agent 用户运行,由 tini 托管)。

容器内 cgroup 只读,做不了按会话的 memory cgroup;RLIMIT_AS 又会让 sharp、onnxruntime 启动即崩。
所以分两层:
- 单进程:shell 里 `ulimit -d`(RLIMIT_DATA),大分配直接失败(MemoryError / 堆分配失败);
- 单会话合计:这里每秒按 PID namespace 汇总 PSS(共享页按比例分摊,不会重复计算),
  超过上限就 SIGKILL 这个会话里占用最大的进程。命令随之以 137 退出,shell 本身一般保留。

外层 bwrap 的命令行里有会话目录;沿父进程链找到它,就归到那个会话(嵌套 namespace 里的进程也算)。
一个会话可能同时有常驻 shell 和一次性隔离命令(预览转换),按会话合计。
只看非容器 namespace 的进程:/exec 的快照脚本等不在 bwrap 里,不受这里限制。
容器整体的 mem_limit 仍是最后一道防线(看门狗每秒一次,来不及拦住极快的分配)。
"""

import os
import re
import signal
import sys
import time

LIMIT_MB = int(os.environ.get("SESSION_MAX_MEMORY_MB", "0") or 0)
INTERVAL = float(os.environ.get("MEMWATCH_INTERVAL_SEC", "1"))
SESSION_ARG = re.compile(r"^/sandbox_data/sessions/([A-Za-z0-9_-]{1,64})/")


def log(msg: str) -> None:
    print(f"memwatch: {msg}", file=sys.stderr, flush=True)


def _read(path: str, mode: str = "r"):
    with open(path, mode) as fh:
        return fh.read()


def scan(own_ns: str) -> dict[str, list[tuple[int, int, str]]]:
    """会话 id -> [(pss_kb, pid, comm)],只含 bwrap namespace 里的进程。"""
    ns_of, ppid_of, comm_of, session_of_bwrap = {}, {}, {}, {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            ns_of[pid] = os.readlink(f"/proc/{pid}/ns/pid")
            stat = _read(f"/proc/{pid}/stat")
            comm_of[pid] = stat[stat.index("(") + 1 : stat.rindex(")")]
            ppid_of[pid] = int(stat[stat.rindex(")") + 2 :].split()[1])
            if comm_of[pid] == "bwrap" and ns_of[pid] == own_ns:
                for arg in _read(f"/proc/{pid}/cmdline", "rb").split(b"\0"):
                    m = SESSION_ARG.match(arg.decode(errors="replace"))
                    if m:
                        session_of_bwrap[pid] = m.group(1)
                        break
        except (OSError, ValueError, IndexError):
            continue
    # 沿父进程链找到外层 bwrap 归到会话。不能只认外层 bwrap 子进程的 namespace:会话里可以再
    # unshare 出嵌套的 PID namespace,那里的进程不在任何已知 namespace 中,会被漏算
    session_of_pid: dict[int, str | None] = {}

    def session_of(pid: int) -> str | None:
        chain = []
        while pid not in session_of_pid:
            if pid in session_of_bwrap:
                session_of_pid[pid] = session_of_bwrap[pid]
                break
            parent = ppid_of.get(pid)
            if parent is None or parent == pid or len(chain) > 4096:
                session_of_pid[pid] = None
                break
            chain.append(pid)
            pid = parent
        result = session_of_pid[pid]
        for p in chain:
            session_of_pid[p] = result
        return result

    usage: dict[str, list[tuple[int, int, str]]] = {}
    for pid, ns in ns_of.items():
        if ns == own_ns:
            continue  # 外层 bwrap 本身和容器里的其他进程
        session = session_of(pid)
        if session is None:
            continue
        try:
            for line in _read(f"/proc/{pid}/smaps_rollup").splitlines():
                if line.startswith("Pss:"):
                    usage.setdefault(session, []).append((int(line.split()[1]), pid, comm_of[pid]))
                    break
        except OSError:
            continue
    return usage


def enforce(usage, limit_kb: int, only: str | None = None) -> list[tuple[str, int, int]]:
    killed = []
    for session, procs in usage.items():
        if only and session != only:
            continue
        total = sum(p[0] for p in procs)
        if total <= limit_kb:
            continue
        pss, pid, comm = max(procs)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
        killed.append((session, pid, total))
        log(
            f"session={session} pss_mb={total // 1024} limit_mb={limit_kb // 1024} "
            f"killed pid={pid} comm={comm} pss_mb={pss // 1024}"
        )
    return killed


def main() -> None:
    only = sys.argv[1] if len(sys.argv) > 1 else None  # 只管一个会话:验证脚本用
    if LIMIT_MB <= 0:
        log("SESSION_MAX_MEMORY_MB not set; per-session memory limit disabled")
        signal.pause()  # 保持容器存活,行为同原来的 sleep infinity
        return
    log(f"limit_mb={LIMIT_MB} interval={INTERVAL}s" + (f" only={only}" if only else ""))
    own_ns = os.readlink("/proc/self/ns/pid")
    while True:
        try:
            enforce(scan(own_ns), LIMIT_MB * 1024, only)
        except Exception as exc:  # 看门狗自己不能退出:它是容器的主进程
            log(f"scan failed: {exc!r}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
