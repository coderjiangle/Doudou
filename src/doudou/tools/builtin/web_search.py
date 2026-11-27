"""Web search tool backed by a configurable provider.

支持两家：

* **Tavily**（``https://api.tavily.com/search``）；
* **DeepSeek 自研搜索**（``POST https://api.deepseek.com/v1/tools/search``，返回
  ``title`` / ``url`` / ``summary`` 等结构化结果，见官方「网页搜索与抓取」文档）。

都用 httpx 直接打 REST，不引入各家 SDK：少一个依赖、超时与截断完全由我们控制，
测试也能用 ``MockTransport`` 离线跑。

**缺少对应 provider 的 API Key 时本分组不注册任何工具** ——
工具列表应该只包含「现在真的能用」的能力。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx
from langchain_core.tools import BaseTool, tool

from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.tools.decorators import annotate
from doudou.utils.async_utils import retry_async
from doudou.utils.text import truncate

logger = get_logger(__name__)

GROUP = "web"
TAVILY_ENDPOINT = "https://api.tavily.com/search"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/v1/tools/search"

#: 当前 provider 的 key 所在环境变量（两个 provider 共用同一个配置路径）。
API_KEY_ENV = "DOUDOU_TOOLS_WEB_SEARCH_API_KEY"

#: DeepSeek 搜索的 count 上限（接口要求 1–50）。
DEEPSEEK_MAX_COUNT = 50

#: 每条结果的摘要上限，避免一次搜索就把上下文吃满。
MAX_SNIPPET_CHARS = 600


def api_key_for(settings: Settings) -> str | None:
    """当前 provider 对应的 API Key。"""
    config = settings.app.tools.web_search
    if config.api_key:
        return config.api_key
    if config.provider == "deepseek":
        return settings.secrets.deepseek_api_key
    return settings.secrets.tavily_api_key


def missing_requirement(settings: Settings) -> str | None:
    """未配置凭据时返回可读原因（给 ``describe_groups`` 用）。"""
    if api_key_for(settings):
        return None
    return f"缺少 {API_KEY_ENV}"


def build_tools(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> Sequence[BaseTool]:
    """构造联网检索工具；没有凭据时返回空列表。"""
    config = settings.app.tools.web_search
    provider = config.provider
    api_key = api_key_for(settings)
    if not api_key:
        logger.debug(
            "tools.web_search_skipped",
            provider=provider,
            reason=f"missing {API_KEY_ENV}",
        )
        return []

    injected = client

    @tool
    async def web_search(query: str) -> str:
        """联网搜索，返回带来源链接的结果摘要。

        Args:
            query: 搜索词。用自然语言描述要查什么，例如
                "国产大模型 2026 年最新进展"。

        Returns:
            若干条结果，每条包含标题、链接与摘要；若搜索服务返回了综合答案，
            会一并附在最前面。

        仅在需要**实时或近期易变**信息（最新新闻、天气、价格、近期动态、
        最新版本号/文档变更）或需核实具体外部事实与来源链接时调用本工具。
        通用常识、基础概念、常规编程语法与推理问题请直接用自身知识回答，
        不要调用本工具。结果只是一段摘要，需要细节时应再打开具体链接；
        不要只依据标题下结论。
        """
        if provider == "deepseek":
            data = await _deepseek_search(
                query,
                api_key=api_key,
                timeout_seconds=config.timeout,
                max_results=config.max_results,
                need_content=config.need_content,
                client=injected,
            )
        else:
            data = await _tavily_search(
                query,
                api_key=api_key,
                timeout_seconds=config.timeout,
                max_results=config.max_results,
                search_depth=config.search_depth,
                include_answer=config.include_answer,
                client=injected,
            )
        return _format(query, data)

    annotate(web_search, group=GROUP, timeout=config.timeout + 5)
    return [web_search]


async def _tavily_search(
    query: str,
    *,
    api_key: str,
    timeout_seconds: float,
    max_results: int,
    search_depth: str,
    include_answer: bool,
    client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    """调用 Tavily，并把结果转成与 DeepSeek 一致的形状。"""
    return await _post_json(
        TAVILY_ENDPOINT,
        payload={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "include_answer": include_answer,
        },
        headers=None,
        timeout_seconds=timeout_seconds,
        client=client,
        label="web_search.tavily",
    )


async def _deepseek_search(
    query: str,
    *,
    api_key: str,
    timeout_seconds: float,
    max_results: int,
    need_content: bool,
    client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    """调用 DeepSeek 自研搜索，并把结果压成统一的 ``results`` 结构。

    DeepSeek 的字段比 Tavily 多（``summary`` / ``snippet`` / ``content`` /
    ``site_name`` / ``date`` / ``authority``）。这里优先取 ``summary``：
    它是官方为模型阅读准备的长摘要（约 600–800 字），比 ``snippet`` 信息量大，
    又不像 ``content`` 那样动辄整页。
    """
    count = max(1, min(DEEPSEEK_MAX_COUNT, max_results))
    data = await _post_json(
        DEEPSEEK_ENDPOINT,
        payload={"query": query, "count": count, "need_content": need_content},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout_seconds=timeout_seconds,
        client=client,
        label="web_search.deepseek",
    )

    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        return {"results": []}

    results: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("summary") or item.get("snippet") or item.get("content") or "",
            }
        )
    return {"results": results}


async def _post_json(
    url: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str] | None,
    timeout_seconds: float,
    client: httpx.AsyncClient | None,
    label: str,
) -> dict[str, Any]:
    """POST 一个 JSON 接口，返回解析后的对象。

    只对网络层与超时重试：4xx（鉴权、余额、参数）重试没有意义。
    """

    async def _call() -> dict[str, Any]:
        if client is not None:
            response = await client.post(
                url, json=payload, headers=headers, timeout=timeout_seconds
            )
        else:
            async with httpx.AsyncClient(timeout=timeout_seconds) as owned:
                response = await owned.post(url, json=payload, headers=headers)
        _raise_for_status(response)
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"搜索接口返回了非对象结构：{type(data).__name__}")
        return data

    return await retry_async(
        _call,
        attempts=3,
        retry_on=(httpx.TransportError, httpx.TimeoutException),
        label=label,
    )


def _raise_for_status(response: httpx.Response) -> None:
    """把接口的错误体翻成可读的异常信息。

    DeepSeek 用 OpenAI 兼容的 ``{"error": {"message": ...}}``，Tavily 用
    ``{"detail": ...}``；两者都带上，模型才知道是「余额不足」还是「Key 不对」。
    """
    if response.is_success:
        return

    message = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            message = str(error["message"])
        elif isinstance(body.get("detail"), str):
            message = body["detail"]

    raise httpx.HTTPStatusError(
        message or f"搜索接口返回 HTTP {response.status_code}",
        request=response.request,
        response=response,
    )


def _format(query: str, data: dict[str, Any]) -> str:
    """把接口响应压成模型好读的文本，并保留来源链接。"""
    lines: list[str] = [f"搜索词：{query}"]
    answer = data.get("answer")
    if isinstance(answer, str) and answer.strip():
        lines.append(f"\n综合答案：{truncate(answer.strip(), 1000)}")

    raw_results = data.get("results")
    if not isinstance(raw_results, list) or not raw_results:
        lines.append("\n没有检索到结果。可以换一组关键词再试。")
        return "\n".join(lines)

    lines.append(f"\n结果（{len(raw_results)} 条）：")
    for index, item in enumerate(raw_results, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "（无标题）").strip()
        url = str(item.get("url") or "").strip()
        content = " ".join(str(item.get("content") or "").split())
        lines.append(f"{index}. {title}")
        if url:
            lines.append(f"   链接：{url}")
        if content:
            lines.append(f"   摘要：{truncate(content, MAX_SNIPPET_CHARS)}")
    return "\n".join(lines)


__all__ = ["GROUP", "api_key_for", "build_tools", "missing_requirement"]
