"""文件工具在宿主侧读取:跟随符号链接后不能读到别的会话或会话目录外的文件。"""

import pytest

from app.sandbox import bash_handler, session_paths
from app.sandbox.bash_handler import BashSession
from app.sandbox.errors import ContainerFileSystemError


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(session_paths, "SANDBOX_DATA_ROOT", tmp_path)
    monkeypatch.setattr(session_paths, "DEFAULT_SKILLS_DIR", tmp_path / "default_skills")
    (tmp_path / "default_skills" / "docx").mkdir(parents=True)
    (tmp_path / "default_skills" / "docx" / "SKILL.md").write_text("skill doc")
    made = {}
    for sid in ("sess-a", "sess-b"):
        paths = session_paths.SessionPaths.create(sid, tmp_path / "sessions")
        for d in paths.subdirs:
            d.mkdir(parents=True)
        made[sid] = BashSession(paths, docker_client=object())
    (made["sess-b"].paths.uploads / "secret.txt").write_text("B's secret")
    return tmp_path, made["sess-a"]


def _cp(session, virtual):
    return session.resolve_container_path(virtual)


async def test_reads_own_files_and_skills(sessions):
    _, a = sessions
    (a.paths.scratchpad / "n.txt").write_text("hello")
    assert await a.read_container_file(_cp(a, "/workspace/scratchpad/n.txt")) == "hello"
    assert await a.is_file(_cp(a, "/workspace/scratchpad/n.txt"))
    assert await a.is_directory(_cp(a, "/workspace/scratchpad"))
    assert not await a.path_exists(_cp(a, "/workspace/scratchpad/missing"))
    skill = _cp(a, "/workspace/skills/docx/SKILL.md")
    assert await a.read_container_file(skill) == "skill doc"
    listing = await a.list_directory(_cp(a, "/workspace/scratchpad"))
    assert "n.txt" in listing


async def test_symlink_to_other_session_is_blocked(sessions):
    root, a = sessions
    (a.paths.scratchpad / "d").symlink_to(root, target_is_directory=True)
    (a.paths.scratchpad / "f").symlink_to(root / "sessions/sess-b/uploads/secret.txt")
    leak_dir = _cp(a, "/workspace/scratchpad/d/sessions")
    leak_file = _cp(a, "/workspace/scratchpad/d/sessions/sess-b/uploads/secret.txt")
    with pytest.raises(ContainerFileSystemError, match="escapes"):
        await a.list_directory(leak_dir)
    with pytest.raises(ContainerFileSystemError, match="escapes"):
        await a.read_container_file(leak_file)
    with pytest.raises(ContainerFileSystemError, match="escapes"):
        await a.is_file(_cp(a, "/workspace/scratchpad/f"))
    # 链接本身在目录列表里显示为链接,不展开
    assert "d -> " in await a.list_directory(_cp(a, "/workspace/scratchpad"))


async def test_large_file_is_refused(sessions, monkeypatch):
    _, a = sessions
    monkeypatch.setattr(bash_handler, "MAX_READ_BYTES", 10)
    (a.paths.scratchpad / "big.txt").write_text("x" * 11)
    with pytest.raises(ContainerFileSystemError, match="larger than"):
        await a.read_container_file(_cp(a, "/workspace/scratchpad/big.txt"))
