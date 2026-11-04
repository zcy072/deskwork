"""摘要调用失败时不能把早期对话换成错误文本;主模型失败时改用备用模型。"""

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.middlewares import SafeSummarizationMiddleware


class _Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


class _Down(_Model):
    def _generate(self, *a, **k):
        raise RuntimeError("502 Bad Gateway")


def _down():
    return _Down(messages=iter([]))


async def _run(summarizer, fallbacks=()):
    agent = create_agent(
        model=_Model(messages=iter([AIMessage(content="final answer")])),
        tools=[],
        middleware=[
            SafeSummarizationMiddleware(
                model=summarizer,
                fallbacks=list(fallbacks),
                trigger=("messages", 2),
                keep=("messages", 1),
            )
        ],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "s"}}
    await agent.aupdate_state(
        config, {"messages": [HumanMessage("fact 0"), AIMessage("b"), HumanMessage("c")]}
    )
    state = await agent.ainvoke({"messages": [HumanMessage("q")]}, config)
    return [m.text for m in state["messages"]]


async def test_failed_summary_keeps_history():
    texts = await _run(_down(), [_down()])
    assert texts[0] == "fact 0"
    assert not any("Error generating summary" in t for t in texts)
    assert texts[-1] == "final answer"


async def test_summary_uses_fallback_model():
    fallback = _Model(messages=iter([AIMessage(content="SUMMARY TEXT")]))
    texts = await _run(_down(), [fallback])
    assert "fact 0" not in texts
    assert any("SUMMARY TEXT" in t for t in texts)
    assert texts[-1] == "final answer"


async def test_summary_keeps_current_turn_and_its_tool_calls():
    user = HumanMessage("current question", id="current")
    calls = AIMessage("", tool_calls=[{"name": "web_search", "args": {}, "id": "c1"}])
    result = ToolMessage("result", tool_call_id="c1")
    middleware = SafeSummarizationMiddleware(
        model=_Model(messages=iter([AIMessage("old history summary")])),
        trigger=("messages", 2),
        keep=("messages", 1),
    )
    messages = [HumanMessage("old question"), AIMessage("old answer"), user, calls, result]
    update = await middleware.abefore_model({"messages": messages}, None)
    assert update["messages"][-3:] == [user, calls, result]
    # 去掉 RemoveMessage 指令后即为应用摘要后的状态;不能继续重复摘要。
    assert await middleware.abefore_model({"messages": update["messages"][1:]}, None) is None
    # 没有已结束的轮次时跳过摘要,不能删掉正在执行的调用记录。
    assert await middleware.abefore_model({"messages": [user, calls, result]}, None) is None
