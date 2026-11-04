"""联网搜索 / 抓取:调用 DeepSeek 的 Search & Fetch API(POST /v1/tools/search | fetch)。

请求由 DeepSeek 的服务器发出,后端只和 api.deepseek.com 通信,所以不存在访问内网的 SSRF 问题;
但抓取的 URL 会被目标站点看到,文档里的注入指令可以让模型把数据拼进 URL 发出去,
因此 web_fetch 对"不是用户给的、也不是搜索结果里的"URL 需要用户确认(见 ToolGuardMiddleware)。
搜索词只发给 DeepSeek,不会到达第三方。按成功请求计费,失败与空结果不计费。
"""

import asyncio
import os
import re
import secrets
from urllib.parse import urlsplit

import httpx
from langchain_core.messages import BaseMessage, ToolMessage

from app.agents.messages import is_user_message
from app.config import settings
from app.utils import logger

SEARCH_TOOL = "web_search"
FETCH_TOOL = "web_fetch"
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
SPILL_MAX_CHARS = 2_000_000

_URL_RE = re.compile(r"https?://[^\s<>\"'`，。；！？、（）【】《》]+")
_TRAILING = ".,;:!?)]}>'\""


class WebToolError(RuntimeError):
    """信息直接给模型看。"""


def web_enabled() -> bool:
    return bool(settings.WEB_TOOLS_API_KEY.strip())


def _error_text(resp: httpx.Response) -> str:
    try:
        return str(resp.json()["error"]["message"])
    except Exception:
        return resp.text[:200]


async def _post(path: str, body: dict) -> dict:
    url = settings.WEB_TOOLS_BASE_URL.rstrip("/") + path
    headers = {"Authorization": f"Bearer {settings.WEB_TOOLS_API_KEY.strip()}"}
    async with httpx.AsyncClient(timeout=settings.WEB_TOOLS_TIMEOUT) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = await client.post(url, json=body, headers=headers)
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise WebToolError(
                        f"联网服务暂时连不上({type(exc).__name__}),请稍后再试或告知用户。"
                    ) from exc
                resp = None
            if resp is not None:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
                    raise WebToolError(_status_message(resp))
            delay = 0.5 * 2 ** (attempt - 1)
            logger.warning(
                "%s 请求失败,%.1fs 后重试(%d/%d)", path, delay, attempt, MAX_ATTEMPTS - 1
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def _status_message(resp: httpx.Response) -> str:
    code, detail = resp.status_code, _error_text(resp)
    if code == 401:
        return "联网工具的 API Key 无效(WEB_TOOLS_API_KEY),请告知用户检查配置。"
    if code == 402:
        return "联网工具所用账户余额不足,请告知用户充值。"
    if code == 403:
        return f"该网址被抓取服务的安全策略拒绝,换一个来源: {detail}"
    if code == 404:
        return (
            f"抓取不到可用内容(页面不存在、被站点禁止、类型不支持或没有正文),换一个来源: {detail}"
        )
    if code == 429:
        return "联网请求过于频繁(QPS 限制),已重试仍失败;稍后再试,不要连续重复调用。"
    if code >= 500:
        return f"联网服务暂时故障(HTTP {code}),已重试仍失败;请稍后再试或告知用户。"
    return f"请求无效(HTTP {code}): {detail}"


async def search(query: str, count: int) -> list[dict]:
    data = await _post("/v1/tools/search", {"query": query, "count": count})
    return [r for r in data.get("results") or [] if isinstance(r, dict) and r.get("url")]


async def fetch(url: str) -> tuple[str, str]:
    """返回 (标题, Markdown 正文)。"""
    data = await _post("/v1/tools/fetch", {"url": url})
    content = str(data.get("content") or "")
    if not content.strip():
        raise WebToolError("页面没有可提取的正文,换一个来源。")
    return str(data.get("title") or ""), content


def format_search(results: list[dict]) -> str:
    if not results:
        return "没有搜到结果。换几个关键词,或去掉 site:/after: 等限制再试;仍没有就如实告诉用户。"
    parts = []
    for i, r in enumerate(results, 1):
        meta = " · ".join(str(v) for v in (r.get("site_name"), (r.get("date") or "")[:10]) if v)
        text = r.get("summary") or r.get("snippet") or ""
        parts.append(
            f"[{i}] {r.get('title') or '(无标题)'}\n{r['url']}"
            + (f"\n{meta}" if meta else "")
            + f"\n{text}"
        )
    return "\n\n".join(parts)


def save_full_page(scratchpad, virtual_scratchpad: str, content: str) -> str | None:
    """完整正文写到 scratchpad,返回沙箱内路径;写失败返回 None。

    scratchpad 对 agent 可写:随机文件名 + O_EXCL + O_NOFOLLOW,防止预先放好的符号链接把写入引出沙箱。
    """
    name = f"web_fetch_{secrets.token_hex(4)}.md"
    try:
        target = scratchpad / name
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
        os.fchmod(fd, 0o666)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content[:SPILL_MAX_CHARS])
    except OSError as exc:
        logger.warning("保存抓取页面失败: %s", exc)
        return None
    return f"{virtual_scratchpad}/{name}"


def url_key(url: str) -> str:
    """比较用:去掉 #片段 和末尾的 /,scheme/host 小写。"""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}{query}"


def urls_in_text(text: str) -> set[str]:
    return {url_key(m.rstrip(_TRAILING)) for m in _URL_RE.findall(text or "")}


def known_urls(messages: list[BaseMessage]) -> set[str]:
    """不需要确认就能抓取的 URL:用户消息里出现的,以及 web_search 返回的结果链接。

    上传文件、网页正文、命令输出里的 URL 不算:它们可能是注入的,带着会话里的数据。
    搜索结果是搜索引擎事先收录的,不可能含有本会话的数据。
    摘要中间件插入的总结也是 HumanMessage,但内容来自模型,可能复述了注入的 URL,同样不算。
    """
    known: set[str] = set()
    for m in messages:
        if is_user_message(m):
            known |= urls_in_text(m.text)
        elif isinstance(m, ToolMessage) and m.name == SEARCH_TOOL and isinstance(m.artifact, dict):
            known |= {url_key(u) for u in m.artifact.get("urls") or []}
    return known
