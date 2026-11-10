"""Office 文件预览:在沙箱里用 LibreOffice 转成 PDF,前端复用 PDF 预览。

转换产物放在会话的 scratchpad/_preview/<路径哈希>/,经 read_host_file 读取(不跟随符号链接)后删除。
结果按 (会话, 路径, 内容哈希) 缓存(mtime 可被保留或回拨,不可靠);同一文件的并发请求合并成一次转换;
全局限制同时进行的转换数。转换在独立的 bwrap 里执行(run_isolated),不占会话 shell 的锁,
与 agent 的命令并行。
"""

import asyncio
import hashlib
import shlex
import shutil
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path

from app.runtime.sessions import complete_before_cancelling, sessions
from app.sandbox import CONTAINER_MANAGER
from app.sandbox.session_paths import SessionPaths

OFFICE_SUFFIXES = {".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".odt", ".odp", ".ods"}
ALLOWED_DIRS = ("uploads", "output")
SOFFICE_SCRIPT = "/workspace/skills/docx/scripts/office/soffice.py"
CONVERT_TIMEOUT_SEC = 90  # 沙箱执行上限(EXEC_TIMEOUT_SEC),写大了也会被截断
MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_PDF_BYTES = 30 * 1024 * 1024
MAX_CONCURRENT = 2
CACHE_MAX = 6
PREWARM_MAX_FILES = 3

_Key = tuple[str, str, str]
_CACHE: OrderedDict[_Key, bytes] = OrderedDict()
_INFLIGHT: dict[_Key, asyncio.Task[tuple[bytes, bool]]] = {}
_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)


class PreviewError(Exception):
    """转换失败;消息可直接展示给用户。"""


def is_office(relative_path: str) -> bool:
    return Path(relative_path).suffix.lower() in OFFICE_SUFFIXES


def _locate(session_id: str, relative_path: str) -> tuple[SessionPaths, Path, str]:
    """校验路径并返回 (paths, 宿主路径, 相对会话根的路径)。

    抛 ValueError(路径或类型不合法)、FileNotFoundError。
    """
    if not is_office(relative_path):
        raise ValueError("不支持预览该类型的文件")
    paths = SessionPaths.create(session_id)
    host = paths.resolve_host_path(relative_path)
    root = paths.session_root.resolve()
    # 与 /files/download 相同:对解析后的路径判断白名单,output/../scratchpad/x 不能绕过
    if not any(host.is_relative_to(root / d) for d in ALLOWED_DIRS):
        raise ValueError("Invalid path")
    if not host.is_file():
        raise FileNotFoundError(relative_path)
    st = host.stat()
    if st.st_size > MAX_SOURCE_BYTES:
        raise ValueError(f"文件过大(超过 {MAX_SOURCE_BYTES // 1024 // 1024}MB),请下载查看")
    return paths, host, host.relative_to(root).as_posix()


def _digest(host: Path) -> str:
    h = hashlib.sha256()
    with host.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


async def _convert(session_id: str, rel: str, host: Path, digest: str) -> tuple[bytes, bool]:
    """返回 (PDF, 源文件在转换期间是否保持不变);变过的结果只返回、不缓存。"""
    async with _SEMAPHORE:
        session = await CONTAINER_MANAGER.get_or_create(session_id)
        paths = session.paths
        # 目录带内容哈希:同一文件的不同版本并发转换时互不清掉对方的产物
        tag = hashlib.sha1(f"{rel}\0{digest}".encode()).hexdigest()[:12]
        work = f"{paths.virtual_scratchpad}/_preview/{tag}"
        src = shlex.quote(f"{paths.virtual_root}/{rel}")
        cmd = " && ".join(
            [
                f"mkdir -p {work}",
                f"find {work} -maxdepth 1 -type f -delete",
                f"python3 {SOFFICE_SCRIPT} --headless --convert-to pdf --outdir {work} {src} >/dev/null 2>&1",
            ]
        )
        host_dir = paths.scratchpad / "_preview" / tag
        try:
            out, is_error = await session.run_isolated(cmd, timeout=CONVERT_TIMEOUT_SEC)
            if is_error:
                raise PreviewError(f"转换失败:{out[:200]}")
            pdf = host_dir / f"{Path(rel).stem}.pdf"
            try:
                data = await asyncio.to_thread(paths.read_host_file, pdf, MAX_PDF_BYTES + 1)
            except FileNotFoundError as exc:
                raise PreviewError("转换失败:没有生成 PDF(文件可能已损坏或有密码)") from exc
            except (OSError, ValueError) as exc:
                raise PreviewError(f"读取转换结果失败:{exc}") from exc
        finally:
            # 结果已在内存缓存里;中间 PDF 留在 scratchpad 只会占配额、进快照
            await asyncio.to_thread(shutil.rmtree, host_dir, True)
        if len(data) > MAX_PDF_BYTES:
            raise PreviewError("转换结果过大,请下载查看")
        try:
            stable = await asyncio.to_thread(_digest, host) == digest
        except OSError:
            stable = False
        return data, stable


async def office_to_pdf(session_id: str, relative_path: str) -> bytes:
    """返回 PDF 字节。抛 ValueError / FileNotFoundError(请求有误)或 PreviewError(转换失败)。"""
    handle = sessions.get(session_id)
    if sessions.closing or (handle and handle.operation != "run"):
        raise PreviewError("会话正在执行维护操作,请稍后再试")
    _, host, rel = await asyncio.to_thread(_locate, session_id, relative_path)
    try:
        digest = await asyncio.to_thread(_digest, host)
    except OSError as exc:
        raise FileNotFoundError(relative_path) from exc
    key = (session_id, rel, digest)
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    task = _INFLIGHT.get(key)
    if task is None:
        handle = sessions.get(session_id)
        if sessions.closing or (handle and handle.operation != "run"):
            raise PreviewError("会话正在执行维护操作,请稍后再试")
        task = asyncio.create_task(_convert(session_id, rel, host, digest))
        _INFLIGHT[key] = task

        def _done(t: asyncio.Task[tuple[bytes, bool]]) -> None:
            _INFLIGHT.pop(key, None)
            if not t.cancelled() and t.exception() is None:
                data, stable = t.result()
                if stable:
                    _CACHE[key] = data
                    while len(_CACHE) > CACHE_MAX:
                        _CACHE.popitem(last=False)

        task.add_done_callback(_done)
    # shield:某个请求断开不应取消其他等待同一结果的请求
    data, _ = await asyncio.shield(task)
    return data


async def prewarm(session_id: str, relative_paths: list[str]) -> None:
    """本轮结束后提前转换新交付的 Office 文件,用户点预览时直接命中缓存。

    尽力而为:失败、文件已不在(如会话已删除)都静默跳过,逐个转换,最多 PREWARM_MAX_FILES 个。
    """
    seen: set[str] = set()
    for rel in relative_paths:
        if len(seen) >= PREWARM_MAX_FILES:
            break
        if rel in seen or not is_office(rel):
            continue
        seen.add(rel)
        with suppress(ValueError, FileNotFoundError, PreviewError, OSError):
            await office_to_pdf(session_id, rel)


async def drain_previews(session_id: str | None = None) -> None:
    tasks = [
        task for key, task in list(_INFLIGHT.items()) if session_id is None or key[0] == session_id
    ]
    if tasks:

        async def drain():
            await asyncio.gather(*tasks, return_exceptions=True)

        await complete_before_cancelling(asyncio.create_task(drain()))
    for key in list(_CACHE):
        if session_id is None or key[0] == session_id:
            _CACHE.pop(key, None)
