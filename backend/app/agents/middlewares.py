from collections import OrderedDict
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelFallbackMiddleware,
    ModelRequest,
    ModelResponse,
    SummarizationMiddleware,
    ToolCallRequest,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage, ToolMessage, get_buffer_string
from langgraph.config import get_stream_writer
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command, interrupt

from app.agents.boundary import BUILTIN_UNTRUSTED_TOOLS, wrap_result
from app.agents.llms import get_fallback_models, get_model
from app.agents.messages import is_user_message
from app.agents.permissions import policy_for
from app.agents.prompts import SEPERATOR, SKILL_USAGE_INSTRUCTIONS
from app.agents.risk import risky_reason
from app.agents.skills_helper import get_skills_xml
from app.config import settings
from app.db import is_db_ready, repository
from app.owner import current_owner
from app.services.mcp_client import confirm_tool_names, mcp_tool_names
from app.services.memory import INJECT_LIMIT, format_for_prompt, select_memories
from app.services.web_access import FETCH_TOOL, known_urls, url_key
from app.utils import logger


class _SummaryFailed(Exception):
    pass


class SafeSummarizationMiddleware(SummarizationMiddleware):
    """摘要失败时保留原历史。

    上游在摘要调用出错时返回 "Error generating summary: ..." 文本,并照样用它替换掉早期对话,
    一次 502 就会让模型永久丢失前文。这里主模型失败后按顺序试备用模型,全部失败则本次不摘要,
    下一次模型调用前再试。不用 model.with_fallbacks():上游会调用 self.model._get_ls_params(),
    RunnableWithFallbacks 没有这个方法。
    """

    def __init__(self, *, fallbacks: list[BaseChatModel] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fallbacks = list(fallbacks or [])

    def _determine_cutoff_index(self, messages):
        # 本轮用户消息与工具调用还承担次数限制、记忆注入和 URL 授权依据。
        # 只跳过摘要 HumanMessage 不够:被摘要删除的调用也会导致计数归零。
        # 因此只压缩已结束的轮次,当前轮保留到下次真实用户输入后再压缩。
        current_turn = next(
            (i for i in range(len(messages) - 1, -1, -1) if is_user_message(messages[i])),
            0,
        )
        if current_turn == 1 and messages[0].additional_kwargs.get("lc_source") == "summarization":
            return 0  # 已结束的历史只剩摘要,不要在每次工具循环里重新总结同一条摘要。
        return min(super()._determine_cutoff_index(messages), current_turn)

    def before_model(self, state, runtime):
        try:
            return super().before_model(state, runtime)
        except _SummaryFailed:
            return None

    async def abefore_model(self, state, runtime):
        try:
            return await super().abefore_model(state, runtime)
        except _SummaryFailed:
            return None

    def _summary_prompt(self, messages_to_summarize) -> str | None:
        trimmed = self._trim_messages_for_summary(messages_to_summarize)
        if not trimmed:
            return None
        return self.summary_prompt.format(messages=get_buffer_string(trimmed)).rstrip()

    def _create_summary(self, messages_to_summarize) -> str:
        if not messages_to_summarize:
            return "No previous conversation history."
        prompt = self._summary_prompt(messages_to_summarize)
        if prompt is None:
            return "Previous conversation was too long to summarize."
        for model in [self.model, *self.fallbacks]:
            try:
                return model.invoke(prompt, config=_SUMMARY_CONFIG).text.strip()
            except Exception:
                logger.warning("Summary generation failed", exc_info=True)
        raise _SummaryFailed

    async def _acreate_summary(self, messages_to_summarize) -> str:
        if not messages_to_summarize:
            return "No previous conversation history."
        prompt = self._summary_prompt(messages_to_summarize)
        if prompt is None:
            return "Previous conversation was too long to summarize."
        for model in [self.model, *self.fallbacks]:
            try:
                return (await model.ainvoke(prompt, config=_SUMMARY_CONFIG)).text.strip()
            except Exception:
                logger.warning("Summary generation failed", exc_info=True)
        raise _SummaryFailed


_SUMMARY_CONFIG = {"metadata": {"lc_source": "summarization"}}


def build_summarization_middleware() -> SummarizationMiddleware:
    return SafeSummarizationMiddleware(
        model=get_model(),
        fallbacks=get_fallback_models(),
        trigger=[
            ("tokens", settings.SUMMARY_TRIGGER_TOKENS),
            ("messages", settings.SUMMARY_TRIGGER_MESSAGES),
        ],
        keep=("messages", settings.SUMMARY_KEEP_MESSAGES),
    )


class SkillsMiddleware(AgentMiddleware):
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        currently_available_skills = get_skills_xml()
        if not currently_available_skills:
            return await handler(request)

        skill_addendum = SEPERATOR + SKILL_USAGE_INSTRUCTIONS.format(
            loaded_skills=currently_available_skills
        )
        new_content = list(request.system_message.content_blocks) + [
            {"type": "text", "text": skill_addendum}
        ]
        new_system_message = SystemMessage(content=new_content)
        return await handler(request.override(system_message=new_system_message))


def _ask_allow(question: str) -> bool:
    answer = interrupt({"question": question, "kind": "single_select", "options": ["允许", "拒绝"]})
    return str(answer).strip() == "允许"


def _refusal(request: ToolCallRequest, text: str) -> ToolMessage:
    call = request.tool_call
    return ToolMessage(content=text, tool_call_id=call["id"], name=call["name"], status="error")


class ToolGuardMiddleware(AgentMiddleware):
    """工具调用前的确认/拒绝、调用后的事件推送和不可信内容包裹。

    主 Agent 和子 Agent 都要挂上,否则子 Agent 能绕过这些防护。
    子 Agent 的工具结果不往前端推事件(emit_events=False),否则会混进主 Agent 的时间线。
    """

    def __init__(self, emit_events: bool = True) -> None:
        super().__init__()
        self.emit_events = emit_events

    @staticmethod
    def _confirm_risky(request: ToolCallRequest) -> ToolMessage | None:
        """高风险 bash 命令执行前向用户确认;拒绝时返回 ToolMessage 告知模型。"""
        call = request.tool_call
        if not settings.CONFIRM_RISKY_COMMANDS or call["name"] != "bash_tool":
            return None
        if policy_for("bash_tool") == "confirm":
            return None  # TOOL_POLICY 已让用户确认过每条命令,不再问第二次
        command = (call.get("args") or {}).get("command", "")
        reason = risky_reason(command)
        if not reason:
            return None
        if _ask_allow(f"即将执行高风险操作({reason}),是否允许?\n{command[:300]}"):
            return None
        return _refusal(request, "用户拒绝执行该命令,请不要重试,换一种不破坏数据的方式或询问用户。")

    @staticmethod
    def _confirm_mcp(request: ToolCallRequest) -> ToolMessage | None:
        """外部 MCP 工具(未声明只读且封闭)调用前向用户确认;拒绝时返回 ToolMessage。
        TOOL_POLICY 里显式设为 allow 的工具免确认,由部署者为单个工具放行;
        设为 confirm 时策略检查已经问过,不再问第二次。"""
        call = request.tool_call
        if call["name"] not in confirm_tool_names() or policy_for(call["name"]) in (
            "allow",
            "confirm",
        ):
            return None
        args = str(call.get("args") or {})[:300]
        if _ask_allow(f"即将调用外部 MCP 工具 {call['name']},是否允许?\n参数:{args}"):
            return None
        return _refusal(request, "用户拒绝调用该外部工具,请不要重试,换一种方式或询问用户。")

    @staticmethod
    def _confirm_web_fetch(request: ToolCallRequest) -> ToolMessage | None:
        """web_fetch 要抓的 URL 既不是用户给的、也不在搜索结果里时,先向用户确认。

        这类 URL 多半来自上传文件或网页正文,注入指令可以把会话数据拼进 URL 发给第三方站点,
        所以问题里给出完整 URL。TOOL_POLICY 里把 web_fetch 设为 allow 可整体免确认;
        设为 confirm 时每次调用都已经问过,不再问第二次。
        """
        call = request.tool_call
        if (
            call["name"] != FETCH_TOOL
            or not settings.WEB_FETCH_CONFIRM_UNKNOWN
            or policy_for(FETCH_TOOL) in ("allow", "confirm")
        ):
            return None
        url = str((call.get("args") or {}).get("url", "")).strip()
        if not url.lower().startswith(("http://", "https://")):
            return None  # 工具自己会拒绝,不必打扰用户
        if url_key(url) in known_urls((request.state or {}).get("messages") or []):
            return None
        if _ask_allow(f"即将抓取一个不是你提供、也不在搜索结果里的网址,是否允许?\n{url[:1000]}"):
            return None
        return _refusal(request, "用户拒绝抓取该网址,请不要重试,换一个来源或询问用户。")

    @staticmethod
    def _check_policy(request: ToolCallRequest) -> ToolMessage | None:
        """按 TOOL_POLICY 处理:deny 直接拒绝,confirm 向用户确认,拒绝时返回 ToolMessage。"""
        call = request.tool_call
        level = policy_for(call["name"])
        if level not in ("deny", "confirm"):
            return None
        if level == "confirm":
            args = str(call.get("args") or {})[:300]
            if _ask_allow(f"即将调用工具 {call['name']},是否允许?\n参数:{args}"):
                return None
        return _refusal(request, "该工具的调用未被允许,请不要重试,换一种方式完成或询问用户。")

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        denied = (
            self._check_policy(request)
            or self._confirm_risky(request)
            or self._confirm_mcp(request)
            or self._confirm_web_fetch(request)
        )
        if denied is not None:
            self._emit(request, denied)
            return denied
        try:
            result = await handler(request)
            message = result
            if isinstance(result, Command):
                messages = (result.update or {}).get("messages") or []
                message = messages[-1] if messages else None

            name = request.tool_call["name"]
            if message is not None:
                logger.debug(
                    "tool result received: tool=%s chars=%d",
                    name,
                    len(str(getattr(message, "content", ""))),
                )
                self._emit(request, message)
            if settings.UNTRUSTED_BOUNDARY and (
                name in BUILTIN_UNTRUSTED_TOOLS or name in mcp_tool_names()
            ):
                return wrap_result(result, name)
            return result
        except GraphBubbleUp:
            # interrupt() 等控制流信号,不是工具失败
            raise
        except Exception as exc:
            # 多数工具自己把错误转成结果;漏出来的(MCP 连接断开、数据库错误等)也交给模型处理,
            # 不中断整轮,前端的工具卡片也能收到结果而不是一直转圈
            logger.exception("Tool %s failed", request.tool_call["name"])
            failed = _refusal(request, f"Error: {type(exc).__name__}: {exc}")
            self._emit(request, failed)
            return failed

    def _emit(self, request: ToolCallRequest, message: ToolMessage) -> None:
        if self.emit_events:
            get_stream_writer()(
                {"kind": "tool.result", "name": request.tool_call["name"]}
                | message.to_json()["kwargs"]
            )


class LoggingModelFallbackMiddleware(ModelFallbackMiddleware):
    """与官方 ModelFallbackMiddleware 行为一致,只是切换时记录日志,便于排查中转站故障。"""

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        try:
            return await handler(request)
        except Exception as exc:
            last: Exception = exc
            logger.warning("主模型调用失败,尝试备用模型: %s: %s", type(exc).__name__, exc)
        for model in self.models:
            name = getattr(model, "model_name", None) or getattr(model, "model", "?")
            try:
                response = await handler(request.override(model=model))
                logger.warning("已切换到备用模型 %s", name)
                return response
            except Exception as exc:
                last = exc
                logger.warning("备用模型 %s 也失败: %s: %s", name, type(exc).__name__, exc)
        raise last


class MemoryMiddleware(AgentMiddleware):
    """每轮用户消息的第一次模型调用前,把与该消息最相关的长期记忆放在这条消息前面。

    记忆只放在本次请求里最后一条用户消息的开头,不写入对话状态,所以不会被摘要压缩,
    也不占 checkpoint。不放进系统提示:那里带提示缓存标记,记忆一变,系统提示和整段历史
    都不命中缓存;放在最后一条用户消息里,只有上一轮及之后的内容不命中。
    同一轮内工具循环的后续模型调用复用第一次的结果(按最后一条 HumanMessage 的 id 缓存),
    不再重复读库 / 建索引 / 写库。只有 BM25 命中的记忆才会被 touch 顺延;
    没有命中时退回的"最近使用"记忆只注入、不 touch,否则同样几条永远不会过期。
    读库失败时静默跳过(记忆是增强,不能拖垮对话)。
    """

    _CACHE_MAX = 256

    def __init__(self) -> None:
        super().__init__()
        # (owner, human message id) -> 记忆块;"" 表示本轮没有记忆
        self._turn_cache: OrderedDict[tuple[str, str], str] = OrderedDict()

    async def _addendum(self, request: ModelRequest) -> str:
        owner = current_owner.get()
        last_human = next((m for m in reversed(request.messages) if is_user_message(m)), None)
        key = (owner, last_human.id) if last_human is not None and last_human.id else None
        if key is not None and key in self._turn_cache:
            self._turn_cache.move_to_end(key)
            return self._turn_cache[key]
        memories = await repository.list_memories(owner)
        addendum = ""
        if memories:
            chosen, matched = select_memories(
                memories, last_human.text if last_human is not None else "", INJECT_LIMIT
            )
            if matched:
                await repository.touch_memories([m.id for m in chosen], owner)
            addendum = format_for_prompt(chosen)
        if key is not None:
            self._turn_cache[key] = addendum
            while len(self._turn_cache) > self._CACHE_MAX:
                self._turn_cache.popitem(last=False)
        return addendum

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if not is_db_ready():
            return await handler(request)
        try:
            addendum = await self._addendum(request)
        except Exception as exc:
            logger.warning("读取长期记忆失败,本次跳过: %s", exc)
            return await handler(request)
        index = next(
            (
                i
                for i in range(len(request.messages) - 1, -1, -1)
                if is_user_message(request.messages[i])
            ),
            None,
        )
        if not addendum or index is None:
            return await handler(request)
        human = request.messages[index]
        if isinstance(human.content, str):
            content = f"{addendum}\n\n{human.content}"
        else:
            content = [{"type": "text", "text": addendum}, *human.content]
        messages = list(request.messages)
        messages[index] = human.model_copy(update={"content": content})
        return await handler(request.override(messages=messages))
