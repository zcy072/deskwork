"""executor 的文件读写逻辑:用内存假会话覆盖 str_replace / insert / view 的边界。"""

from types import SimpleNamespace

import pytest

from app.sandbox.errors import ContainerFileSystemError
from app.tools import executor
from app.tools.executor import (
    DEFAULT_VIEW_MAX_CHARS,
    _apply_view_range,
    insert_in_container_file,
    str_replace_in_container_file,
    view_container_path_content,
)


class FakeSession:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.paths = SimpleNamespace(
            virtual_root="/workspace",
            container_to_virtual_path=lambda p: p,
        )

    def resolve_container_path(self, raw: str) -> str:
        return raw

    async def is_file(self, path: str) -> bool:
        return path in self.files

    async def is_directory(self, path: str) -> bool:
        return path == "/workspace/dir"

    async def path_exists(self, path: str) -> bool:
        return path in self.files or path == "/workspace/dir"

    async def list_directory(self, path: str) -> str:
        return "listing"

    async def read_container_file(self, path: str, *, strict: bool = False) -> str:
        value = self.files[path]
        if isinstance(value, bytes):  # 用 bytes 模拟磁盘上的非 UTF-8 文件
            return value.decode("utf-8", errors="strict" if strict else "replace")
        return value

    async def write_container_file(self, path: str, text: str) -> None:
        self.files[path] = text


F = "/workspace/f.txt"


@pytest.fixture
def files(monkeypatch):
    store: dict[str, str] = {}
    session = FakeSession(store)

    async def get_or_create(_sid):
        return session

    monkeypatch.setattr(executor.CONTAINER_MANAGER, "get_or_create", get_or_create)
    return store


async def test_str_replace_no_match(files):
    files[F] = "hello"
    with pytest.raises(ContainerFileSystemError, match="No match"):
        await str_replace_in_container_file("s", F, "x", "y")
    assert files[F] == "hello"


async def test_str_replace_multiple_matches(files):
    files[F] = "aa aa"
    with pytest.raises(ContainerFileSystemError, match="2 matches"):
        await str_replace_in_container_file("s", F, "aa", "b")
    assert files[F] == "aa aa"


async def test_str_replace_single_match(files):
    files[F] = "one two"
    await str_replace_in_container_file("s", F, "two", "2")
    assert files[F] == "one 2"


async def test_missing_file_errors(files):
    with pytest.raises(ContainerFileSystemError, match="File not found"):
        await str_replace_in_container_file("s", F, "a", "b")
    with pytest.raises(ContainerFileSystemError, match="File not found"):
        await insert_in_container_file("s", F, 0, "a")


@pytest.mark.parametrize(
    ("content", "line", "text", "expected"),
    [
        ("a\nb\n", 0, "x", "x\na\nb\n"),
        ("a\nb\n", 1, "x", "a\nx\nb\n"),
        ("a\nb\n", 2, "x", "a\nb\nx\n"),
        ("a\nb", 2, "x", "a\nb\nx"),
        ("a\nb", 1, "x\ny", "a\nx\ny\nb"),
        ("a\nb", 1, "x\n", "a\nx\nb"),
        ("", 0, "x", "x"),
        ("a\r\nb\r\n", 1, "x", "a\r\nx\r\nb\r\n"),  # 保持 CRLF
    ],
)
async def test_insert(files, content, line, text, expected):
    files[F] = content
    await insert_in_container_file("s", F, line, text)
    assert files[F] == expected


async def test_insert_invalid_line(files):
    files[F] = "a\nb\n"
    with pytest.raises(ContainerFileSystemError, match="beyond"):
        await insert_in_container_file("s", F, 3, "x")
    with pytest.raises(ContainerFileSystemError, match=">= 0"):
        await insert_in_container_file("s", F, -1, "x")
    assert files[F] == "a\nb\n"


def test_apply_view_range():
    lines = ["a", "b", "c"]
    assert _apply_view_range(lines, None) == (lines, 1)
    assert _apply_view_range(lines, [2, -1]) == (["b", "c"], 2)
    assert _apply_view_range(lines, [1, 99]) == (lines, 1)
    with pytest.raises(ContainerFileSystemError, match="beyond"):
        _apply_view_range(lines, [4, 5])
    with pytest.raises(ContainerFileSystemError, match="start"):
        _apply_view_range(lines, [3, 2])
    with pytest.raises(ContainerFileSystemError, match="exactly 2"):
        _apply_view_range(lines, [1])
    with pytest.raises(ContainerFileSystemError, match=r"beyond the end of the file \(0 lines\)"):
        _apply_view_range([], [1, -1])
    with pytest.raises(ContainerFileSystemError, match=">= 1"):
        _apply_view_range(lines, [0, 2])
    with pytest.raises(ContainerFileSystemError, match="end"):
        _apply_view_range(lines, [1, 0])


async def test_view_numbers_lines_and_range(files):
    files[F] = "a\nb\nc"
    assert await view_container_path_content("s", F) == "1: a\n2: b\n3: c"
    assert await view_container_path_content("s", F, view_range=[2, 3]) == "2: b\n3: c"


async def test_view_directory_and_missing(files):
    assert await view_container_path_content("s", "/workspace/dir") == "listing"
    with pytest.raises(ContainerFileSystemError, match="view_range"):
        await view_container_path_content("s", "/workspace/dir", view_range=[1, 2])
    with pytest.raises(ContainerFileSystemError, match="not found"):
        await view_container_path_content("s", "/workspace/nope")


async def test_view_explicit_max_chars_truncates(files):
    files[F] = "x" * 100
    out = await view_container_path_content("s", F, max_chars=10)
    assert out.startswith("1: xxxxxxx")
    assert "truncated" in out


async def test_view_default_cap(files):
    files[F] = "\n".join("y" * 50 for _ in range(2000))  # ~106KB 带行号
    out = await view_container_path_content("s", F)
    assert out.startswith("1: yyy")
    assert "truncated" in out and "view_range" in out
    assert len(out) < DEFAULT_VIEW_MAX_CHARS + 300
    # view_range 仍可读到被截断的后半部分
    tail = await view_container_path_content("s", F, view_range=[1990, 2000])
    assert "1999: " in tail and "truncated" not in tail


async def test_view_explicit_max_chars_can_exceed_default(files):
    files[F] = "z" * (DEFAULT_VIEW_MAX_CHARS + 1000)
    out = await view_container_path_content("s", F, max_chars=DEFAULT_VIEW_MAX_CHARS * 2)
    assert "truncated" not in out


async def test_edit_refuses_non_utf8_file(files):
    """GBK 文件经 errors=replace 解码后写回会毁掉全部中文,必须拒绝。"""
    original = "客户,金额\n张三,100\n".encode("gbk")
    files[F] = original
    with pytest.raises(ContainerFileSystemError, match="not UTF-8"):
        await str_replace_in_container_file("s", F, "100", "200")
    with pytest.raises(ContainerFileSystemError, match="not UTF-8"):
        await insert_in_container_file("s", F, 1, "x")
    assert files[F] == original


async def test_insert_keeps_form_feed_and_line_numbers(files):
    files[F] = "a\fb\nc\n"
    await insert_in_container_file("s", F, 1, "new")
    assert files[F] == "a\fb\nnew\nc\n"
    view = await executor._view_container_path_content("s", F)
    assert view.split("\n")[0] == "1: a\fb"
    assert "3: c" in view


async def test_directory_listing_is_truncated(files, monkeypatch):
    async def big_listing(_p):
        return "x" * 50_000

    monkeypatch.setattr(FakeSession, "list_directory", lambda self, p: big_listing(p))
    out = await executor._view_container_path_content("s", "/workspace/dir")
    assert len(out) < 41_000 and "truncated" in out
