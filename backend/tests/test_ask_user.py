"""主动澄清:ask_user_input 通过 interrupt 暂停,用户回答后原线程恢复。"""

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from app.config import settings
from app.db import close_db, init_db
from app.services import chat_operations as chat
from app.services.history import persist_user_turn
from app.tools import ask_user_input


class ScriptedModel(BaseChatModel):
    script: list
    i: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


ASK_CALL = AIMessage(
    content="需要确认",
    tool_calls=[
        {
            "id": "c1",
            "name": "ask_user_input",
            "args": {
                "question": "输出什么格式?",
                "type": "single_select",
                "options": ["PDF", "Word"],
            },
        }
    ],
)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    await init_db()
    saver = InMemorySaver()
    ag = create_agent(
        model=ScriptedModel(script=[ASK_CALL, AIMessage(content="好的,生成 Word")]),
        tools=[ask_user_input],
        checkpointer=saver,
    )
    events: list[tuple[str, dict]] = []
    statuses: list[str] = []

    async def fake_push(prompt_id, event, data):
        events.append((event, data))

    async def fake_status(session_id, status):
        statuses.append(status)

    async def noop(*_a, **_k):
        return None

    # 助手消息要挂在已存在的会话上,否则外键约束会让终态落库失败(现在会如实报错)
    await persist_user_turn("s1", "p1", "转换文档", [])
    monkeypatch.setattr(chat, "get_agent", lambda: ag)
    monkeypatch.setattr(chat, "get_checkpointer", lambda: saver)
    monkeypatch.setattr(chat, "_push", fake_push)
    monkeypatch.setattr(chat, "set_thread_status", fake_status)
    monkeypatch.setattr(chat, "mark_done", noop)
    yield ag, events, statuses
    await close_db()


def _names(events):
    return [e for e, _ in events]


async def test_interrupt_emits_ask_event_and_waits(env, monkeypatch):
    ag, events, statuses = env
    outcomes = []

    async def record(_sid, status):
        outcomes.append(status)

    monkeypatch.setattr(chat, "_record_outcome", record)
    await chat._run_agent("p1", "s1", "<user_query>\n转换文档\n</user_query>", "转换文档")

    ask = [d for e, d in events if e == "ask.user"]
    assert ask == [
        {"question": "输出什么格式?", "kind": "single_select", "options": ["PDF", "Word"]}
    ]
    assert statuses[-1] == "waiting"
    assert outcomes == ["waiting"]  # 会话列表据此显示"等待你的回答"
    assert _names(events)[-1] == "stream.end"
    assert "error" not in _names(events)
    assert await _pending(ag, {"configurable": {"thread_id": "s1"}}) == ask[0]


async def _pending(agent, cfg):
    """当前要回答的提问 payload(没有则 None)。"""
    pending = chat._unanswered_interrupts(await agent.aget_state(cfg))
    return pending[0].value if pending else None


async def test_answer_resumes_same_tool_call(env):
    ag, events, statuses = env
    cfg = {"configurable": {"thread_id": "s1"}}
    await chat._run_agent("p1", "s1", "<user_query>\n转换文档\n</user_query>", "转换文档")
    events.clear()
    await chat._run_agent("p2", "s1", "<user_query>\nWord\n</user_query>", "Word")

    assert statuses[-1] == "complete"
    assert "ask.user" not in _names(events)
    assert await _pending(ag, cfg) is None
    msgs = (await ag.aget_state(cfg)).values["messages"]
    tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
    assert [m.content for m in tool_msgs] == ["User answered: Word"]  # 答案是原文,没被包装
    assert msgs[-1].content == "好的,生成 Word"
    # 恢复没有新增 HumanMessage:提问和回答属于同一个工具调用
    assert sum(m.type == "human" for m in msgs) == 1


async def test_repair_does_not_cancel_pending_question(env):
    ag, _events, _statuses = env
    cfg = {"configurable": {"thread_id": "s1"}}
    await chat._run_agent("p1", "s1", "<user_query>\nx\n</user_query>", "x")
    await chat._repair_cancelled_tool_calls(ag, cfg)
    msgs = (await ag.aget_state(cfg)).values["messages"]
    assert not any(isinstance(m, ToolMessage) for m in msgs)
    assert await _pending(ag, cfg) is not None


def test_recorder_keeps_ask_part():
    from app.services.history import TurnRecorder

    r = TurnRecorder()
    r.record("ask.user", {"question": "Q", "kind": "free_text", "options": []})
    assert r.parts == [{"type": "ask", "question": "Q", "kind": "free_text", "options": []}]


async def test_resume_command_targets_first_of_multiple_interrupts():
    from types import SimpleNamespace as NS

    def agent(*ids):
        snap = NS(interrupts=[NS(id=i, value={"question": i}) for i in ids])

        class A:
            async def aget_state(self, _cfg):
                return snap

        return A()

    assert (await chat._resume_command(agent(), {}, "是")) is None
    assert (await chat._resume_command(agent("a"), {}, "是")).resume == "是"
    # 并行工具调用同时挂起多个确认时,按 id 只回答最早的一个
    assert (await chat._resume_command(agent("a", "b"), {}, "是")).resume == {"a": "是"}


async def test_resume_skips_interrupts_that_already_have_a_result():
    from types import SimpleNamespace as NS

    snap = NS(
        interrupts=[NS(id="a", value={"question": "A"}), NS(id="b", value={"question": "B"})],
        tasks=[
            NS(result={"messages": []}, interrupts=[NS(id="a")]),  # a 已回答
            NS(result=None, interrupts=[NS(id="b")]),
        ],
    )

    class A:
        async def aget_state(self, _cfg):
            return snap

    assert (await chat._resume_command(A(), {}, "x")).resume == {"b": "x"}
    assert await _pending(A(), {}) == {"question": "B"}


async def test_ask_user_input_free_text_defaults(monkeypatch):
    from app.tools import ask_tools

    seen = []
    monkeypatch.setattr(ask_tools, "interrupt", lambda payload: seen.append(payload) or "42")
    out = await ask_user_input.coroutine(question="q?", type="free_text")
    assert out == "User answered: 42"
    assert seen == [{"question": "q?", "kind": "free_text", "options": []}]


def test_ask_user_input_schema_rejects_bad_type():
    with pytest.raises(ValidationError):
        ask_user_input.args_schema.model_validate({"question": "q", "type": "radio"})


PARALLEL_ASK = AIMessage(
    content="",
    tool_calls=[
        {
            "id": "c1",
            "name": "ask_user_input",
            "args": {"question": "选择颜色", "type": "single_select", "options": ["红", "蓝"]},
        },
        {
            "id": "c2",
            "name": "ask_user_input",
            "args": {"question": "选择格式", "type": "single_select", "options": ["PDF", "Word"]},
        },
    ],
)


async def test_parallel_questions_are_shown_and_answered_one_at_a_time(env, monkeypatch):
    """B4:并行提出两个问题时,一次只发一张卡片,答案按顺序对应各自的问题。"""
    _ag, events, statuses = env
    saver = InMemorySaver()
    ag = create_agent(
        model=ScriptedModel(script=[PARALLEL_ASK, AIMessage(content="好的")]),
        tools=[ask_user_input],
        checkpointer=saver,
    )
    monkeypatch.setattr(chat, "get_agent", lambda: ag)
    monkeypatch.setattr(chat, "get_checkpointer", lambda: saver)
    cfg = {"configurable": {"thread_id": "s1"}}

    await chat._run_agent("p1", "s1", "<user_query>\nx\n</user_query>", "x")
    asks = [d["question"] for e, d in events if e == "ask.user"]
    assert asks == ["选择颜色"] and statuses[-1] == "waiting"

    events.clear()
    await chat._run_agent("p2", "s1", "<user_query>\n红\n</user_query>", "红")
    asks = [d["question"] for e, d in events if e == "ask.user"]
    assert asks == ["选择格式"] and statuses[-1] == "waiting"

    events.clear()
    await chat._run_agent("p3", "s1", "<user_query>\nWord\n</user_query>", "Word")
    assert "ask.user" not in _names(events) and statuses[-1] == "complete"
    msgs = (await ag.aget_state(cfg)).values["messages"]
    answers = {m.tool_call_id: m.content for m in msgs if isinstance(m, ToolMessage)}
    assert answers == {"c1": "User answered: 红", "c2": "User answered: Word"}
