from dataclasses import replace
from typing import NotRequired

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command
from typing_extensions import TypedDict

from app.agents.context import RunContext
from app.agents.middlewares import ToolGuardMiddleware
from app.agents.prompts import TASK_DESCRIPTION_PREFIX
from app.config import settings
from app.services.budget import BudgetExceeded
from app.services.image_view import message_text
from app.utils import get_trace_id, logger


class SubAgent(TypedDict):
    """Configuration for a specialized sub-agent."""

    name: str
    description: str
    system_prompt: str
    tools: NotRequired[list[str]]


def create_task_tool(tools, subagents: list[SubAgent], model, state_schema):
    """Create a task delegation tool that enables context isolation through sub-agents."""
    agents = {}

    # Build tool name mapping for selective tool assignment
    tools_by_name = {t.name: t for t in tools}

    # Create specialized sub-agents based on configurations
    for _agent in subagents:
        # Sub-agents without an explicit tool list get every tool
        _tools = [tools_by_name[t] for t in _agent["tools"]] if "tools" in _agent else tools
        agents[_agent["name"]] = create_agent(
            model,
            system_prompt=_agent["system_prompt"],
            tools=_tools,
            state_schema=state_schema,
            context_schema=RunContext,
            middleware=[
                ToolGuardMiddleware(emit_events=False),
                # 子 Agent 的调用不会出现在主 Agent 的流事件里,BudgetGuard 看不到它,
                # 必须自己限步,否则会无限循环(曾有子 Agent 空转 20 多分钟)。
                # 超限抛异常,由下面的 task 转成错误结果交给主 Agent。
                *(
                    [ModelCallLimitMiddleware(run_limit=limit, exit_behavior="error")]
                    if (limit := settings.SUBAGENT_MAX_LLM_CALLS)
                    else []
                ),
            ],
        )

    # Generate description of available sub-agents for the tool description
    other_agents_string = "\n".join(
        f"- {_agent['name']}: {_agent['description']}" for _agent in subagents
    )

    @tool(description=TASK_DESCRIPTION_PREFIX.format(other_agents=other_agents_string))
    async def task(
        description: str,
        subagent_type: str,
        runtime: ToolRuntime[RunContext],
    ):
        """Delegate a task to a specialized sub-agent with isolated context."""
        call_id = runtime.tool_call_id

        def reply(text: str, *, error: bool = False) -> Command:
            msg = ToolMessage(text, tool_call_id=call_id, status="error" if error else "success")
            return Command(update={"messages": [msg]})

        if subagent_type not in agents:
            return reply(
                f"Error: invoked agent of type {subagent_type}, the only allowed types are "
                f"{[f'`{k}`' for k in agents]}",
                error=True,
            )

        try:
            # 只传任务描述:父 Agent 的 todos 等状态不属于子 Agent
            result = await agents[subagent_type].ainvoke(
                {"messages": [{"role": "user", "content": description}]},
                context=(
                    replace(runtime.context, workspace_mode="read")
                    if isinstance(runtime.context, RunContext)
                    else None
                ),
            )
        except BudgetExceeded:
            raise
        except GraphBubbleUp:
            raise
        except Exception:
            logger.exception("Sub-agent %s failed", subagent_type)
            return reply(
                f"Error: sub-agent {subagent_type} failed (trace id: {get_trace_id()})", error=True
            )

        text = message_text(result["messages"][-1].content)
        if not text:
            return reply(f"Error: sub-agent {subagent_type} returned no text.", error=True)
        return reply(text)

    return task
