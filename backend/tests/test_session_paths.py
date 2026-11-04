"""SessionPaths 路径映射的行为基线(宿主 / 容器 / 虚拟三套路径)。"""

import pytest

from app.sandbox.constants import (
    CONTAINER_DEFAULT_SKILLS,
    CONTAINER_SESSIONS_ROOT,
)
from app.sandbox.session_paths import SessionPaths, normalize_virtual_root


@pytest.fixture
def paths(tmp_path):
    return SessionPaths.create("abc123", sessions_root=tmp_path)


def test_normalize_virtual_root():
    assert normalize_virtual_root("workspace/") == "/workspace"
    assert normalize_virtual_root("/workspace") == "/workspace"


def test_subdirs_live_under_session_root(paths, tmp_path):
    assert paths.session_root == tmp_path.resolve() / "abc123"
    assert paths.uploads == paths.session_root / "uploads"
    assert paths.output == paths.session_root / "output"
    assert paths.scratchpad == paths.session_root / "scratchpad"


def test_resolve_host_path_inside_session(paths):
    resolved = paths.resolve_host_path("uploads/foo.txt")
    assert resolved == paths.uploads / "foo.txt"


@pytest.mark.parametrize("bad", ["../other", "uploads/../../escape", "/etc/passwd"])
def test_resolve_host_path_rejects_escape(paths, bad):
    with pytest.raises(ValueError):
        paths.resolve_host_path(bad)


def test_virtual_to_container_path_for_session_file(paths):
    # 容器根由宿主 SANDBOX_DATA_ROOT 推导,tmp_path 不在其下,这里只验证相对段
    rel = paths._resolve_virtual("/workspace/uploads/foo.txt")
    assert rel == (False, "uploads/foo.txt")


def test_virtual_skills_maps_to_global_dir(paths):
    assert paths.virtual_to_container_path("/workspace/skills/pdf/x.py") == (
        f"{CONTAINER_DEFAULT_SKILLS}/pdf/x.py"
    )
    assert paths.virtual_to_container_path("/workspace/skills") == CONTAINER_DEFAULT_SKILLS


def test_virtual_path_outside_workspace_rejected(paths):
    with pytest.raises(ValueError):
        paths.virtual_to_container_path("/etc/passwd")
    with pytest.raises(ValueError):
        paths.virtual_to_container_path("/workspacefoo/bar")


def test_container_to_virtual_roundtrip_for_skills(paths):
    container = f"{CONTAINER_DEFAULT_SKILLS}/docx/SKILL.md"
    assert paths.container_to_relative(container) == "skills/docx/SKILL.md"
    assert paths.container_to_virtual_path(container) == "/workspace/skills/docx/SKILL.md"


def test_container_to_relative_rejects_foreign_path(paths):
    with pytest.raises(ValueError):
        paths.container_to_relative(f"{CONTAINER_SESSIONS_ROOT}/other-session/uploads/a")


def test_virtual_helpers(paths):
    assert paths.virtual_scratchpad == "/workspace/scratchpad"
    assert paths.virtual_skills == "/workspace/skills"
    assert paths.virtual_uploads() == "/workspace/uploads"
    assert paths.virtual_subdir("output") == "/workspace/output"


def test_read_host_file_refuses_symlinks_and_escapes(tmp_path):
    """agent 可以把 scratchpad 里的产物换成链接,宿主侧读取不能顺着读出会话外的内容。"""
    paths = SessionPaths.create("read-test", sessions_root=tmp_path / "sessions")
    paths.scratchpad.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("KEY")
    ok = paths.scratchpad / "ok.md"
    ok.write_text("hello")
    assert paths.read_host_file(ok) == b"hello"
    assert paths.read_host_file(ok, 2) == b"he"

    (paths.scratchpad / "link.md").symlink_to(secret)
    with pytest.raises(OSError):
        paths.read_host_file(paths.scratchpad / "link.md")

    # 中间目录被换成链接:resolve 后已不在会话内
    (paths.scratchpad / "_rag").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        paths.read_host_file(paths.scratchpad / "_rag" / "secret.txt")

    (paths.scratchpad / "dir").mkdir()  # 目录、FIFO 等非普通文件
    with pytest.raises(ValueError):
        paths.read_host_file(paths.scratchpad / "dir")
