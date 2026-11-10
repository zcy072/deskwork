"""删除会话时的级联清理:数据库、LangGraph checkpoint、沙箱文件、Redis 键。"""

import asyncio
import json
from pathlib import Path

from app.agents.checkpointers import get_checkpointer
from app.db import is_db_ready, repository
from app.db import runs as run_store
from app.runtime.sessions import complete_before_cancelling, sessions
from app.sandbox import CONTAINER_MANAGER
from app.sandbox.constants import SANDBOX_DATA_ROOT
from app.services import chat_operations
from app.utils import get_thread_status, logger, purge_session

# 沙箱清理失败(如 manager 不可用)时留下的标记,下次启动重试;避免靠"数据库里没有就删"扫孤儿,
# 那样数据库一旦被重置,会把所有会话文件误删。
PENDING_PURGE_DIR = SANDBOX_DATA_ROOT / "pending_purge"


def _write_pending(marker: Path, tasks: dict) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps(tasks))
    temporary.replace(marker)


async def _finish_pending(marker: Path, tasks: dict) -> bool:
    for name in list(tasks):
        try:
            if name == "checkpoints":
                for thread_id in tasks[name]:
                    await get_checkpointer().adelete_thread(thread_id)
            elif name == "sandbox":
                await CONTAINER_MANAGER.purge_session(marker.name)
            elif name == "redis":
                await purge_session(marker.name)
            tasks.pop(name)
            _write_pending(marker, tasks)
        except Exception:
            logger.exception("Pending %s cleanup for %s failed", name, marker.name)
    if not tasks:
        marker.unlink(missing_ok=True)
        return True
    return False


async def retry_pending_purges(root: Path | None = None) -> int:
    root = root or PENDING_PURGE_DIR
    if not root.is_dir():
        return 0
    done = 0
    for marker in root.iterdir():
        if not marker.is_file() or marker.suffix == ".tmp":
            continue
        try:
            text = marker.read_text()
            if (
                text
                and is_db_ready()
                and await repository.get_conversation(marker.name) is not None
            ):
                # Delete rolled back, or an import committed: keep its resources.
                marker.unlink()
                continue
            tasks = json.loads(text) if text else {"sandbox": True}  # legacy marker
            done += await _finish_pending(marker, tasks)
        except Exception:
            logger.exception("Pending cleanup for %s failed", marker.name)
    return done


async def discard_uncommitted_import(session_id: str, wrote_checkpoint: bool) -> None:
    """A committed conversation is the receipt for a successful import.

    Persist cleanup intent before checking it. If the database or cleanup is
    unavailable, startup retries the marker and checks the receipt again.
    The caller must drain all owned IO and the commit before entering here.
    """
    marker = PENDING_PURGE_DIR / session_id
    tasks = {"sandbox": True}
    if wrote_checkpoint:
        tasks = {"checkpoints": [session_id], **tasks}
    _write_pending(marker, tasks)
    try:
        if await repository.get_conversation(session_id) is not None:
            marker.unlink(missing_ok=True)
            return
        await _finish_pending(marker, tasks)
    except Exception:
        # Never delete files based on an inconclusive COMMIT outcome.
        logger.exception("Import reconciliation for %s requires retry", session_id)


async def delete_conversation_data(conversation_id: str) -> bool:
    """返回 False 表示会话不存在。生成中的会话先取消,避免删除后 agent 仍在写文件。

    正在启动新一轮的会话拒绝删除(ActiveRunError),否则启动流程会把会话重新写回来。"""
    handle = sessions.begin_delete(conversation_id)
    try:
        # The request owns the entire delete, including the run finalizer and
        # database commit. Drain it before cancellation can release the gate.
        return await complete_before_cancelling(asyncio.create_task(_delete(conversation_id)))
    finally:
        if handle.phase == "recovery_required":
            handle.operation = "run"
        else:
            sessions.release(handle)


async def _delete(conversation_id: str) -> bool:
    handle = sessions.get(conversation_id)
    if (handle and handle.task) or (
        not is_db_ready() and await get_thread_status(conversation_id) == "running"
    ):
        try:
            await chat_operations.cancel_chat(conversation_id)
        except (LookupError, RuntimeError):
            if handle:
                handle.phase = "recovery_required"
            raise chat_operations.ActiveRunError("无法确认任务已停止,请稍后重试") from None
        if handle and handle.phase == "recovery_required":
            raise chat_operations.ActiveRunError("无法确认工具已停止,请恢复环境后重试")

    await chat_operations.drain_session_work(conversation_id)

    threads = await run_store.graph_threads(conversation_id) if is_db_ready() else {conversation_id}
    marker = PENDING_PURGE_DIR / conversation_id
    tasks = {"checkpoints": sorted(threads), "sandbox": True, "redis": True}
    if marker.exists():
        raise chat_operations.ActiveRunError("会话正在等待清理,请稍后重试")
    _write_pending(marker, tasks)
    try:
        deleted = await repository.delete_conversation(conversation_id)
    except Exception:
        marker.unlink(missing_ok=True)
        raise
    if not deleted:
        marker.unlink(missing_ok=True)
        return False
    await _finish_pending(marker, tasks)
    return True
