from collections.abc import Collection

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage

from app.agents.messages import is_user_message


def call_index_this_turn(runtime: ToolRuntime, names: Collection[str]) -> int:
    """当前这次调用是本轮(最近一条用户消息之后)第几次调用 names 里的工具,从 1 开始。

    按模型发起的 tool_calls 计数而不是数已有的 ToolMessage:同一条回复里并行发起的调用
    看到的是同一份 state,数结果的话它们会一起放行,上限就被绕过了。
    """
    messages = (runtime.state or {}).get("messages") or []
    start = 0
    for i in range(len(messages) - 1, -1, -1):
        if is_user_message(messages[i]):
            start = i + 1
            break
    index = 0
    for msg in messages[start:]:
        if not isinstance(msg, AIMessage):
            continue
        for call in msg.tool_calls:
            if call["name"] in names:
                index += 1
                if call["id"] == runtime.tool_call_id:
                    return index
    return index + 1  # 不在 state 里(直接调用工具时),按下一次算
