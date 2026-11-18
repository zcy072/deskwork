"""sandbox/memwatch.py:按会话(PID namespace)合计 PSS,超限只杀该会话里最大的进程。"""

import importlib.util
import os
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "sandbox" / "memwatch.py"
if not _PATH.exists():  # backend 镜像里没有 sandbox/ 目录
    pytest.skip("sandbox/memwatch.py not available", allow_module_level=True)
_spec = importlib.util.spec_from_file_location("memwatch", _PATH)
memwatch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(memwatch)

OWN = "pid:[1]"


def _fake_proc(monkeypatch, procs):
    """procs: pid -> (ns, ppid, comm, cmdline, pss_kb)"""

    def readlink(path):
        return procs[int(path.split("/")[2])][0]

    def read(path, mode="r"):
        pid = int(path.split("/")[2])
        ns, ppid, comm, cmdline, pss = procs[pid]
        if path.endswith("/stat"):
            return f"{pid} ({comm}) S {ppid} 0 0"
        if path.endswith("/cmdline"):
            return b"\0".join(a.encode() for a in cmdline)
        if pss is None:
            raise PermissionError(path)
        return f"Rss: 1 kB\nPss: {pss} kB\n"

    monkeypatch.setattr(memwatch.os, "listdir", lambda _p: [str(p) for p in procs] + ["self"])
    monkeypatch.setattr(memwatch.os, "readlink", readlink)
    monkeypatch.setattr(memwatch, "_read", read)


def _bwrap(session):
    return ["bwrap", "--bind", f"/sandbox_data/sessions/{session}/output", "/workspace/output"]


@pytest.fixture
def procs():
    return {
        10: (OWN, 1, "bwrap", _bwrap("a"), 900),  # 外层 bwrap 在容器 namespace,不计入
        11: ("pid:[a]", 10, "bwrap", ["bwrap"], 100),  # namespace 的 init
        12: ("pid:[a]", 11, "bash", ["bash"], 3000),
        13: ("pid:[a]", 12, "python3", ["python3"], 700000),
        14: ("pid:[a]", 12, "python3", ["python3"], 400000),
        20: (OWN, 1, "bwrap", _bwrap("b"), 900),
        21: ("pid:[b]", 20, "bwrap", ["bwrap"], 100),
        22: ("pid:[b]", 21, "soffice.bin", ["soffice"], 200000),
        # 预览转换:同一会话的第二个 bwrap,应和常驻 shell 合计
        30: (OWN, 1, "bwrap", _bwrap("b"), 900),
        31: ("pid:[b2]", 30, "pdftoppm", ["pdftoppm"], 300000),
        # 不在 bwrap 里的进程(/exec 脚本、看门狗自身)不归任何会话
        40: (OWN, 1, "python3", ["python3", "/opt/memwatch.py"], 900000),
    }


def test_scan_groups_by_session(monkeypatch, procs):
    _fake_proc(monkeypatch, procs)
    usage = memwatch.scan(OWN)
    assert sorted(usage) == ["a", "b"]
    assert sum(p[0] for p in usage["a"]) == 100 + 3000 + 700000 + 400000
    assert sum(p[0] for p in usage["b"]) == 100 + 200000 + 300000


def test_enforce_kills_largest_process_of_session_over_limit(monkeypatch, procs):
    _fake_proc(monkeypatch, procs)
    killed = []
    monkeypatch.setattr(memwatch.os, "kill", lambda pid, sig: killed.append(pid))
    result = memwatch.enforce(memwatch.scan(OWN), 1024 * 1024)
    assert killed == [13]  # 只有会话 a 超限,杀它最大的进程;会话 b 不受影响
    assert [r[0] for r in result] == ["a"]


def test_enforce_only_filter(monkeypatch, procs):
    _fake_proc(monkeypatch, procs)
    killed = []
    monkeypatch.setattr(memwatch.os, "kill", lambda pid, sig: killed.append(pid))
    memwatch.enforce(memwatch.scan(OWN), 100 * 1024, only="b")
    assert killed == [31]


def test_unreadable_or_vanished_process_is_skipped(monkeypatch, procs):
    procs[14] = ("pid:[a]", 12, "python3", ["python3"], None)
    _fake_proc(monkeypatch, procs)
    assert sum(p[0] for p in memwatch.scan(OWN)["a"]) == 100 + 3000 + 700000


def test_session_id_must_match_strict_pattern(monkeypatch, procs):
    procs[10] = (OWN, 1, "bwrap", _bwrap("../etc"), 900)
    _fake_proc(monkeypatch, procs)
    assert "a" not in memwatch.scan(OWN) and "../etc" not in memwatch.scan(OWN)


def test_own_namespace_read_from_real_proc():
    assert os.readlink("/proc/self/ns/pid").startswith("pid:[")


def test_nested_namespace_counts_toward_session(monkeypatch, procs):
    # 会话里 unshare 出嵌套 PID namespace:那里的进程也要算进该会话,否则能绕过合计上限
    procs[15] = ("pid:[a]", 12, "unshare", ["unshare"], 100)
    procs[16] = ("pid:[nested]", 15, "python3", ["python3"], 900000)
    _fake_proc(monkeypatch, procs)
    killed = []
    monkeypatch.setattr(memwatch.os, "kill", lambda pid, sig: killed.append(pid))
    usage = memwatch.scan(OWN)
    assert 16 in {pid for _pss, pid, _c in usage["a"]}
    memwatch.enforce(usage, 1024 * 1024)
    assert killed == [16]
