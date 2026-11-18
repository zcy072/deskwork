from functools import lru_cache

from langchain.agents import create_agent
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware

from app.agents.boundary import BOUNDARY_INSTRUCTION
from app.agents.checkpointers import get_checkpointer
from app.agents.context import RunContext
from app.agents.llms import get_fallback_models, get_model
from app.agents.middlewares import (
    LoggingModelFallbackMiddleware,
    MemoryMiddleware,
    SkillsMiddleware,
    ToolGuardMiddleware,
    build_summarization_middleware,
)
from app.agents.permissions import unknown_tools
from app.agents.prompts import (
    FILESYSTEM_ENVIRONMENT_INSTRUCTION,
    IMAGE_INSTRUCTION,
    MAIN_AGENT_INSTRUCTION,
    MULTI_AGENT_INSTRUCTION,
    NO_VISION_INSTRUCTION,
    REVIEW_INSTRUCTION,
    SEPERATOR,
    SUBAGENT_USAGE_INSTRUCTIONS,
    TODO_INSTRUCTION,
    WEB_INSTRUCTION,
)
from app.agents.state import DeepAgentState
from app.agents.subagents import multi_agent_roles, visual_designer_sub_agent
from app.config import settings
from app.services.doc_review import review_enabled
from app.services.image_view import vision_enabled
from app.services.mcp_client import get_mcp_tools
from app.services.web_access import web_enabled
from app.tools import (
    ask_user_input,
    bash_tool,
    create_file,
    create_task_tool,
    display_widget,
    forget,
    insert,
    present_files,
    read_todos,
    recall,
    remember,
    review_document,
    search_document,
    str_replace,
    view_file,
    view_image,
    web_fetch,
    web_search,
    write_todos,
)
from app.utils import logger

# 子 Agent 只读:各角色的 tools 都从这里挑(见 subagents.READ_ONLY_TOOLS)
sub_agent_tools = [bash_tool, view_file, search_document]

built_in_tools = [
    ask_user_input,
    write_todos,
    read_todos,
    bash_tool,
    view_file,
    str_replace,
    create_file,
    insert,
    present_files,
    display_widget,
    search_document,
]

INSTRUCTION = (
    MAIN_AGENT_INSTRUCTION
    + SEPERATOR
    + FILESYSTEM_ENVIRONMENT_INSTRUCTION
    + SEPERATOR
    + TODO_INSTRUCTION
    + SEPERATOR
    + SUBAGENT_USAGE_INSTRUCTIONS
)


@lru_cache(maxsize=1)
def get_agent():
    """首次使用时才构造 Agent(及其模型),import 本模块不再触发 LLM 配置读取。

    外部 MCP 工具在 lifespan 里先加载(init_mcp),所以这里必须在它之后才首次调用。
    """
    return build_agent(model=get_model(), checkpointer=get_checkpointer())


def build_agent(*, model, checkpointer):
    """Assemble one application graph from explicitly owned dependencies."""
    roles = [visual_designer_sub_agent] + (multi_agent_roles if settings.MULTI_AGENT else [])
    task_tool = create_task_tool(sub_agent_tools, roles, model, DeepAgentState)
    fallbacks = get_fallback_models()
    middleware = [build_summarization_middleware(), SkillsMiddleware(), ToolGuardMiddleware()]
    if settings.MEMORY_ENABLED:
        middleware.append(MemoryMiddleware())
    if fallbacks:
        middleware.append(LoggingModelFallbackMiddleware(*fallbacks))
    if settings.PROMPT_CACHING:
        # 放在最后,这样打标记时看到的是其他中间件改完后的最终请求
        middleware.append(AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"))
    tools = (
        built_in_tools
        + ([review_document] if review_enabled() else [])
        + ([view_image] if vision_enabled() else [])
        + ([remember, recall, forget] if settings.MEMORY_ENABLED else [])
        + ([web_search, web_fetch] if web_enabled() else [])
        + get_mcp_tools()
        + [task_tool]
    )
    if unknown := unknown_tools(t.name for t in tools):
        # 也可能是没连上的 MCP 服务的工具;只告警,不阻止启动
        logger.warning("TOOL_POLICY 里有未注册的工具名(拼错了?): %s", ", ".join(unknown))
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=(
            INSTRUCTION
            + (BOUNDARY_INSTRUCTION if settings.UNTRUSTED_BOUNDARY else "")
            + (SEPERATOR + REVIEW_INSTRUCTION if review_enabled() else "")
            + (SEPERATOR + (IMAGE_INSTRUCTION if vision_enabled() else NO_VISION_INSTRUCTION))
            + (SEPERATOR + MULTI_AGENT_INSTRUCTION if settings.MULTI_AGENT else "")
            + (SEPERATOR + WEB_INSTRUCTION if web_enabled() else "")
        ),
        state_schema=DeepAgentState,
        context_schema=RunContext,
        checkpointer=checkpointer,
        middleware=middleware,
    )
