"""产物版本:保存最近一次交付的独立副本,再次交付不同内容时转为历史。

历史放在 <会话目录>/versions/<文件名>/<旧文件 mtime_ns>,不在沙箱挂载里,agent 的命令碰不到。
隐藏的 .current 保存最近交付,不随 output 的改写而变化。
每个文件只保留最近 KEEP 份历史;超过 MAX_BYTES 的文件不留。删除会话时一起清掉。
"""

import hashlib
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path

from app.sandbox.session_paths import SessionPaths

KEEP = 5
MAX_BYTES = 20 * 1024 * 1024
VERSIONS_DIR = "versions"
CURRENT = ".current"


def _dir(paths: SessionPaths, file_name: str) -> Path:
    return paths.session_root / VERSIONS_DIR / file_name


def archive_previous(paths: SessionPaths, dest: Path) -> Path | None:
    """优先取上次交付副本;没有副本的旧会话,在覆盖前备份现有文件。"""
    try:
        current = _dir(paths, dest.name) / CURRENT
        if current.is_file() and not current.is_symlink():
            return current
        if dest.is_symlink() or not dest.is_file():
            return None
        st = dest.stat()
        if st.st_size > MAX_BYTES:
            return None
        target_dir = _dir(paths, dest.name)
        target_dir.mkdir(parents=True, exist_ok=True)
        return _archive_copy(dest, target_dir)
    except OSError:
        return None


def finish_archive(paths: SessionPaths, dest: Path, archived: Path | None) -> bool:
    """只在交付成功后调用:归档旧内容,原子更新交付副本,裁剪历史。"""
    try:
        if dest.is_symlink() or not dest.is_file():
            return False
        directory = _dir(paths, dest.name)
        if archived is not None and _same_content(archived, dest):
            if archived.name != CURRENT:
                archived.unlink(missing_ok=True)
        elif archived is not None and archived.name == CURRENT:
            _archive_copy(archived, directory)
        directory.mkdir(parents=True, exist_ok=True)
        if dest.stat().st_size <= MAX_BYTES:
            # 不能直接覆盖 .current:复制失败时仍要保留上次成功交付。
            copy_file_atomic(dest, directory / CURRENT)
        else:
            (directory / CURRENT).unlink(missing_ok=True)
        _prune(directory)
        return (directory / CURRENT).is_file()
    except OSError:
        return False


def copy_file_atomic(source: Path, target: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".delivery-", delete=False) as f:
        temporary = Path(f.name)
    try:
        shutil.copy2(source, temporary)
        temporary.replace(target)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _same_content(left: Path, right: Path) -> bool:
    # 不使用基于 mtime/size 缓存比较结果的 filecmp:Agent 可能保持相同的文件时间。
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as a, right.open("rb") as b:
        while data := a.read(64 * 1024):
            if data != b.read(len(data)):
                return False
        return not b.read(1)


def _archive_copy(source: Path, directory: Path) -> Path:
    """mtime 相同但内容不同时分配相邻版本号,避免覆盖旧内容。"""
    version = source.stat().st_mtime_ns
    target = directory / str(version)
    while target.exists():
        if _same_content(source, target):
            return target
        version += 1
        target = directory / str(version)
    copy_file_atomic(source, target)
    return target


def _prune(directory: Path) -> None:
    if not directory.is_dir():
        return
    entries = sorted(
        (p for p in directory.iterdir() if p.is_file() and p.name.isdigit()),
        key=lambda p: int(p.name),
    )
    for old in entries[:-KEEP]:
        old.unlink(missing_ok=True)


def list_versions(paths: SessionPaths, file_name: str) -> list[dict]:
    """历史版本,新的在前。version 是旧文件的 mtime_ns(字符串),modified_at 为 epoch 秒。"""
    if "/" in file_name or file_name in ("", ".", ".."):
        raise ValueError("Invalid file name")
    directory = _dir(paths, file_name)
    if not directory.is_dir():
        return []
    out = []
    for p in directory.iterdir():
        if p.is_file() and not p.is_symlink() and p.name.isdigit():
            out.append(
                {
                    "version": p.name,
                    "size": p.stat().st_size,
                    "modified_at": int(p.name) / 1e9,
                }
            )
    return sorted(out, key=lambda v: int(v["version"]), reverse=True)


def read_version(paths: SessionPaths, file_name: str, version: str) -> bytes:
    """抛 ValueError(参数不合法)、FileNotFoundError。"""
    if "/" in file_name or file_name in ("", ".", "..") or not version.isdigit():
        raise ValueError("Invalid version")
    return paths.read_host_file(_dir(paths, file_name) / version, MAX_BYTES)


def digest_file(path: Path) -> tuple[str, int]:
    """(sha256 十六进制, 字节数)。交付时记入记录,之后据此找回当时的版本。"""
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def find_delivered(
    paths: SessionPaths, file_name: str, sha256: str, size: int | None = None
) -> bytes | None:
    """按内容摘要找回某次交付的版本:依次看 output 里的文件、上次交付副本和历史版本。
    找不到(被裁剪、超过大小上限)返回 None。size 仅用来跳过明显不符的候选。"""
    if "/" in file_name or file_name in ("", ".", ".."):
        raise ValueError("Invalid file name")
    directory = _dir(paths, file_name)
    candidates = [paths.output / file_name, directory / CURRENT]
    if directory.is_dir():
        candidates += sorted(
            (p for p in directory.iterdir() if p.name.isdigit()),
            key=lambda p: int(p.name),
            reverse=True,
        )
    for path in candidates:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if size is not None and path.stat().st_size != size:
                continue
            # 先分块算摘要,对上了才整份读进内存:历史版本多、文件大时不必每份都读全
            with paths.open_host_file(path) as fh:
                if hashlib.file_digest(fh, "sha256").hexdigest() != sha256:
                    continue
                fh.seek(0)
                data = fh.read()
        except (OSError, ValueError):
            continue
        if hashlib.sha256(data).hexdigest() == sha256:  # 两次读取之间文件可能被改写
            return data
    return None
