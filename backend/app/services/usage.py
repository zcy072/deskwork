"""累计一轮对话里所有 LLM 调用的 token 用量,供评测与成本统计使用。

纯逻辑(无 IO)。usage_metadata 的字段名来自 langchain-core 的 UsageMetadata。
"""

from typing import Any


class UsageTracker:
    def __init__(self) -> None:
        self.llm_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.by_model: dict[str, dict[str, int]] = {}
        self.models: list[str] = []  # 实际响应的模型(按首次出现排序),回退发生时会有多个
        self._seen: set[str] = set()

    def add(self, message: Any) -> None:
        """累加一条 AIMessage 的用量;同一条消息(按 id)只计一次。

        流式 + updates 模式下同一条消息可能被多次看到,所以必须去重。
        """
        usage = getattr(message, "usage_metadata", None)
        if not usage:
            return
        message_id = getattr(message, "id", None)
        if message_id:
            if message_id in self._seen:
                return
            self._seen.add(message_id)
        self.llm_calls += 1
        metadata = getattr(message, "response_metadata", None) or {}
        model = metadata.get("model_name") or metadata.get("model")
        if model and model not in self.models:
            self.models.append(model)
        tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        self.input_tokens += tin
        self.output_tokens += tout
        stat = self.by_model.setdefault(model or "unknown", {"input_tokens": 0, "output_tokens": 0})
        stat["input_tokens"] += tin
        stat["output_tokens"] += tout
        details = usage.get("input_token_details") or {}
        self.cache_read_tokens += int(details.get("cache_read") or 0)

    def to_event(self) -> dict:
        return {
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "models": list(self.models),
            "by_model": self.by_model,
        }


def aggregate_usage(rows: list[dict]) -> dict:
    """把多轮的 usage 事件汇总成会话级总量。"""
    total = {
        "turns": len(rows),
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "by_model": {},
    }
    for row in rows:
        for key in ("llm_calls", "input_tokens", "output_tokens", "cache_read_tokens"):
            total[key] += int(row.get(key) or 0)
        for model, stat in (row.get("by_model") or {}).items():
            agg = total["by_model"].setdefault(model, {"input_tokens": 0, "output_tokens": 0})
            agg["input_tokens"] += int(stat.get("input_tokens") or 0)
            agg["output_tokens"] += int(stat.get("output_tokens") or 0)
    total["total_tokens"] = total["input_tokens"] + total["output_tokens"]
    return total
