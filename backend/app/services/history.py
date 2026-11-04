"""把一轮对话的事件流落库,供会话列表与历史回放使用。

TurnRecorder 是纯逻辑(无 IO),按事件顺序累积 assistant 消息的 parts;
persist_* 函数负责写库,失败只记日志,绝不影响实时流。
"""

import asyncio
import time

from app.db import is_db_ready, repository
from app.utils import logger


class TurnRecorder:
    """累积一条 assistant 消息:连续的 ai.token 合并为一个 text part。"""

    def __init__(self) -> None:
        self.parts: list[dict] = []
        self.message_id: int | None = None  # 首次落库后记下行 id,之后按主键更新
        self.last_write: float = 0.0  # 上次落库的 monotonic 时间,用于合并 running 快照

    def record(self, event: str, data: dict) -> None:
        if event == "ai.token":
            text = data.get("text") or ""
            if not text:
                return
            if self.parts and self.parts[-1]["type"] == "text":
                self.parts[-1]["text"] += text
            else:
                self.parts.append({"type": "text", "text": text})
        elif event == "ai.tool_call":
            if data.get("text"):
                self.record("ai.token", {"text": data["text"]})
            for call in data.get("tool_calls", []):
                self.parts.append(
                    {
                        "type": "tool_call",
                        "id": call.get("id"),
                        "name": call.get("name"),
                        "args": call.get("args"),
                    }
                )
        elif event == "tool.result":
            self.parts.append({"type": "tool_result", **data})
        elif event == "files.presented":
            self.parts.append({"type": "files", "files": data.get("files", [])})
        elif event == "ask.user":
            self.parts.append({"type": "ask", **data})
        elif event == "widget.presented":
            self.parts.append({"type": "widgets", "widgets": data.get("widgets", [])})
        elif event == "sources.presented":
            self.parts.append({"type": "sources", "sources": data.get("sources", [])})

    @property
    def text(self) -> str:
        return "".join(p["text"] for p in self.parts if p["type"] == "text")


async def persist_user_turn(
    session_id: str,
    prompt_id: str,
    user_query: str,
    uploaded_files: list[dict],
    owner: str = "",
) -> None:
    if not is_db_ready():
        return
    try:
        await repository.ensure_conversation(session_id, user_query, owner)
        await repository.add_user_message_with_files(
            session_id,
            prompt_id=prompt_id,
            content=user_query,
            parts=[{"type": "text", "text": user_query}],
            files=uploaded_files,
        )
        await repository.clear_run_state(session_id)
    except Exception:
        # 不能吞掉:新会话的行没写进去时,接着跑的任务对所有读接口都是 404,用户看不到却在消耗 token
        logger.exception("Failed to persist user turn for session '%s'", session_id)
        raise


# 这些事件之后把当前进度写库(status="running"),崩溃/重启时已产出的内容不会整段丢失
CHECKPOINT_EVENTS = frozenset(
    {"tool.result", "files.presented", "widget.presented", "ask.user", "sources.presented"}
)


# running 快照的最小写库间隔(秒);终态(status != "running")总是写
RUNNING_SNAPSHOT_MIN_INTERVAL = 1.5


# 终态写库失败时的重试次数与间隔(秒);running 快照不重试,下一个检查点会再写
TERMINAL_SAVE_ATTEMPTS = 3
TERMINAL_SAVE_RETRY_DELAY = 0.5


async def persist_assistant_turn(
    session_id: str,
    prompt_id: str,
    recorder: TurnRecorder,
    status: str,
    usage: dict | None = None,
) -> bool:
    """写入 assistant 消息。返回 True 表示已保存(或无需保存),False 表示写库失败。

    终态(status != "running")会有限重试;重试按 recorder.message_id / prompt_id 更新同一行,不会重复建消息。
    """
    if not is_db_ready() or not recorder.parts:
        return True
    now = time.monotonic()
    if status == "running" and now - recorder.last_write < RUNNING_SNAPSHOT_MIN_INTERVAL:
        return True
    attempts = 1 if status == "running" else TERMINAL_SAVE_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            msg = await repository.save_assistant_message(
                session_id,
                prompt_id,
                content=recorder.text,
                parts=recorder.parts,
                status=status,
                usage=usage,
                message_id=recorder.message_id,
            )
            recorder.message_id = msg.id
            recorder.last_write = now
            return True
        except Exception:
            logger.exception(
                "Failed to persist assistant turn for session '%s' (attempt %d/%d)",
                session_id,
                attempt,
                attempts,
            )
            if attempt < attempts:
                await asyncio.sleep(TERMINAL_SAVE_RETRY_DELAY)
    return False
