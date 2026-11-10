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
