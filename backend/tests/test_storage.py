"""存储占用分类统计与单文件删除。"""

import pytest
from fastapi.testclient import TestClient

from app import app
from app.api.routes import stream
from app.sandbox.session_paths import SessionPaths
from app.services.storage import (
    clear_history_versions,
    clear_scratchpad,
    delete_session_file,
    list_session_files,
    storage_breakdown,
)


@pytest.fixture
def paths(tmp_path):
    p = SessionPaths.create("s", tmp_path)
    for d in ("uploads", "output", "scratchpad", "versions/a.txt", "snapshots/n1"):
        (p.session_root / d).mkdir(parents=True)
    return p


@pytest.fixture
def client(paths, monkeypatch):
    monkeypatch.setattr(stream.SessionPaths, "create", staticmethod(lambda _sid: paths))
    return TestClient(app)


def test_breakdown_counts_each_category(paths):
    root = paths.session_root
    (root / "uploads" / "u").write_bytes(b"1" * 10)
    (root / "output" / "o").write_bytes(b"1" * 20)
    (root / "scratchpad" / "s").write_bytes(b"1" * 30)
    (root / "versions" / "a.txt" / "100").write_bytes(b"1" * 40)
    (root / "snapshots" / "n1" / "x").write_bytes(b"1" * 50)
    assert storage_breakdown(paths) == {
        "uploads": 10,
        "output": 20,
        "scratchpad": 30,
        "versions": 40,
        "snapshots": 50,
    }


def test_breakdown_missing_dirs_are_zero(tmp_path):
    p = SessionPaths.create("empty", tmp_path)
    p.session_root.mkdir(parents=True)
    assert set(storage_breakdown(p).values()) == {0}


def test_delete_file_returns_freed_bytes_and_keeps_history(paths):
    root = paths.session_root
    (root / "output" / "a.txt").write_bytes(b"x" * 7)
    (root / "versions" / "a.txt" / "100").write_bytes(b"old")
    assert delete_session_file(paths, "output/a.txt") == 7
    assert not (root / "output" / "a.txt").exists()
    assert (root / "versions" / "a.txt" / "100").exists()


@pytest.mark.parametrize(
    "bad", ["scratchpad/x", "versions/a.txt/100", "output", "output/../scratchpad/x", "../x", ""]
)
def test_delete_rejects_other_locations(paths, bad):
    (paths.session_root / "scratchpad" / "x").write_bytes(b"x")
    with pytest.raises(ValueError):
        delete_session_file(paths, bad)
    assert (paths.session_root / "scratchpad" / "x").exists()


def test_delete_missing_or_directory_is_not_found(paths):
    (paths.session_root / "output" / "dir").mkdir()
    for rel in ("output/nope", "output/dir"):
        with pytest.raises(FileNotFoundError):
            delete_session_file(paths, rel)


def test_delete_symlink_removes_link_not_target(paths, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"keep")
    (paths.session_root / "output" / "link").symlink_to(outside)
    assert delete_session_file(paths, "output/link") == 0
    assert outside.read_bytes() == b"keep"


def test_delete_refuses_parent_symlinked_out_of_session(paths, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "f").write_bytes(b"keep")
    (paths.session_root / "output" / "sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        delete_session_file(paths, "output/sub/f")
    assert (outside / "f").exists()


def test_storage_endpoint(client, paths):
    (paths.session_root / "uploads" / "u").write_bytes(b"1" * 10)
    (paths.session_root / "versions" / "a.txt" / "100").write_bytes(b"1" * 5)
    body = client.get("/files/storage", params={"session_id": "s"}).json()
    assert body["sizes"]["uploads"] == 10 and body["sizes"]["versions"] == 5
    assert body["quota_used"] == 10  # 版本副本不计入配额
    assert body["quota_limit"] > 0


def test_delete_endpoint(client, paths, monkeypatch):
    (paths.session_root / "output" / "a.txt").write_bytes(b"x" * 4)
    params = {"session_id": "s", "relative_path": "output/a.txt"}
    assert client.delete("/files", params=params).json() == {"freed": 4}
    assert client.delete("/files", params=params).status_code == 404
    bad = client.delete("/files", params={"session_id": "s", "relative_path": "scratchpad/x"})
    assert bad.status_code == 400


def test_delete_endpoint_blocked_while_running(client, paths, monkeypatch):
    (paths.session_root / "output" / "a.txt").write_bytes(b"x")
    monkeypatch.setattr(stream, "get_active_prompt_id", lambda _sid: "p1")
    r = client.delete("/files", params={"session_id": "s", "relative_path": "output/a.txt"})
    assert r.status_code == 409
    assert (paths.session_root / "output" / "a.txt").exists()


def test_list_files_only_existing_regular_files(paths, tmp_path):
    root = paths.session_root
    (root / "uploads" / "a.csv").write_bytes(b"1")
    (root / "output" / "sub").mkdir()
    (root / "output" / "sub" / "b.txt").write_bytes(b"1")
    (root / "scratchpad" / "tmp").write_bytes(b"1")
    (root / "output" / "link").symlink_to(tmp_path)
    assert list_session_files(paths) == ["output/sub/b.txt", "uploads/a.csv"]


def test_list_endpoint(client, paths):
    (paths.session_root / "output" / "a.txt").write_bytes(b"1")
    assert client.get("/files/list", params={"session_id": "s"}).json() == ["output/a.txt"]


def test_clear_history_versions_keeps_current(paths):
    d = paths.session_root / "versions" / "a.txt"
    (d / "100").write_bytes(b"1" * 3)
    (d / "200").write_bytes(b"1" * 4)
    (d / ".current").write_bytes(b"cur")
    assert clear_history_versions(paths) == 7
    assert sorted(p.name for p in d.iterdir()) == [".current"]


def test_clear_history_versions_without_versions_dir(tmp_path):
    p = SessionPaths.create("empty", tmp_path)
    p.session_root.mkdir(parents=True)
    assert clear_history_versions(p) == 0


def test_clear_versions_endpoint(client, paths, monkeypatch):
    d = paths.session_root / "versions" / "a.txt"
    (d / "100").write_bytes(b"1" * 5)
    assert client.delete("/files/versions", params={"session_id": "s"}).json() == {"freed": 5}
    monkeypatch.setattr(stream, "get_active_prompt_id", lambda _sid: "p1")
    (d / "300").write_bytes(b"1")
    assert client.delete("/files/versions", params={"session_id": "s"}).status_code == 409
    assert (d / "300").exists()


def test_clear_scratchpad_removes_contents_but_not_links_targets(paths, tmp_path):
    scratch = paths.session_root / "scratchpad"
    (scratch / "a.txt").write_bytes(b"1" * 5)
    (scratch / "sub").mkdir()
    (scratch / "sub" / "b.txt").write_bytes(b"1" * 7)
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"keep")
    (scratch / "link").symlink_to(outside)
    (scratch / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    assert clear_scratchpad(paths) == 12
    assert scratch.is_dir() and list(scratch.iterdir()) == []
    assert outside.read_bytes() == b"keep"
    assert (paths.session_root / "uploads").is_dir()


def test_clear_scratchpad_missing_dir(tmp_path):
    p = SessionPaths.create("empty", tmp_path)
    p.session_root.mkdir(parents=True)
    assert clear_scratchpad(p) == 0


def test_clear_scratchpad_endpoint(client, paths, monkeypatch):
    (paths.session_root / "scratchpad" / "a").write_bytes(b"1" * 3)
    assert client.delete("/files/scratchpad", params={"session_id": "s"}).json() == {"freed": 3}
    monkeypatch.setattr(stream, "get_active_prompt_id", lambda _sid: "p1")
    (paths.session_root / "scratchpad" / "b").write_bytes(b"1")
    assert client.delete("/files/scratchpad", params={"session_id": "s"}).status_code == 409
    assert (paths.session_root / "scratchpad" / "b").exists()
