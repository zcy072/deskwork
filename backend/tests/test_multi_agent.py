"""多 Agent 分工:规划者 / 审查者是只读角色,开关控制是否注册。"""

import pytest

from app.agents.subagents import multi_agent_roles, planner_sub_agent, reviewer_sub_agent
from app.tools import create_task_tool

WRITE_TOOLS = {"create_file", "str_replace", "insert"}


@pytest.mark.parametrize("role", [planner_sub_agent, reviewer_sub_agent])
def test_roles_are_read_only(role):
    assert not WRITE_TOOLS & set(role["tools"])
    assert "Do NOT create, modify or delete" in role["system_prompt"]


def test_role_names_unique_and_registered():
    names = [r["name"] for r in multi_agent_roles]
    assert names == ["planner-agent", "reviewer-agent"]


def test_task_tool_lists_roles():
    from langchain_core.tools import tool

    from app.agents.state import DeepAgentState
    from tests.test_ask_user import ScriptedModel

    names = ["bash_tool", "view_file", "search_document"]
    tools = []
    for n in names:

        @tool(n)
        def _t(x: str = "") -> str:
            """t"""
            return x

        tools.append(_t)
    task = create_task_tool(tools, multi_agent_roles, ScriptedModel(script=[]), DeepAgentState)
    assert "planner-agent" in task.description and "reviewer-agent" in task.description
