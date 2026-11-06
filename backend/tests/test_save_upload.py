"""上传直接写进 uploads:权限固定,不跟随预置的符号链接,不覆盖已有文件。"""

import os
import stat

import pytest

from app.sandbox.bash_handler import BashSession
from app.sandbox.session_paths import SessionPaths


def _session(tmp_path) -> BashSession:
    paths = SessionPaths.create("upload-test", sessions_root=tmp_path / "sessions")
    paths.uploads.mkdir(parents=True)
    return BashSession(paths, manager_client=object())


async def test_upload_written_with_fixed_mode(tmp_path):
    session = _session(tmp_path)
    old = os.umask(0o077)
    try:
        dest = await session.save_upload("a.txt", b"hello")
    finally:
        os.umask(old)
    assert dest.read_bytes() == b"hello"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o665


async def test_upload_does_not_follow_planted_symlink(tmp_path):
    session = _session(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (session.paths.uploads / "a.txt").symlink_to(victim)
    with pytest.raises(OSError):
        await session.save_upload("a.txt", b"evil")
    assert victim.read_text() == "keep"


async def test_upload_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        await _session(tmp_path).save_upload("../x", b"")
