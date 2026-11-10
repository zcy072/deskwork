"""present_files 不能顺着 output/ 里 agent 预先建的符号链接写到别处。"""

import asyncio
from types import SimpleNamespace

from app.tools import executor


class FakeSession:
    def __init__(self, tmp_path) -> None:
        self.output = tmp_path / "output"
        self.scratch = tmp_path / "scratchpad"
        self.output.mkdir()
        self.scratch.mkdir()
        self.workspace_lock = asyncio.Lock()
        self.paths = SimpleNamespace(
            output=self.output,
            session_root=tmp_path,
            virtual_root="/workspace",
            virtual_to_container_path=lambda v: v,
            virtual_to_host_path=lambda v: self.scratch / v.rsplit("/", 1)[-1],
            trusted_root=lambda v: tmp_path,
        )
        self.manager_client = None
        self.container_name = "c"

    def resolve_virtual_path(self, raw: str) -> str:
        return "/workspace/scratchpad/" + raw.rsplit("/", 1)[-1]

    async def is_file(self, _path: str) -> bool:
        return True


async def test_present_does_not_write_through_symlink(tmp_path, monkeypatch):
    session = FakeSession(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("SECRET")
    (session.output / "a.txt").symlink_to(victim)
    (session.scratch / "a.txt").write_text("AGENT")

    async def get_or_create(_sid):
        return session

    monkeypatch.setattr(executor.CONTAINER_MANAGER, "get_or_create", get_or_create)
    await executor._present_container_files("s1", ["scratchpad/a.txt"])

    assert victim.read_text() == "SECRET"
    assert not (session.output / "a.txt").is_symlink()
    assert (session.output / "a.txt").read_text() == "AGENT"


async def test_present_from_scratchpad_reports_output_path(tmp_path, monkeypatch):
    """B1:从暂存目录交付时,记录要指向 output 里的副本,否则下载接口会拒绝。"""
    session = FakeSession(tmp_path)
    (session.scratch / "report.txt").write_text("DATA")

    async def get_or_create(_sid):
        return session

    monkeypatch.setattr(executor.CONTAINER_MANAGER, "get_or_create", get_or_create)
    _msg, records = await executor._present_container_files("s1", ["scratchpad/report.txt"])

    assert records[0]["container_path"] == "/workspace/output/report.txt"
    assert records[0]["file_name"] == "report.txt"
    assert (session.output / "report.txt").read_text() == "DATA"


def _swap_on_open(monkeypatch, trigger, action):
    from app.sandbox import host_files

    opened = host_files.os.open
    done = []

    def hook(path, flags, *args, **kwargs):
        if not done and trigger(str(path)):
            done.append(True)
            action()
        return opened(path, flags, *args, **kwargs)

    monkeypatch.setattr(host_files.os, "open", hook)
    return done


def _session_tree(tmp_path):
    from app.sandbox.session_paths import SessionPaths

    paths = SessionPaths.create("s", tmp_path / "sessions")
    for d in paths.subdirs:
        d.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("must be preserved")
    return paths, outside


def test_r11_temporary_replaced_by_link_is_not_followed(tmp_path, monkeypatch):
    """R11:临时文件创建后被换成链接,也不会按名字重新打开而写到会话外。"""
    from app.services import file_versions as fv

    paths, outside = _session_tree(tmp_path)
    src = paths.scratchpad / "a.txt"
    src.write_text("delivery bytes")
    dest = paths.output / "a.txt"
    real_copy = __import__("shutil").copyfileobj

    def swap_temp_then_copy(fsrc, fdst, length=0):
        for tmp in paths.output.glob(".delivery-*"):
            tmp.unlink()
            tmp.symlink_to(outside / "a.txt")
        return real_copy(fsrc, fdst, length)

    from app.sandbox import host_files

    monkeypatch.setattr(host_files.shutil, "copyfileobj", swap_temp_then_copy)
    import pytest

    with pytest.raises(OSError, match="replaced"):
        fv.copy_file_atomic(src, dest, paths.session_root)
    assert (outside / "a.txt").read_text() == "must be preserved"
    assert not dest.is_symlink()


def test_r11_output_dir_replaced_by_link_is_not_followed(tmp_path, monkeypatch):
    from app.services import file_versions as fv

    paths, outside = _session_tree(tmp_path)
    src = paths.scratchpad / "a.txt"
    src.write_text("delivery bytes")

    def swap_output():
        paths.output.rename(paths.session_root / "old-output")
        paths.output.symlink_to(outside, target_is_directory=True)

    done = _swap_on_open(monkeypatch, lambda p: p == "output", swap_output)
    try:
        fv.copy_file_atomic(src, paths.output / "a.txt", paths.session_root)
    except OSError:
        pass
    assert done
    assert (outside / "a.txt").read_text() == "must be preserved"
    assert sorted(p.name for p in outside.iterdir()) == ["a.txt"]


def test_r11_source_parent_replaced_by_link_is_not_read(tmp_path, monkeypatch):
    from app.services import file_versions as fv

    paths, outside = _session_tree(tmp_path)
    (outside / "a.txt").write_text("OUTSIDE_SECRET")
    nested = paths.scratchpad / "d"
    nested.mkdir()
    (nested / "a.txt").write_text("inside")

    def swap_source_dir():
        nested.rename(paths.scratchpad / "old-d")
        nested.symlink_to(outside, target_is_directory=True)

    done = _swap_on_open(monkeypatch, lambda p: p == "d", swap_source_dir)
    try:
        fv.copy_file_atomic(nested / "a.txt", paths.output / "a.txt", paths.session_root)
    except OSError:
        pass
    assert done
    dest = paths.output / "a.txt"
    assert not dest.exists() or "OUTSIDE_SECRET" not in dest.read_text()
