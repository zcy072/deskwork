"""会话备份包:把消息和 uploads/ output/ 里的文件打成一个 zip。

zip 内容:
  manifest.json       格式版本、导出时间、每个文件的 sha256/大小,以及被跳过的文件和原因
  conversation.json   标题、消息(含 parts、用量)与附件元数据
  files/<uploads|output>/...  实际文件

  checkpoint.json     LangGraph 的 checkpoint 与 writes 原始行(base64),导入后可接着原上下文对话;
                      太大或当前 saver 不支持时省略,原因记在 manifest["checkpoint"]
不含历史版本和快照。文件读取不跟随符号链接(agent 可写这两个目录);超过单文件或总量上限的文件跳过并记入清单。
"""

import hashlib
import json
import os
import stat
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

from app.db.models import Conversation, Message
from app.sandbox.session_paths import SessionPaths
from app.services.storage import DELETABLE_DIRS

FORMAT_VERSION = 3
# 修改 Agent 的节点/状态结构或 checkpoint 恢复语义时递增;提示词改动不需要递增。
GRAPH_VERSION = 1
CHECKPOINT_PACKAGES = (
    "langgraph",
    "langgraph-checkpoint",
    "langgraph-checkpoint-sqlite",
    "langchain",
    "langchain-core",
)
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_FILES = 5000
CHUNK = 1024 * 1024


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def conversation_document(
    conv: Conversation, messages: list[Message], attachments: dict[int, list[dict]]
) -> dict:
    return {
        "id": conv.id,
        "title": conv.title,
        "created_at": _iso(conv.created_at),
        "updated_at": _iso(conv.updated_at),
        "messages": [
            {
                "id": m.id,
                "prompt_id": m.prompt_id,
                "role": m.role,
                "content": m.content,
                "parts": m.parts,
                "status": m.status,
                "usage": m.usage,
                "created_at": _iso(m.created_at),
                **(
                    {
                        "checkpoint_id": m.checkpoint_id,
                        "attachments": attachments.get(m.id, []),
                    }
                    if m.role == "user"
                    else {}
                ),
            }
            for m in messages
        ],
    }


def _walk(root: Path, top: str) -> list[tuple[str, Path]]:
    """top 目录下的普通文件(相对会话根的路径, 绝对路径);符号链接不收。"""
    found: list[tuple[str, Path]] = []
    stack = [root / top]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        found.append(
                            (Path(entry.path).relative_to(root).as_posix(), Path(entry.path))
                        )
        except OSError:
            continue
    return sorted(found)


PRECOMPRESSED_SUFFIXES = frozenset(
    ".docx .xlsx .pptx .odt .ods .odp .zip .gz .tgz .bz2 .xz .7z .png .jpg .jpeg .gif .webp .mp3 .mp4".split()
)


def _copy_into_zip(zf: zipfile.ZipFile, arcname: str, src: Path, limit: int) -> tuple[str, int]:
    """O_NOFOLLOW 打开后边读边写,返回 (sha256, 字节数)。超过 limit 抛 ValueError。"""
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        digest = hashlib.sha256()
        size = 0
        # 和 zf.open(arcname) 默认生成的条目一致(当前时间、0600),只是压缩方式按类型选
        info = zipfile.ZipInfo(arcname, date_time=time.localtime()[:6])
        info.external_attr = 0o600 << 16
        # Office 文档、图片、压缩包本身已经压缩过,再 deflate 只费 CPU
        compressed = src.suffix.lower() in PRECOMPRESSED_SUFFIXES
        info.compress_type = zipfile.ZIP_STORED if compressed else zipfile.ZIP_DEFLATED
        with os.fdopen(fd, "rb") as fh, zf.open(info, "w", force_zip64=True) as out:
            fd = -1
            while chunk := fh.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    raise ValueError("file grew past the size limit")
                digest.update(chunk)
                out.write(chunk)
        return digest.hexdigest(), size
    finally:
        if fd != -1:
            os.close(fd)


def _checkpoint_info(checkpoint: dict | None, note: str | None) -> dict:
    if checkpoint is None:
        return {"included": False, "reason": note or "未导出"}
    return {
        "included": True,
        "checkpoints": len(checkpoint["checkpoints"]),
        "writes": len(checkpoint["writes"]),
        "versions": checkpoint_versions(),
        "graph_version": GRAPH_VERSION,
    }


def checkpoint_versions() -> dict[str, str]:
    out = {}
    for name in CHECKPOINT_PACKAGES:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    return out


def build_session_archive(
    paths: SessionPaths,
    document: dict,
    dest: Path,
    checkpoint: dict | None = None,
    checkpoint_note: str | None = None,
) -> dict:
    """把会话写成 zip 到 dest,返回 manifest。checkpoint 为 None 时在清单里写明原因。"""
    root = paths.session_root.resolve()
    files: list[dict] = []
    skipped: list[dict] = []
    total = 0
    candidates: list[tuple[str, Path]] = []
    for top in DELETABLE_DIRS:
        candidates.extend(_walk(root, top))
    if len(candidates) > MAX_FILES:
        for rel, _ in candidates[MAX_FILES:]:
            skipped.append({"path": rel, "reason": f"超过文件数上限 {MAX_FILES}"})
        candidates = candidates[:MAX_FILES]

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, src in candidates:
            try:
                size = src.stat(follow_symlinks=False).st_size
            except OSError:
                skipped.append({"path": rel, "reason": "读取失败"})
                continue
            if size > MAX_FILE_BYTES:
                skipped.append({"path": rel, "reason": "单个文件超过 200 MB"})
                continue
            if total + size > MAX_TOTAL_BYTES:
                skipped.append({"path": rel, "reason": "备份总量超过 1 GB"})
                continue
            try:
                sha, written = _copy_into_zip(zf, f"files/{rel}", src, MAX_FILE_BYTES)
            except (OSError, ValueError) as exc:
                skipped.append({"path": rel, "reason": f"读取失败:{exc}"})
                continue
            total += written
            files.append({"path": rel, "size": written, "sha256": sha})

        # 消息里引用了、但磁盘上已经没有的文件,单独列出,恢复时一眼能看出缺什么
        present = {f["path"] for f in files} | {s["path"] for s in skipped}
        missing = _missing_references(document, present)
        manifest = {
            "format_version": FORMAT_VERSION,
            "exported_at": _iso(datetime.now(UTC)),
            "conversation_id": document["id"],
            "message_count": len(document["messages"]),
            "files": files,
            "skipped": skipped,
            "missing": missing,
            "checkpoint": _checkpoint_info(checkpoint, checkpoint_note),
            "note": "备份消息、uploads/output 文件和 checkpoint;不含历史版本和回退快照。",
        }
        zf.writestr("conversation.json", json.dumps(document, ensure_ascii=False, indent=2))
        if checkpoint is not None:
            zf.writestr("checkpoint.json", json.dumps(checkpoint, ensure_ascii=False))
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def _relative(container_path: str | None) -> str | None:
    if not container_path or not container_path.startswith("/workspace/"):
        return None
    rel = container_path[len("/workspace/") :]
    return rel if rel.split("/", 1)[0] in DELETABLE_DIRS else None


def _missing_references(document: dict, present: set[str]) -> list[str]:
    refs: set[str] = set()
    for m in document["messages"]:
        for f in m.get("attachments", []):
            if rel := _relative(f.get("container_path")):
                refs.add(rel)
        for p in m.get("parts") or []:
            if isinstance(p, dict) and p.get("type") == "files":
                for f in p.get("files", []):
                    if isinstance(f, dict) and (rel := _relative(f.get("container_path"))):
                        refs.add(rel)
    return sorted(refs - present)


_ARCHIVE_PREFIX = "deskwork-export-"


def new_archive_path() -> Path:
    fd, name = tempfile.mkstemp(prefix=_ARCHIVE_PREFIX, suffix=".zip")
    os.close(fd)
    return Path(name)


def sweep_stale_archives(max_age_seconds: float = 3600) -> int:
    """删掉遗留的临时备份包。下载中途断开时 FileResponse 不会执行清理任务,文件会留在临时目录。"""
    cutoff = time.time() - max_age_seconds
    removed = 0
    for p in Path(tempfile.gettempdir()).glob(f"{_ARCHIVE_PREFIX}*.zip"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed
