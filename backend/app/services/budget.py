"""单轮对话的预算与防循环守卫。

LangGraph 默认递归上限约 1 万步,等于没有上限:陷入循环的 Agent 会一直消耗 token。
这里在每次 LLM 调用结束后检查三件事:LLM 调用次数、累计 token、同一工具调用连续重复。
任一上限设为 0 表示关闭该项检查。纯逻辑,无 IO。
"""

import json

from app.services.usage import UsageTracker


class BudgetExceeded(Exception):
    """超出预算或检测到循环;消息会原样展示给用户。"""


class BudgetGuard:
    def __init__(self, max_llm_calls: int, max_total_tokens: int, max_repeated_calls: int):
        self.max_llm_calls = max_llm_calls
        self.max_total_tokens = max_total_tokens
        self.max_repeated_calls = max_repeated_calls
        self._last_signature: str | None = None
        self._repeat_count = 0

    @staticmethod
    def _signature(call: dict) -> str:
        args = json.dumps(call.get("args"), sort_keys=True, ensure_ascii=False, default=str)
        return f"{call.get('name')}:{args}"

    def check(self, usage: UsageTracker, tool_calls: list[dict] | None = None) -> None:
        """在一次 LLM 调用完成(其 usage 已累加、tool_calls 已解析)后调用。"""
        event = usage.to_event()
        if self.max_llm_calls and event["llm_calls"] > self.max_llm_calls:
            raise BudgetExceeded(
                f"已达到单轮对话的模型调用上限({self.max_llm_calls} 次),任务被中止。"
                "可以把任务拆小后重试。"
            )
        if self.max_total_tokens and event["total_tokens"] > self.max_total_tokens:
            raise BudgetExceeded(
                f"已达到单轮对话的 token 上限({self.max_total_tokens}),任务被中止。"
            )
        for call in tool_calls or []:
            signature = self._signature(call)
            if signature == self._last_signature:
                self._repeat_count += 1
            else:
                self._last_signature, self._repeat_count = signature, 1
            if self.max_repeated_calls and self._repeat_count >= self.max_repeated_calls:
                raise BudgetExceeded(
                    f"检测到 Agent 连续 {self._repeat_count} 次执行相同的 {call.get('name')} 调用,"
                    "疑似陷入循环,任务被中止。"
                )
