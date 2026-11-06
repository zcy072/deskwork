import asyncio
import shutil
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path

from app.sandbox import CONTAINER_MANAGER
from app.sandbox.bash_handler import BashSession
from app.sandbox.errors import ContainerFileSystemError
from app.services.deliverable_check import check_deliverable
from app.services.file_versions import archive_previous, digest_file, finish_archive
from app.tools.retry import retry_transient

# 未显式给 max_chars 时的输出上限。须容得下完整 SKILL.md(约 20KB + 行号)。
DEFAULT_VIEW_MAX_CHARS = 40_000
# 显式 max_chars 的上限:再大就会把整份大文件塞进上下文
MAX_VIEW_MAX_CHARS = 100_000


def _split_lines(content: str) -> list[str]:
    """只按换行符分行。str.splitlines 还会在 \f、\x1c、\u2028 等处断行,
    插入时会把它们改写成换行,行号也会和 grep -n / sed -n 对不上。"""
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return (
        f"{text[:limit]}\n... ({omitted} characters truncated; "
        "use view_range to read a specific part, or search_document / bash to query the file) ..."
    )


async def _read_text_for_edit(session: BashSession, container_path: str, relative_path: str) -> str:
    """编辑前严格按 UTF-8 解码:read_container_file 的 errors="replace" 会把 GBK 等编码的
    非 ASCII 字符全变成 U+FFFD,改一处再写回就毁掉了整个文件。"""
    try:
        return await session.read_container_file(container_path, strict=True)
    except UnicodeDecodeError as exc:
        raise ContainerFileSystemError(
            f"{relative_path} is not UTF-8 text (e.g. GBK); editing it here would corrupt it. "
            "Use bash_tool (e.g. Python with the right encoding) instead."
        ) from exc


def _relative_session_path(session: BashSession, container_path: str) -> str:
    """Return agent-facing relative path under /workspace."""
    try:
        virtual_path = session.paths.container_to_virtual_path(container_path)
    except ValueError:
        return container_path
    return virtual_path.removeprefix(f"{session.paths.virtual_root.rstrip('/')}/")


async def _resolve_existing_file(session_id: str, path: str) -> tuple[BashSession, str, str]:
    """Return (session, container_path, relative_path); raise if it is not a file."""
    session = await CONTAINER_MANAGER.get_or_create(session_id)
    container_path = session.resolve_container_path(path)
    relative_path = _relative_session_path(session, container_path)
    if not await session.is_file(container_path):
        raise ContainerFileSystemError(f"File not found: {relative_path}")
    return session, container_path, relative_path


async def view_container_path_content(
    session_id: str,
    path: str,
    view_range: list[int] | None = None,
    max_chars: int | None = None,
) -> str:
    """读文件是幂等的,沙箱瞬时故障时自动重试。"""
    return await retry_transient(
        lambda: _view_container_path_content(session_id, path, view_range, max_chars)
    )


async def _view_container_path_content(
    session_id: str,
    path: str,
    view_range: list[int] | None = None,
    max_chars: int | None = None,
) -> str:
    """View the contents of a file in the file system."""
    session = await CONTAINER_MANAGER.get_or_create(session_id)
    container_path = session.resolve_container_path(path)

    relative_path = _relative_session_path(session, container_path)

    if not await session.path_exists(container_path):
        raise ContainerFileSystemError(f"Path not found: {relative_path}")

    if await session.is_directory(container_path):
        if view_range is not None:
            raise ContainerFileSystemError("view_range is only supported when viewing files")
        listing = await session.list_directory(container_path)
        return _truncate(listing, DEFAULT_VIEW_MAX_CHARS if max_chars is None else max_chars)

    if not await session.is_file(container_path):
        raise ContainerFileSystemError(f"Path not found: {relative_path}")

    content = await session.read_container_file(container_path)
    limit = DEFAULT_VIEW_MAX_CHARS if max_chars is None else max_chars
    return await asyncio.to_thread(_render_view, content, view_range, limit)


def _render_view(content: str, view_range: list[int] | None, limit: int) -> str:
    """大文件的分行和加行号是 CPU 活,放在线程里做,不卡事件循环。"""
    selected_lines, start_line = _apply_view_range(_split_lines(content), view_range)
    return _truncate(_format_with_line_numbers(selected_lines, start_line), limit)


async def create_container_file(
    session_id: str,
    path: str,
    file_text: str,
) -> str:
    """Create a file in the file system."""
    session = await CONTAINER_MANAGER.get_or_create(session_id)
    container_path = session.resolve_container_path(path)
    await session.create_container_file(container_path, file_text)
    return f"Successfully created {_relative_session_path(session, container_path)}."


async def str_replace_in_container_file(
    session_id: str,
    path: str,
    old_string: str,
    new_string: str,
) -> str:
    """Replace a string in a file in the file system."""
    session, container_path, relative_path = await _resolve_existing_file(session_id, path)

    content = await _read_text_for_edit(session, container_path, relative_path)
    count = content.count(old_string)
    if count == 0:
        raise ContainerFileSystemError(
            "No match found for replacement. Please check your text and try again."
        )
    if count > 1:
        raise ContainerFileSystemError(
            f"Found {count} matches for replacement text. "
            "Please provide more context to make a unique match."
        )

    await session.write_container_file(
        container_path,
        content.replace(old_string, new_string, 1),
    )
    return f"Successfully replaced text in {relative_path}."


async def insert_in_container_file(
    session_id: str,
    path: str,
    insert_line: int,
    insert_text: str,
) -> str:
    """Insert text into a file at a specific line."""
    session, container_path, relative_path = await _resolve_existing_file(session_id, path)

    if insert_line < 0:
        raise ContainerFileSystemError("insert_line must be >= 0")

    content = await _read_text_for_edit(session, container_path, relative_path)
    lines = _split_lines(content)
    text_lines = _split_lines(insert_text)

    if insert_line == 0:
        new_lines = text_lines + lines
    elif insert_line > len(lines):
        raise ContainerFileSystemError(
            f"insert_line {insert_line} is beyond the end of the file ({len(lines)} lines)"
        )
    else:
        new_lines = lines[:insert_line] + text_lines + lines[insert_line:]

    newline = "\r\n" if "\r\n" in content else "\n"  # 保持原文件的换行风格
    new_content = newline.join(new_lines) + (newline if content.endswith("\n") else "")
    await session.write_container_file(container_path, new_content)
    return f"Successfully inserted text after line {insert_line}."


def _apply_view_range(
    lines: list[str],
    view_range: list[int] | None,
) -> tuple[list[str], int]:
    if view_range is None:
        return lines, 1

    if len(view_range) != 2:
        raise ContainerFileSystemError("view_range must contain exactly 2 integers")

    start, end = view_range
    if start < 1:
        raise ContainerFileSystemError("view_range start must be >= 1")

    total = len(lines)
    if end == -1:
        end = total
    elif end < 1:
        raise ContainerFileSystemError("view_range end must be >= 1 or -1")

    if start > total:
        raise ContainerFileSystemError(
            f"view_range start {start} is beyond the end of the file ({total} lines)"
        )
    if start > end:
        raise ContainerFileSystemError(f"Invalid view_range: start ({start}) > end ({end})")

    return lines[start - 1 : end], start


def _format_with_line_numbers(lines: list[str], start_line: int) -> str:
    return "\n".join(f"{index}: {line}" for index, line in enumerate(lines, start=start_line))


async def present_container_files(
    session_id: str,
    paths: list[str],
) -> tuple[str, list[dict[str, str | int]]]:
    """复制到 output 是幂等的(同名覆盖),沙箱瞬时故障时自动重试。"""
    return await retry_transient(lambda: _present_container_files(session_id, paths))


async def _present_container_files(
    session_id: str,
    paths: list[str],
) -> tuple[str, list[dict[str, str | int]]]:
    """Copy container files to host output/ and return metadata records."""
    session = await CONTAINER_MANAGER.get_or_create(session_id)
    records: list[dict[str, str | int]] = []
    messages: list[str] = []

    for raw_path in paths:
        virtual_path = session.resolve_virtual_path(raw_path)
        container_path = session.paths.virtual_to_container_path(virtual_path)
        if not await session.is_file(container_path):
            relative_path = _relative_session_path(session, container_path)
            raise ContainerFileSystemError(f"File not found: {relative_path}")

        file_name = Path(container_path).name
        host_dest = session.paths.output / file_name
        warning = await _validate_source(session, virtual_path, session_id, file_name)
        # output/ 对 agent 可写,目标若是它预先建好的符号链接,后端会顺着它写到沙箱外
        # 归档和复制可能涉及几十 MB 的读写与比对,都放到线程里,不卡住其他会话的事件流
        archived = await asyncio.to_thread(_prepare_dest, session.paths, host_dest)
        try:
            host_src = session.paths.virtual_to_host_path(virtual_path)
        except ValueError:
            host_src = None
        if host_src is None or not await asyncio.to_thread(_copy_local, host_src, host_dest):
            await session.copy_to_host(container_path, host_dest)
        await asyncio.to_thread(finish_archive, session.paths, host_dest, archived)
        # 文件已复制到 output/;记录必须指向这份副本,下载接口只放行 uploads/ 和 output/
        record = {
            "file_name": file_name,
            "host_path": str(host_dest),
            "container_path": f"{session.paths.virtual_root.rstrip('/')}/output/{file_name}",
        }
        # 摘要让历史消息能找回这次交付的内容,而不是之后被覆盖的最新版
        with suppress(OSError):
            record["sha256"], size = await asyncio.to_thread(digest_file, host_dest)
            record["size"] = size
        records.append(record)
        messages.append(f"Presented {file_name}" + (f"\n{warning}" if warning else ""))

    return "\n".join(messages), records


def _prepare_dest(paths, host_dest: Path):
    if host_dest.is_symlink():
        host_dest.unlink()
    return archive_previous(paths, host_dest)


def _copy_local(host_src: Path, host_dest: Path) -> bool:
    """源文件在宿主机上可直接读时就地复制,返回 True;否则返回 False 交给沙箱复制。"""
    if not host_src.is_file() or host_src.is_symlink():
        return False
    if host_src.resolve() != host_dest.resolve():
        host_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(host_src, host_dest)
    return True


# 同一文件连续校验失败的次数;达到上限后照常交付并附带警告,避免 Agent 在修不好时无限重试
MAX_CHECK_FAILURES = 3
MAX_CHECK_FAILURE_ENTRIES = 1024
_check_failures: OrderedDict[tuple[str, str], int] = OrderedDict()


async def _validate_source(session, virtual_path: str, session_id: str, file_name: str) -> str:
    """交付前检查源文件。失败抛 ContainerFileSystemError(此时还没动 output/);
    连续失败达到上限则放行并返回警告文本。没有宿主机路径可读时跳过检查。"""
    try:
        host_src = session.paths.virtual_to_host_path(virtual_path)
    except ValueError:
        return ""
    if not host_src.is_file() or host_src.is_symlink():
        return ""
    problem = await asyncio.to_thread(check_deliverable, host_src)
    key = (session_id, file_name)
    if problem is None:
        _check_failures.pop(key, None)
        return ""
    count = _check_failures.get(key, 0) + 1
    if count < MAX_CHECK_FAILURES:
        _check_failures[key] = count
        _check_failures.move_to_end(key)
        while len(_check_failures) > MAX_CHECK_FAILURE_ENTRIES:
            _check_failures.popitem(last=False)
        raise ContainerFileSystemError(
            f"{file_name} 未通过交付前检查:{problem}。output/ 中的旧版本未被改动。"
            f"请修复后重新调用 present_files(第 {count}/{MAX_CHECK_FAILURES - 1} 次修复机会)。"
        )
    _check_failures.pop(key, None)
    return f"警告:{file_name} 仍未通过交付前检查({problem}),已按现状交付,请在回答中如实告知用户。"
