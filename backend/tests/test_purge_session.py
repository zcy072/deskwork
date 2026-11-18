"""purge_session:宿主侧删干净就不再请求 manager;删不掉时才让 manager 以 root 清理。"""

import shutil

from app.sandbox import bash_handler, session_paths
from app.sandbox.bash_handler import BashSessionsManager


class SpyClient:
    def __init__(self):
        self.removed = []

    async def remove_session(self, container, path):
        self.removed.append(path)


def _manager(tmp_path, monkeypatch):
    monkeypatch.setattr(bash_handler, "ManagerClient", SpyClient)
    monkeypatch.setattr(session_paths, "SANDBOX_DATA_ROOT", tmp_path.parent)
    manager = BashSessionsManager(host_sessions_root=tmp_path)
    return manager, manager._manager_client


async def test_purge_skips_manager_when_host_delete_succeeds(tmp_path, monkeypatch):
    manager, client = _manager(tmp_path, monkeypatch)
    (tmp_path / "s1" / "output").mkdir(parents=True)
    (tmp_path / "s1" / "output" / "a.txt").write_text("x")
    await manager.purge_session("s1")
    assert not (tmp_path / "s1").exists()
    assert client.removed == []


async def test_purge_falls_back_to_manager_when_files_remain(tmp_path, monkeypatch):
    manager, client = _manager(tmp_path, monkeypatch)
    (tmp_path / "s2" / "output").mkdir(parents=True)
    monkeypatch.setattr(shutil, "rmtree", lambda *_a, **_k: None)  # 模拟宿主侧无权删除
    await manager.purge_session("s2")
    assert len(client.removed) == 1 and client.removed[0].endswith(f"/{tmp_path.name}/s2")
