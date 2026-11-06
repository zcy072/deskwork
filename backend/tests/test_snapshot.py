"""回退时恢复沙箱文件:快照脚本的生成、跳过条件、淘汰与恢复。"""

import os

import pytest

from app.config import settings
from app.sandbox import session_paths
from app.sandbox.bash_handler import BashSessionsManager
from app.sandbox.manager_client import ExecResult
from app.sandbox.quota import MB, dir_size
from app.sandbox.snapshots import (
    SNAPSHOT_EXCLUDED,
    snapshot_manifest,
    snapshot_name,
    snapshot_script,
)

PID = "0b6f7a52-1c1e-4f3c-9d55-aaaaaaaaaaaa"


class FakeClient:
    def __init__(self, returncode=0):
        self.scripts: list[str] = []
        self.returncode = returncode

    async def provision_session(self, container, path):
        pass

    async def run_exec(self, container, script, timeout=None):
        self.scripts.append(script)
        return ExecResult(self.returncode, "", "boom" if self.returncode else "")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(session_paths, "SANDBOX_DATA_ROOT", tmp_path)
    mgr = BashSessionsManager(host_sessions_root=tmp_path / "sessions")
    mgr._manager_client = FakeClient()
    return mgr


def test_snapshot_name_sorts_by_time_and_ends_with_prompt_id():
    a, b = snapshot_name(PID), snapshot_name(PID)
    assert a < b and a.endswith(f"_{PID}")
    assert snapshot_name(PID, suffix_only=True) == f"_{PID}"


@pytest.mark.parametrize("bad", ["", "x; rm -rf /", "../../etc", "a b"])
def test_snapshot_name_rejects_unsafe_prompt_id(bad):
    with pytest.raises(ValueError):
        snapshot_name(bad)


async def test_snapshot_runs_copy_and_prunes(manager, monkeypatch):
    monkeypatch.setattr(settings, "SNAPSHOT_KEEP", 3)
    monkeypatch.setattr(settings, "SNAPSHOT_MAX_MB", 10)
    await manager.get_or_create("s1")
    assert await manager.snapshot_session("s1", PID) is True
    script = manager._manager_client.scripts[-1]
    assert f"_{PID}" in script and "head -n -3" in script
    assert "--exclude=scratchpad/_rag" in script and "cp -al" in script


async def test_snapshot_fingerprint_ignores_temp_dirs_and_tracks_changes(manager):
    sess = await manager.get_or_create("s1")
    root = sess.paths.session_root
    (sess.paths.output / "a.txt").write_text("a")
    size, base = snapshot_manifest(root)
    assert size == 1

    for temp in SNAPSHOT_EXCLUDED:
        (sess.paths.scratchpad / temp).mkdir()
        (sess.paths.scratchpad / temp / "x").write_bytes(b"0" * 100)
    assert snapshot_manifest(root) == (1, base)

    (sess.paths.output / "a.txt").write_text("b")  # 大小相同,mtime 变了
    os.utime(sess.paths.output / "a.txt", ns=(1, 1))
    assert snapshot_manifest(root)[1] != base

    _, before = snapshot_manifest(root)
    (sess.paths.scratchpad / "_rag2").mkdir()  # 只排除顶层的同名目录
    assert snapshot_manifest(root)[1] != before


async def test_snapshot_script_embeds_current_fingerprint(manager, monkeypatch):
    monkeypatch.setattr(settings, "SNAPSHOT_KEEP", 3)
    sess = await manager.get_or_create("s1")
    (sess.paths.output / "a.txt").write_text("a")
    assert await manager.snapshot_session("s1", PID) is True
    _, fingerprint = snapshot_manifest(sess.paths.session_root)
    assert fingerprint in manager._manager_client.scripts[-1]


async def test_snapshot_disabled_when_keep_is_zero(manager, monkeypatch):
    monkeypatch.setattr(settings, "SNAPSHOT_KEEP", 0)
    assert await manager.snapshot_session("s1", PID) is False
    assert manager._manager_client.scripts == []


async def test_snapshot_skipped_when_too_large(manager, monkeypatch):
    monkeypatch.setattr(settings, "SNAPSHOT_KEEP", 3)
    monkeypatch.setattr(settings, "SNAPSHOT_MAX_MB", 1)
    sess = await manager.get_or_create("s1")
    (sess.paths.output / "big").write_bytes(b"0" * (2 * MB))
    before = len(manager._manager_client.scripts)
    assert await manager.snapshot_session("s1", PID) is False
    assert len(manager._manager_client.scripts) == before


async def test_snapshot_failure_returns_false(manager, monkeypatch):
    monkeypatch.setattr(settings, "SNAPSHOT_KEEP", 3)
    await manager.get_or_create("s1")
    manager._manager_client.returncode = 1
    assert await manager.snapshot_session("s1", PID) is False


async def test_restore_reports_missing_snapshot(manager):
    await manager.get_or_create("s1")
    manager._manager_client.returncode = 3
    assert await manager.restore_snapshot("s1", PID) is False


async def test_restore_stops_shell_and_runs_script(manager):
    sess = await manager.get_or_create("s1")
    stopped = []

    async def fake_stop():
        stopped.append(True)

    sess._stop = fake_stop
    assert await manager.restore_snapshot("s1", PID) is True
    assert stopped == [True]
    script = manager._manager_client.scripts[-1]
    assert f"_{PID}" in script and "-delete" in script


def test_dir_size_counts_hard_links_once(tmp_path):
    (tmp_path / "a").write_bytes(b"0" * 10)
    os.link(tmp_path / "a", tmp_path / "b")
    assert dir_size(tmp_path) == 10


def test_snapshot_script_rejects_zero_keep():
    """keep=0 时 head -n -0 会删光所有快照,必须在拼脚本时拒绝。"""
    with pytest.raises(ValueError):
        snapshot_script("/sandbox_data/sessions/s1", PID, "f" * 64, 0)


async def test_session_snapshot_skips_when_keep_is_zero(manager, monkeypatch):
    monkeypatch.setattr(settings, "SNAPSHOT_KEEP", 0)
    sess = await manager.get_or_create("s1")
    assert await sess.snapshot(PID) is False
    assert manager._manager_client.scripts == []
