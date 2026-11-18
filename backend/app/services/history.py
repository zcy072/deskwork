"""把一轮对话的事件流累积成 assistant 消息的 parts,供落库与历史回放使用。

TurnRecorder 是纯逻辑(无 IO);写库由 runtime/events.py 经 db.runs.save 完成。
"""


class TurnRecorder:
    """累积一条 assistant 消息:连续的 ai.token 合并为一个 text part。"""

    def __init__(self) -> None:
        self.parts: list[dict] = []
        self.streamed_text = False

    def record(self, event: str, data: dict) -> None:
        if event == "ai.token":
            text = data.get("text") or ""
            if not text:
                return
            if self.parts and self.parts[-1]["type"] == "text":
                self.parts[-1]["text"] += text
            else:
                self.parts.append({"type": "text", "text": text})
            self.streamed_text = True
        elif event == "ai.tool_call":
            if data.get("text") and not self.streamed_text:
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
            self.streamed_text = False
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


# 这些事件之后把当前进度写库(status="running"),崩溃/重启时已产出的内容不会整段丢失
CHECKPOINT_EVENTS = frozenset(
    {"tool.result", "files.presented", "widget.presented", "ask.user", "sources.presented"}
)
