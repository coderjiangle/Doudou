"""Web search tool backed by a configurable search provider.

当前只实现 Tavily。用 httpx 直接打 REST 接口，不引入 tavily-python：
少一个依赖、超时与截断完全由我们控制，且工具层本来就需要 httpx。

**缺少 ``TAVILY_API_KEY`` 时本分组不注册任何工具** ——
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
#: 每条结果的摘要上限，避免一次搜索就把上下文吃满。
MAX_SNIPPET_CHARS = 600


def build_tools(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> Sequence[BaseTool]:
    """构造联网检索工具；没有凭据时返回空列表。"""
    api_key = settings.secrets.tavily_api_key
    if not api_key:
        logger.debug("tools.web_search_skipped", reason="missing TAVILY_API_KEY")
        return []

    # provider 取值由 config 的 Literal 约束（目前只有 tavily），因此这里不做分支判断 ——
    # 非法取值在加载配置时就会被拒掉，多一层运行时判断只会变成永不触发的死代码。
    config = settings.app.tools.web_search
    injected = client

    @tool
    async def web_search(query: str) -> str:
        """联网搜索，返回带来源链接的结果摘要。

        Args:
            query: 搜索词。用自然语言描述要查什么，例如
                "国产大模型 2026 年最新进展"。

        Returns:
            若干条结果，每条包含标题、链接与摘要；若搜索服务给出了
            综合答案，会一并附在最前面。

        需要**实时或外部**信息时使用本工具（新闻、价格、文档、版本号等），
        不要凭训练数据里的记忆回答这类问题。结果只是一段摘要，
        需要细节时应再打开具体链接；不要只依据标题下结论。
        """
        results = await _search(
            query,
            api_key=api_key,
            timeout_seconds=config.timeout,
            max_results=config.max_results,
            search_depth=config.search_depth,
            include_answer=config.include_answer,
            client=injected,
        )
        return _format(query, results)

    annotate(web_search, group=GROUP, timeout=config.timeout + 5)
    return [web_search]


async def _search(
    query: str,
    *,
    api_key: str,
    timeout_seconds: float,
    max_results: int,
    search_depth: str,
    include_answer: bool,
    client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    """调用搜索接口并把结果转成结构化数据。"""
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": include_answer,
    }

    async def _call() -> dict[str, Any]:
        if client is not None:
            response = await client.post(TAVILY_ENDPOINT, json=payload, timeout=timeout_seconds)
        else:
            async with httpx.AsyncClient(timeout=timeout_seconds) as owned:
                response = await owned.post(TAVILY_ENDPOINT, json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"搜索接口返回了非对象结构：{type(data).__name__}")
        return data

    # 只对网络层与 5xx 重试：4xx（含配额与鉴权）重试没有意义。
    return await retry_async(
        _call,
        attempts=3,
        retry_on=(httpx.TransportError, httpx.TimeoutException),
        label="web_search",
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


__all__ = ["GROUP", "build_tools"]
