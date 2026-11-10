"""Recoverable workspace replacement; graph selection commits with message truncation."""

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.db import repository
from app.db import runs as run_store
from app.runtime.sessions import file_io
from app.sandbox.session_paths import SessionPaths


class RewindError(RuntimeError):
    pass


class FilesUnavailable(RewindError):
    pass


def snapshot_available(paths: SessionPaths, prompt_id: str) -> bool:
    return any(
        all((p / d).is_dir() for d in ("output", "scratchpad"))
        for p in (paths.session_root / "snapshots").glob(f"*_{prompt_id}")
    )


async def files_available(session_id: str, prompt_id: str) -> bool:
    run = await run_store.get(prompt_id)
    if run is not None and run.conversation_id != session_id:
        return False
    if run is not None and run.input_json.get("snapshot_ready") is False:
        return False
    return await file_io(snapshot_available, SessionPaths.create(session_id), prompt_id)


async def reused_files(session_id: str, message_id: int) -> list[dict]:
    files = await repository.get_message_files(message_id)
    paths = SessionPaths.create(session_id)

    def validate():
        for record in files:
            try:
                host = paths.virtual_to_host_path(record["container_path"])
                if not host.is_relative_to(paths.uploads.resolve()):
                    raise ValueError("Attachment is outside uploads")
                with paths.open_host_file(host):
                    pass
            except (ValueError, OSError):
                raise RewindError(
                    f"原附件 {record['file_name']} 已不可用,请重新上传后再回退"
                ) from None

    await file_io(validate)
    return files


def _write_journal(root: Path, document: dict) -> None:
    temporary = root / "journal.tmp"
    with temporary.open("w") as out:
        json.dump(document, out)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(root / "journal.json")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class RewindOperation:
    paths: SessionPaths
    run_id: str
    message_id: int
    graph_ref: dict
    files: bool
    fork_thread_id: str | None = None

    @property
    def root(self) -> Path:
        return self.paths.session_root / "operations" / self.run_id

    def _document(self) -> dict:
        return {
            "run_id": self.run_id,
            "session_id": self.paths.session_id,
            "message_id": self.message_id,
            "graph_ref": self.graph_ref,
            "files": self.files,
            "fork_thread_id": self.fork_thread_id,
        }

    def stage(self, snapshot: Path | None) -> None:
        self.root.mkdir(parents=True, exist_ok=False)
        try:
            _write_journal(self.root, self._document())
            if snapshot is not None:
                for name in ("output", "scratchpad"):
                    shutil.copytree(snapshot / name, self.root / "stage" / name, symlinks=True)
        except BaseException:
            shutil.rmtree(self.root, ignore_errors=True)
            raise

    def set_fork_thread(self, thread_id: str) -> None:
        self.fork_thread_id = thread_id
        _write_journal(self.root, self._document())

    def apply(self) -> None:
        if not self.files:
            return
        backup = self.root / "backup"
        backup.mkdir(exist_ok=True)
        for name in ("output", "scratchpad"):
            (self.paths.session_root / name).rename(backup / name)
            (self.root / "stage" / name).rename(self.paths.session_root / name)

    def recover(self, committed: bool) -> None:
        if not self.root.exists():
            return
        if not committed and self.files:
            for name in ("output", "scratchpad"):
                old = self.root / "backup" / name
                if old.exists():
                    current = self.paths.session_root / name
                    if current.is_symlink():
                        current.unlink()
                    elif current.exists():
                        shutil.rmtree(current)
                    old.rename(current)
        shutil.rmtree(self.root)


async def prepare(
    session_id: str, run_id: str, message_id: int, conversation_only: bool = False
) -> RewindOperation:
    message = await repository.get_message(session_id, message_id)
    if message is None or message.role != "user" or message.checkpoint_id is None:
        raise RewindError("该消息没有可恢复的执行上下文")
    await reused_files(session_id, message_id)
    paths = SessionPaths.create(session_id)
    snapshots = paths.session_root / "snapshots"
    snapshot = next(iter(sorted(snapshots.glob(f"*_{message.prompt_id}"), reverse=True)), None)
    if not conversation_only and not await files_available(session_id, message.prompt_id):
        raise FilesUnavailable("文件快照不可用。可以明确选择仅回退对话,当前文件将保留。")
    graph = (await run_store.graph_config(session_id))["configurable"]
    if message.checkpoint_id == "":
        graph_ref = {"thread_id": str(uuid.uuid4())}
    else:
        graph_ref = {"thread_id": graph["thread_id"], "checkpoint_id": message.checkpoint_id}
    operation = RewindOperation(paths, run_id, message_id, graph_ref, not conversation_only)
    try:
        await file_io(operation.stage, snapshot if operation.files else None)
    except BaseException:
        await file_io(operation.recover, False)
        raise
    return operation


async def recover_operation(operation: RewindOperation, committed: bool, checkpointer=None) -> None:
    if operation.fork_thread_id and not committed:
        if checkpointer is None:
            from app.agents.checkpointers import get_checkpointer

            checkpointer = get_checkpointer()
        await checkpointer.adelete_thread(operation.fork_thread_id)
    await file_io(operation.recover, committed)


async def recover_operations(sessions_root: Path) -> None:
    for journal in sessions_root.glob("*/operations/*/journal.json"):
        data = await file_io(lambda p=journal: json.loads(p.read_text()))
        sid, run_id = data["session_id"], data["run_id"]
        paths = SessionPaths.create(sid)
        if journal.parent != paths.session_root / "operations" / run_id:
            raise RewindError("Invalid recovery journal path")
        run = await run_store.get(run_id)
        op = RewindOperation(
            paths,
            run_id,
            data["message_id"],
            data["graph_ref"],
            data["files"],
            data.get("fork_thread_id"),
        )
        await recover_operation(op, bool(run and run.input_committed))
