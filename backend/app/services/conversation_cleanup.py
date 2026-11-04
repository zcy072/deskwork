"""删除会话时的级联清理:数据库、LangGraph checkpoint、沙箱文件、Redis 键。"""

from pathlib import Path

from app.agents.checkpointers import get_checkpointer
from app.db import repository
from app.sandbox import CONTAINER_MANAGER
from app.sandbox.constants import SANDBOX_DATA_ROOT
from app.services import chat_operations
from app.utils import get_thread_status, logger, purge_session

# 沙箱清理失败(如 manager 不可用)时留下的标记,下次启动重试;避免靠"数据库里没有就删"扫孤儿,
# 那样数据库一旦被重置,会把所有会话文件误删。
PENDING_PURGE_DIR = SANDBOX_DATA_ROOT / "pending_purge"


def _mark_pending(conversation_id: str, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / conversation_id).touch()


async def retry_pending_purges(root: Path | None = None) -> int:
    """重试之前失败的沙箱清理,返回成功清理的数量。"""
    root = root or PENDING_PURGE_DIR
    if not root.is_dir():
        return 0
    done = 0
    for marker in root.iterdir():
        try:
            await CONTAINER_MANAGER.purge_session(marker.name)
            marker.unlink(missing_ok=True)
            done += 1
        except Exception:
            logger.warning("Pending sandbox purge for %s still failing", marker.name)
    return done


async def delete_conversation_data(conversation_id: str) -> bool:
    """返回 False 表示会话不存在。生成中的会话先取消,避免删除后 agent 仍在写文件。

    正在启动新一轮的会话拒绝删除(ActiveRunError),否则启动流程会把会话重新写回来。"""
    if conversation_id in chat_operations._starting_sessions:
        raise chat_operations.ActiveRunError(
            f"Session '{conversation_id}' is starting a generation"
        )
    chat_operations._deleting_sessions.add(conversation_id)
    try:
        return await _delete(conversation_id)
    finally:
        chat_operations._deleting_sessions.discard(conversation_id)


async def _delete(conversation_id: str) -> bool:
    if await get_thread_status(conversation_id) == "running":
        try:
            await chat_operations.cancel_chat(conversation_id)
        except (LookupError, RuntimeError):
            logger.warning("Could not cancel running chat %s before delete", conversation_id)

    if not await repository.delete_conversation(conversation_id):
        return False

    # 以下均为尽力而为:库记录已删,个别清理失败只记日志,不让用户的删除请求失败
    for name, cleanup in (
        ("checkpoints", lambda: get_checkpointer().adelete_thread(conversation_id)),
        ("sandbox files", lambda: _purge_sandbox(conversation_id)),
        ("redis keys", lambda: purge_session(conversation_id)),
    ):
        try:
            await cleanup()
        except Exception:
            logger.exception("Failed to clean up %s for conversation %s", name, conversation_id)
    return True


async def _purge_sandbox(conversation_id: str) -> None:
    try:
        await CONTAINER_MANAGER.purge_session(conversation_id)
    except Exception:
        _mark_pending(conversation_id, PENDING_PURGE_DIR)
        raise
