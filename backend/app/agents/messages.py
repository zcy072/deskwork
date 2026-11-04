"""区分真实用户输入与摘要中间件生成的 HumanMessage。"""

from langchain_core.messages import BaseMessage, HumanMessage


def is_user_message(message: BaseMessage) -> bool:
    return (
        isinstance(message, HumanMessage)
        and message.additional_kwargs.get("lc_source") != "summarization"
    )
