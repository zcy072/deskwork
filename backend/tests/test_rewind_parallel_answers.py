"""R13 回归:回退并行问题中的某个答案,只清掉它和之后的答案,之前的保留。"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agents.recovery import _unanswered_interrupts
from app.db import repository, runs
from tests.test_run_recovery import runtime, send  # noqa: F401

THREE_ASKS = AIMessage(
    content="",
    tool_calls=[
        {
            "id": f"c{i}",
            "name": "ask_user_input",
            "args": {"question": f"q{i}", "type": "free_text"},
        }
        for i in (1, 2, 3)
    ],
)


async def _pending(graph):
    return _unanswered_interrupts(await graph.aget_state(await runs.graph_config("s")))


async def _answers(graph):
    state = await graph.aget_state(await runs.graph_config("s"))
    return {
        m.tool_call_id: m.content for m in state.values["messages"] if isinstance(m, ToolMessage)
    }


@pytest.mark.parametrize("edited", [0, 1, 2])
async def test_rewinding_one_parallel_answer_keeps_earlier_answers(runtime, edited):  # noqa: F811
    graph = runtime.graph(
        [THREE_ASKS.model_copy(deep=True), AIMessage(content="done"), AIMessage(content="again")]
    )
    await send("question")
    originals = ["a", "b", "c"]
    for answer in originals:
        interrupt = (await _pending(graph))[0].id
        assert (await send(answer, mode="resume", interrupt_id=interrupt)).status in (
            "waiting",
            "complete",
        )
    target = next(
        m
        for m in await repository.get_messages("s")
        if m.role == "user" and m.content == originals[edited]
    )
    # 被回退的问题重新挂起,答案换成新的
    pending = await _pending(graph)
    assert pending == []
    stored = await runs.get(target.prompt_id)
    changed = await send(
        "X",
        mode="resume",
        interrupt_id=stored.input_json["answered_interrupt"],
        rewind_to_message_id=target.id,
    )
    expected = {f"c{i + 1}": f"User answered: {a}" for i, a in enumerate(originals[:edited])}
    expected[f"c{edited + 1}"] = "User answered: X"
    answers = await _answers(graph)
    assert {k: v for k, v in answers.items() if k in expected} == expected
    # 之后的问题回到未回答状态,依次再问
    later = [f"c{i + 1}" for i in range(edited + 1, 3)]
    assert not set(later) & set(answers)
    assert changed.status == ("waiting" if later else "complete")
    assert len(await _pending(graph)) == len(later)
