from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from app.tools.turn_limits import call_index_this_turn


def call(name, ident):
    return AIMessage("", tool_calls=[{"name": name, "id": ident, "args": {}}])


def test_summary_does_not_reset_tool_limits():
    messages = [
        HumanMessage("old turn"),
        call("web_search", "old"),
        HumanMessage("current turn"),
        call("web_search", "first"),
        HumanMessage("summary", additional_kwargs={"lc_source": "summarization"}),
        call("view_image", "image"),
        call("web_fetch", "second"),
    ]
    runtime = SimpleNamespace(state={"messages": messages}, tool_call_id="second")
    assert call_index_this_turn(runtime, {"web_search", "web_fetch"}) == 2
    runtime.tool_call_id = "image"
    assert call_index_this_turn(runtime, {"view_image"}) == 1
    messages.extend([HumanMessage("next turn"), call("web_fetch", "next")])
    runtime.tool_call_id = "next"
    assert call_index_this_turn(runtime, {"web_search", "web_fetch"}) == 1
