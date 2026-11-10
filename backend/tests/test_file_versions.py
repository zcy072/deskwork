"""present_files 覆盖同名产物时保留历史版本。"""

import asyncio
import os
from types import SimpleNamespace

import pytest

from app.sandbox.session_paths import SessionPaths
from app.services import file_versions as fv
from app.tools import executor


@pytest.fixture
def paths(tmp_path):
    p = SessionPaths.create("s1", tmp_path)
    for d in p.subdirs:
        d.mkdir(parents=True)
    return p


def _overwrite(paths, text, mtime):
    dest = paths.output / "a.txt"
    arch = fv.archive_previous(paths, dest)
    dest.write_text(text)
    os.utime(dest, ns=(mtime, mtime))
    fv.finish_archive(paths, dest, arch)


def test_first_write_has_no_history(paths):
    _overwrite(paths, "v1", 1_000)
    assert fv.list_versions(paths, "a.txt") == []


def test_overwrite_keeps_old_and_skips_identical(paths):
    _overwrite(paths, "v1", 1_000)
    _overwrite(paths, "v2", 2_000)
    _overwrite(paths, "v2", 3_000)  # 内容没变:不新增
    versions = fv.list_versions(paths, "a.txt")
    assert [v["version"] for v in versions] == ["1000"]
    assert fv.read_version(paths, "a.txt", "1000") == b"v1"


def test_keeps_only_latest(paths):
    for i in range(1, fv.KEEP + 4):
        _overwrite(paths, f"v{i}", i * 1_000)
    versions = fv.list_versions(paths, "a.txt")
    assert len(versions) == fv.KEEP
    assert versions[0]["version"] == str((fv.KEEP + 2) * 1_000)


def test_rejects_bad_names(paths):
    with pytest.raises(ValueError):
        fv.list_versions(paths, "../x")
    with pytest.raises(ValueError):
        fv.read_version(paths, "a.txt", "../1")


def test_symlink_dest_not_archived(paths, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("SECRET")
    link = paths.output / "a.txt"
    link.symlink_to(victim)
    assert fv.archive_previous(paths, link) is None


async def test_present_archives_previous(paths, monkeypatch):
    src = paths.scratchpad / "a.txt"
    session = SimpleNamespace(
        workspace_lock=asyncio.Lock(),
        paths=SimpleNamespace(
            output=paths.output,
            session_root=paths.session_root,
            virtual_root="/workspace",
            virtual_to_container_path=lambda v: v,
            virtual_to_host_path=lambda v: src,
            trusted_root=lambda v: paths.session_root,
        ),
        resolve_virtual_path=lambda raw: "/workspace/scratchpad/a.txt",
        manager_client=None,
        container_name="c",
    )

    async def is_file(_p):
        return True

    session.is_file = is_file

    async def get_or_create(_sid):
        return session

    monkeypatch.setattr(executor.CONTAINER_MANAGER, "get_or_create", get_or_create)
    src.write_text("one")
    await executor._present_container_files("s1", ["scratchpad/a.txt"])
    src.write_text("two")
    await executor._present_container_files("s1", ["scratchpad/a.txt"])
    versions = fv.list_versions(paths, "a.txt")
    assert len(versions) == 1
    assert fv.read_version(paths, "a.txt", versions[0]["version"]) == b"one"
    assert (paths.output / "a.txt").read_text() == "two"


def test_delivery_snapshot_survives_direct_output_edit(paths):
    _overwrite(paths, "first delivery", 1_000)
    dest = paths.output / "a.txt"
    # Agent 在再次调用 present_files 之前就覆盖了 output,交付时再备份已来不及。
    dest.write_text("second delivery")
    os.utime(dest, ns=(2_000, 2_000))
    archived = fv.archive_previous(paths, dest)
    fv.finish_archive(paths, dest, archived)
    versions = fv.list_versions(paths, "a.txt")
    assert len(versions) == 1
    assert fv.read_version(paths, "a.txt", versions[0]["version"]) == b"first delivery"


def test_versions_with_same_mtime_do_not_overwrite_each_other(paths):
    _overwrite(paths, "v1", 1_000)
    _overwrite(paths, "v2", 1_000)
    _overwrite(paths, "v3", 1_000)
    versions = fv.list_versions(paths, "a.txt")
    assert len(versions) == 2
    assert {fv.read_version(paths, "a.txt", v["version"]) for v in versions} == {b"v1", b"v2"}


def test_identical_delivery_with_preserved_mtime_is_not_archived(paths):
    for text in ("v1", "v2", "v2", "v3", "v3"):
        _overwrite(paths, text, 1_000)
    versions = fv.list_versions(paths, "a.txt")
    assert len(versions) == 2
    assert {fv.read_version(paths, "a.txt", v["version"]) for v in versions} == {b"v1", b"v2"}


def test_legacy_output_is_archived_and_starts_tracking_deliveries(paths):
    dest = paths.output / "a.txt"
    dest.write_text("legacy")
    os.utime(dest, ns=(500, 500))
    _overwrite(paths, "v1", 1_000)
    dest.write_text("v2")  # 升级后直接覆盖也保留上次交付
    fv.finish_archive(paths, dest, fv.archive_previous(paths, dest))
    assert {
        fv.read_version(paths, "a.txt", v["version"]) for v in fv.list_versions(paths, "a.txt")
    } == {b"legacy", b"v1"}


def test_large_delivery_keeps_previous_small_version(paths, monkeypatch):
    monkeypatch.setattr(fv, "MAX_BYTES", 3)
    _overwrite(paths, "v1", 1_000)
    _overwrite(paths, "large", 2_000)
    assert fv.read_version(paths, "a.txt", "1000") == b"v1"
    assert not (paths.session_root / "versions" / "a.txt" / fv.CURRENT).exists()


def test_failed_snapshot_copy_keeps_last_successful_delivery(paths, monkeypatch):
    _overwrite(paths, "v1", 1_000)
    dest = paths.output / "a.txt"
    dest.write_text("v2")
    from app.sandbox import host_files

    def fail_copy(_src, _dst, _length=0):
        raise OSError("disk full")

    monkeypatch.setattr(host_files.shutil, "copyfileobj", fail_copy)
    fv.finish_archive(paths, dest, fv.archive_previous(paths, dest))
    directory = paths.session_root / "versions" / "a.txt"
    assert (directory / fv.CURRENT).read_text() == "v1"
    assert not list(directory.glob(".delivery-*"))


def test_version_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import app
    from app.sandbox import session_paths

    monkeypatch.setattr(session_paths, "SESSION_ROOT", tmp_path)
    p = SessionPaths.create("vsess", tmp_path)
    for d in p.subdirs:
        d.mkdir(parents=True)
    _overwrite(p, "v1", 1_000)
    _overwrite(p, "v2", 2_000)
    c = TestClient(app)
    q = {"session_id": "vsess"}
    r = c.get("/files/versions", params={**q, "file_name": "a.txt"})
    assert r.status_code == 200 and [v["version"] for v in r.json()] == ["1000"]
    r = c.get("/files/version", params={**q, "file_name": "a.txt", "version": "1000"})
    assert r.status_code == 200 and r.content == b"v1"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert c.get("/files/versions", params={**q, "file_name": "../x"}).status_code == 400
    assert (
        c.get("/files/version", params={**q, "file_name": "a.txt", "version": "9"}).status_code
        == 404
    )
    assert (
        c.get("/files/version", params={**q, "file_name": "a.txt", "version": "x"}).status_code
        == 422
    )


def test_virtual_to_relative():
    from app.services.chat_operations import _virtual_to_relative

    assert _virtual_to_relative("/workspace/output/a.docx") == "output/a.docx"


def _digest(text):
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def test_find_delivered_returns_old_content_after_overwrite(paths):
    _overwrite(paths, "first", 1_000)
    _overwrite(paths, "second", 2_000)
    assert fv.find_delivered(paths, "a.txt", _digest("first")) == b"first"
    assert fv.find_delivered(paths, "a.txt", _digest("second")) == b"second"


def test_find_delivered_none_after_pruned(paths):
    for i in range(1, fv.KEEP + 4):
        _overwrite(paths, f"v{i}", i * 1_000)
    assert fv.find_delivered(paths, "a.txt", _digest("v1")) is None
    assert fv.find_delivered(paths, "a.txt", _digest(f"v{fv.KEEP + 3}")) is not None


def test_find_delivered_ignores_symlink(paths, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("SECRET")
    (paths.output / "a.txt").symlink_to(victim)
    assert fv.find_delivered(paths, "a.txt", _digest("SECRET")) is None


def test_find_delivered_rejects_bad_name(paths):
    with pytest.raises(ValueError):
        fv.find_delivered(paths, "../x", "0" * 64)


async def test_present_records_digest(paths, monkeypatch):
    src = paths.scratchpad / "a.txt"
    session = SimpleNamespace(
        workspace_lock=asyncio.Lock(),
        paths=SimpleNamespace(
            output=paths.output,
            session_root=paths.session_root,
            virtual_root="/workspace",
            virtual_to_container_path=lambda v: v,
            virtual_to_host_path=lambda v: src,
            trusted_root=lambda v: paths.session_root,
        ),
        resolve_virtual_path=lambda raw: "/workspace/scratchpad/a.txt",
        manager_client=None,
        container_name="c",
    )

    async def is_file(_p):
        return True

    session.is_file = is_file

    async def get_or_create(_sid):
        return session

    monkeypatch.setattr(executor.CONTAINER_MANAGER, "get_or_create", get_or_create)
    src.write_text("hello")
    _msg, records = await executor._present_container_files("s1", ["scratchpad/a.txt"])
    assert records[0]["sha256"] == _digest("hello")
    assert records[0]["size"] == 5
