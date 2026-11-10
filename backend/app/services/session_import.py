"""导入 session_export 生成的备份包,总是建成一个新会话,不覆盖任何已有会话。

备份包来自用户上传,按不可信数据处理:
- zip 条目名只认 manifest 列出的 files/<uploads|output>/...,路径逐段校验;
- 解压按实际读到的字节数限制,不信 zip 头里声明的大小;
- 每个文件的 sha256 与大小必须和 manifest 一致;
- checkpoint 先过 checkpoint_transfer 的类型白名单,再写库;
- 先写文件和 checkpoint,最后一个事务写数据库,失败时清掉前面写的东西,会话不会半途出现。
"""

import hashlib
import json
import os
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.config import settings
from app.sandbox.session_paths import SessionPaths
from app.services.checkpoint_transfer import (
    PAYLOAD_VERSION,
    CheckpointRejected,
    ParsedCheckpoint,
    parse_payload,
)
from app.services.session_export import (
    CHECKPOINT_PACKAGES,
    FORMAT_VERSION,
    GRAPH_VERSION,
    MAX_FILE_BYTES,
    MAX_FILES,
    checkpoint_versions,
)
from app.services.storage import DELETABLE_DIRS

SUPPORTED_VERSIONS = (1, 2, 3, FORMAT_VERSION)
MAX_ARCHIVE_BYTES = 1280 * 1024 * 1024
MAX_JSON_BYTES = 200 * 1024 * 1024
MAX_MESSAGES = 50000
MAX_ATTACHMENTS = 200
CHUNK = 1024 * 1024
MB = 1024 * 1024


class ImportRejected(ValueError):
    """备份包不合法或不能导入;消息可直接展示给用户。"""


@dataclass
class ImportPlan:
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[dict]
    files: list[dict]  # {"path", "size", "sha256"}
    missing: list[str]
    checkpoint: ParsedCheckpoint | None = None
    graph_checkpoint_id: str | None = None
    warnings: list[str] = field(default_factory=list)


def _read_json(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int = MAX_JSON_BYTES) -> Any:
    if info.file_size > limit:
        raise ImportRejected(f"{info.filename} 过大")
    try:
        with zf.open(info) as fh:
            data = fh.read(limit + 1)
        if len(data) > limit:
            raise ImportRejected(f"{info.filename} 过大")
        return json.loads(data)
    except ImportRejected:
        raise
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ImportRejected(f"{info.filename} 无法读取或不是合法的 JSON") from exc


def _parse_dt(value: Any, default: datetime) -> datetime:
    if not isinstance(value, str):
        return default
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return default
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def safe_relative_path(rel: Any) -> tuple[str, ...]:
    """manifest 里的文件路径 -> 路径段;不合法抛 ImportRejected。"""
    if not isinstance(rel, str) or not rel or len(rel) > 1024 or "\x00" in rel or "\\" in rel:
        raise ImportRejected("备份包里有不合法的文件路径")
    path = PurePosixPath(rel)
    parts = path.parts
    if (
        path.is_absolute()
        or len(parts) < 2
        or parts[0] not in DELETABLE_DIRS
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise ImportRejected(f"备份包里有不合法的文件路径:{rel[:80]}")
    if "/".join(parts) != rel:
        raise ImportRejected(f"备份包里有不规范的文件路径:{rel[:80]}")
    return parts


def _str(value: Any, limit: int, what: str) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ImportRejected(f"{what} 格式不对")
    return value


def _clean_message(raw: Any, index: int, now: datetime) -> dict:
    if not isinstance(raw, dict):
        raise ImportRejected(f"第 {index + 1} 条消息格式不对")
    role = raw.get("role")
    if role not in ("user", "assistant"):
        raise ImportRejected(f"第 {index + 1} 条消息的角色不合法")
    parts = raw.get("parts") or []
    usage = raw.get("usage")
    if not isinstance(parts, list) or not (usage is None or isinstance(usage, dict)):
        raise ImportRejected(f"第 {index + 1} 条消息的 parts / usage 格式不对")
    status = raw.get("status", "complete")
    if status not in ("running", "complete", "error", "cancelled"):
        raise ImportRejected(f"第 {index + 1} 条消息的状态不合法")
    checkpoint_id = raw.get("checkpoint_id")
    if checkpoint_id is not None:
        checkpoint_id = _str(checkpoint_id, 64, "checkpoint_id")
    attachments = raw.get("attachments") or []
    run = raw.get("run")
    if run is not None:
        if not isinstance(run, dict) or run.get("status") not in (
            "complete",
            "waiting",
            "error",
            "cancelled",
        ):
            raise ImportRejected("运行记录格式不对")
        committed = run.get("input_committed")
        if committed is not None and type(committed) is not bool:
            raise ImportRejected("运行输入提交状态格式不对")
        run = {
            "status": run["status"],
            "turn_id": _str(run.get("turn_id") or "", 64, "turn_id"),
            "error_code": _str(run.get("error_code") or "", 64, "error_code"),
            "public_error": _str(run.get("public_error") or "", 2000, "public_error"),
            "input_committed": committed,
        }
    seq = raw.get("last_event_seq") or 0
    if type(seq) is not int or not 0 <= seq < 2**63:
        raise ImportRejected("事件序号格式不对")
    if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
        raise ImportRejected(f"第 {index + 1} 条消息的附件格式不对")
    return {
        "role": role,
        "run": run,
        "last_event_seq": seq,
        "prompt_id": _str(raw.get("prompt_id") or "", 64, "prompt_id"),
        "content": _str(raw.get("content") or "", 10 * MB, "content"),
        "parts": parts,
        # 导出时不会有生成中的会话;包里若仍是 running,说明那一轮没正常结束
        "status": "error" if status == "running" else status,
        "usage": usage,
        "checkpoint_id": checkpoint_id if role == "user" else None,
        "created_at": _parse_dt(raw.get("created_at"), now),
        "attachments": [
            {
                "file_name": _str(a.get("file_name"), 255, "附件名"),
                "container_path": _str(a.get("container_path"), 512, "附件路径"),
            }
            for a in attachments
            if isinstance(a, dict)
        ],
    }


def inspect_archive(archive: Path) -> ImportPlan:
    """只读校验备份包并给出导入计划;不写任何东西。"""
    now = datetime.now(UTC)
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ImportRejected("不是有效的 zip 文件") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES + 8:
            raise ImportRejected("备份包内文件过多")
        counts = Counter(i.filename for i in infos)
        if dups := [n for n, c in counts.items() if c > 1]:
            raise ImportRejected(f"备份包里有重复条目:{dups[0][:80]}")
        by_name = {i.filename: i for i in infos}
        if "manifest.json" not in by_name or "conversation.json" not in by_name:
            raise ImportRejected("缺少 manifest.json 或 conversation.json,不是 Deskwork 备份包")
        manifest = _read_json(zf, by_name["manifest.json"])
        document = _read_json(zf, by_name["conversation.json"])
        checkpoint_payload = (
            _read_json(zf, by_name["checkpoint.json"]) if "checkpoint.json" in by_name else None
        )

    if not isinstance(manifest, dict) or not isinstance(document, dict):
        raise ImportRejected("备份包内容格式不对")
    version = manifest.get("format_version")
    if type(version) is not int or version not in SUPPORTED_VERSIONS:
        raise ImportRejected(
            f"备份包格式版本 {version!r} 不受支持(当前支持 {', '.join(map(str, SUPPORTED_VERSIONS))})"
        )

    raw_messages = document.get("messages")
    if not isinstance(raw_messages, list) or len(raw_messages) > MAX_MESSAGES:
        raise ImportRejected("消息列表格式不对或过长")
    messages = [_clean_message(m, i, now) for i, m in enumerate(raw_messages)]

    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ImportRejected("文件清单格式不对或过长")
    seen: set[str] = set()
    total = 0
    clean_files: list[dict] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise ImportRejected("文件清单格式不对")
        rel = entry.get("path")
        safe_relative_path(rel)
        size, sha = entry.get("size"), entry.get("sha256")
        if (
            rel in seen
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= MAX_FILE_BYTES
            or not isinstance(sha, str)
            or len(sha) != 64
        ):
            raise ImportRejected(f"文件清单里 {str(rel)[:80]} 的记录不合法")
        if f"files/{rel}" not in by_name:
            raise ImportRejected(f"备份包里缺少清单列出的文件:{rel}")
        seen.add(rel)
        total += size
        clean_files.append({"path": rel, "size": size, "sha256": sha.lower()})
    limit = settings.SESSION_MAX_STORAGE_MB * MB
    if limit and total > limit:
        raise ImportRejected(
            f"备份内文件共 {total // MB} MB,超过会话存储上限 {settings.SESSION_MAX_STORAGE_MB} MB"
        )

    missing = manifest.get("missing")
    plan = ImportPlan(
        title=str(document.get("title") or "")[:200],
        created_at=_parse_dt(document.get("created_at"), now),
        updated_at=_parse_dt(document.get("updated_at"), now),
        messages=messages,
        files=clean_files,
        missing=[m for m in missing if isinstance(m, str)][:MAX_FILES]
        if isinstance(missing, list)
        else [],
    )

    reason = (
        checkpoint_incompatibility(manifest.get("checkpoint"))
        if checkpoint_payload is not None
        else None
    )
    if reason:
        checkpoint_payload = None
        plan.warnings.append(f"{reason};已保留消息和文件,未恢复执行上下文,不能接续原上下文或回退")
    # Older exports included this exact empty payload for sessions that never
    # entered the graph. Accept it as absent context, not as a restorable graph.
    if (
        isinstance(checkpoint_payload, dict)
        and type(checkpoint_payload.get("payload_version")) is int
        and checkpoint_payload
        == {"payload_version": PAYLOAD_VERSION, "checkpoints": [], "writes": []}
    ):
        if document.get("graph_checkpoint_id") not in (None, ""):
            raise ImportRejected("选中的执行恢复点不在备份中")
        checkpoint_payload = None
    if checkpoint_payload is not None:
        try:
            plan.checkpoint = parse_payload(checkpoint_payload)
        except CheckpointRejected as exc:
            raise ImportRejected(str(exc)) from exc
        known = plan.checkpoint.ids
        cursor = document.get("graph_checkpoint_id")
        if cursor is not None:
            cursor = _str(cursor, 64, "graph_checkpoint_id")
            if cursor not in known:
                raise ImportRejected("选中的执行恢复点不在备份中")
            plan.graph_checkpoint_id = cursor
        lost = 0
        for m in messages:
            # "" 是空线程(第一条消息之前),不在 checkpoint 表里
            if m["checkpoint_id"] and m["checkpoint_id"] not in known:
                m["checkpoint_id"] = None
                lost += 1
        if lost:
            plan.warnings.append(
                f"有 {lost} 条消息的 checkpoint 不在备份包里,这些消息不能回退或重新生成"
            )
    else:
        for m in messages:
            m["checkpoint_id"] = None
        if not reason:
            plan.warnings.append("备份包没有 checkpoint,只能查看,不能让助手接着原上下文继续")
    return plan


def checkpoint_incompatibility(info: Any) -> str | None:
    """仅允许已知图结构与完全相同的依赖恢复执行,不推断补丁版本必然兼容。"""
    if not isinstance(info, dict) or type(info.get("graph_version")) is not int:
        return "旧备份缺少图结构版本,无法确认 checkpoint 兼容性"
    if info["graph_version"] != GRAPH_VERSION:
        return f"备份图结构版本 {info['graph_version']} 与当前版本 {GRAPH_VERSION} 不一致"
    versions = info.get("versions")
    if not isinstance(versions, dict):
        return "备份缺少运行时依赖版本"
    current = checkpoint_versions()
    mismatches = []
    for name in CHECKPOINT_PACKAGES:
        old, new = versions.get(name), current.get(name)
        if not isinstance(old, str) or not new or old != new:
            mismatches.append(f"{name}:备份 {str(old)[:40]} / 当前 {new or '未知'}")
    if mismatches:
        return "checkpoint 运行时版本不匹配(" + "; ".join(mismatches) + ")"
    return None


def _make_dir(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def extract_files(archive: Path, plan: ImportPlan, paths: SessionPaths) -> None:
    """把清单里的文件写进会话目录,边写边校验大小与 sha256。任何不一致抛 ImportRejected。"""
    root = paths.session_root.resolve()
    with zipfile.ZipFile(archive) as zf:
        for entry in plan.files:
            parts = safe_relative_path(entry["path"])
            # 目录只由我们创建,会话是全新的,不存在 agent 放的符号链接
            dest_dir = root.joinpath(*parts[:-1])
            if not dest_dir.resolve().is_relative_to(root):
                raise ImportRejected("文件路径越界")
            # output 要让沙箱里的 agent 能继续写;uploads 对 agent 只读
            mode = 0o777 if parts[0] == "output" else 0o775
            current = root
            for seg in parts[:-1]:
                current = current / seg
                if not current.exists():
                    _make_dir(current, mode)
            dest = dest_dir / parts[-1]
            digest = hashlib.sha256()
            size = 0
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
            try:
                with os.fdopen(fd, "wb") as out, zf.open(f"files/{entry['path']}") as src:
                    os.fchmod(out.fileno(), 0o666 if parts[0] == "output" else 0o665)
                    while chunk := src.read(CHUNK):
                        size += len(chunk)
                        if size > entry["size"]:
                            raise ImportRejected(f"文件 {entry['path']} 比清单声明的更大")
                        digest.update(chunk)
                        out.write(chunk)
            except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                raise ImportRejected(f"文件 {entry['path']} 无法解压") from exc
            if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
                raise ImportRejected(f"文件 {entry['path']} 的内容与清单中的 sha256 不一致")
