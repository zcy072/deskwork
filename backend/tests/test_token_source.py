"""ai.token 只转发主模型节点的输出:摘要中间件等内部模型调用的文本不能拼进回答。"""

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.services import chat_operations as ops


class _Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


async def test_summary_text_is_not_streamed_as_answer(monkeypatch):
    summarizer = _Model(messages=iter([AIMessage(content="SUMMARY TEXT")] * 3))
    agent = create_agent(
        model=_Model(messages=iter([AIMessage(content="final answer")])),
        tools=[],
        middleware=[
            SummarizationMiddleware(model=summarizer, trigger=("messages", 2), keep=("messages", 1))
        ],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "s"}}
    # 先写入几条历史,让本轮触发摘要
    await agent.aupdate_state(
        config, {"messages": [HumanMessage("a"), AIMessage("b"), HumanMessage("c")]}
    )

    events: list[tuple[str, dict]] = []

    async def push(_pid, event, data):
        events.append((event, data))

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(ops, "get_agent", lambda: agent)
    monkeypatch.setattr(ops, "_push", push)
    monkeypatch.setattr(ops, "set_thread_status", noop)
    monkeypatch.setattr(ops, "persist_assistant_turn", noop)
    monkeypatch.setattr(ops, "mark_done", noop)
    monkeypatch.setattr(ops, "refresh_running_ttl", noop)
    monkeypatch.setattr(ops, "is_db_ready", lambda: False)

    await ops._run_agent("p", "s", "q")
    text = "".join(d["text"] for e, d in events if e == "ai.token")
    assert "final answer" in text
    assert "SUMMARY" not in text
