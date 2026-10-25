"""web_search：结果格式化、失败处理与凭据门控（Tavily / DeepSeek 两种 provider）。"""

from __future__ import annotations

import json

import httpx
import pytest

from doudou.tools.builtin import web_search

TAVILY_OK = {
    "answer": "综合答案：某公司发布了新模型。",
    "results": [
        {
            "title": "官方公告",
            "url": "https://example.com/news",
            "content": "某公司 于 2026 年发布 新模型，参数规模为 X。",
            "score": 0.9,
        },
        {"title": "媒体报道", "url": "https://news.example.org/a", "content": "报道正文"},
    ],
}


def _client(payload: object = TAVILY_OK, *, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- 门控


def test_no_tool_without_key(settings) -> None:  # type: ignore[no-untyped-def]
    assert web_search.build_tools(settings) == []


def test_tool_created_with_key(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings(env={"TAVILY_API_KEY": "tvly-test"})

    tools = web_search.build_tools(settings)

    assert [item.name for item in tools] == ["web_search"]


def test_unsupported_provider_is_rejected_at_config_load(tools_settings) -> None:  # type: ignore[no-untyped-def]
    """非法 provider 由配置校验挡住，不靠第二个运行时分支兜底。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        tools_settings(env={"DOUDOU_TOOLS__WEB_SEARCH__PROVIDER": "nope"})


# --------------------------------------------------------------------------- DeepSeek


def test_deepseek_provider_needs_its_own_key(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings(env={"DOUDOU_TOOLS__WEB_SEARCH__PROVIDER": "deepseek"})

    assert web_search.build_tools(settings) == []
    assert web_search.missing_requirement(settings) == "缺少 DEEPSEEK_API_KEY"


def test_tavily_key_does_not_unlock_deepseek(tools_settings) -> None:  # type: ignore[no-untyped-def]
    """换 provider 就得换 key —— 不能拿另一家的凭据把工具点亮。"""
    settings = tools_settings(
        env={
            "TAVILY_API_KEY": "tvly-test",
            "DOUDOU_TOOLS__WEB_SEARCH__PROVIDER": "deepseek",
        }
    )

    assert web_search.build_tools(settings) == []


def test_deepseek_tool_created_with_key(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings(
        env={
            "DEEPSEEK_API_KEY": "sk-test",
            "DOUDOU_TOOLS__WEB_SEARCH__PROVIDER": "deepseek",
        }
    )

    tools = web_search.build_tools(settings)

    assert [item.name for item in tools] == ["web_search"]
    assert web_search.missing_requirement(settings) is None


async def test_deepseek_request_shape_and_formatting(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings(
        env={
            "DEEPSEEK_API_KEY": "sk-test",
            "DOUDOU_TOOLS__WEB_SEARCH__PROVIDER": "deepseek",
        }
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.deepseek.com/v1/tools/search"
        captured.update(json.loads(request.content))
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "官方公告",
                        "url": "https://example.com/news",
                        "summary": "长摘要：发布了新模型。",
                        "snippet": "短摘要",
                        "content": "正文",
                        "date": "2026-06-01T00:00:00+00:00",
                        "authority": 3,
                    },
                    "垃圾数据",
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = web_search.build_tools(settings, client=client)[0]

    result = await tool.ainvoke({"query": "DeepSeek 发布"})

    assert captured["query"] == "DeepSeek 发布"
    assert captured["count"] == 5
    assert captured["need_content"] is False
    assert captured["authorization"] == "Bearer sk-test"
    assert "1. 官方公告" in result
    assert "链接：https://example.com/news" in result
    # 优先用 summary（给模型读的长摘要），而不是 snippet
    assert "长摘要" in result
    await client.aclose()


async def test_deepseek_need_content_is_configurable(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings(
        env={
            "DEEPSEEK_API_KEY": "sk-test",
            "DOUDOU_TOOLS__WEB_SEARCH__PROVIDER": "deepseek",
            "DOUDOU_TOOLS__WEB_SEARCH__NEED_CONTENT": "true",
        }
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"results": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = web_search.build_tools(settings, client=client)[0]

    await tool.ainvoke({"query": "q"})

    assert captured["need_content"] is True
    await client.aclose()


async def test_deepseek_handles_empty_results(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings(
        env={
            "DEEPSEEK_API_KEY": "sk-test",
            "DOUDOU_TOOLS__WEB_SEARCH__PROVIDER": "deepseek",
        }
    )
    client = _client({"results": []})
    tool = web_search.build_tools(settings, client=client)[0]

    result = await tool.ainvoke({"query": "没有结果的问题"})

    assert "没有检索到结果" in result
    await client.aclose()


async def test_deepseek_surfaces_error_message(tools_settings) -> None:  # type: ignore[no-untyped-def]
    """402/401 的 body 里有人话，要把它带到异常里，而不是只剩一个状态码。"""
    settings = tools_settings(
        env={
            "DEEPSEEK_API_KEY": "sk-test",
            "DOUDOU_TOOLS__WEB_SEARCH__PROVIDER": "deepseek",
        }
    )
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            402,
            json={"error": {"message": "Insufficient Balance", "type": "invalid_request_error"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = web_search.build_tools(settings, client=client)[0]

    with pytest.raises(httpx.HTTPStatusError, match="Insufficient Balance"):
        await tool.ainvoke({"query": "q"})

    assert len(calls) == 1
    await client.aclose()


# --------------------------------------------------------------------------- 正常路径


async def test_formats_results_with_sources(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings(env={"TAVILY_API_KEY": "tvly-test"})
    client = _client()
    tool = web_search.build_tools(settings, client=client)[0]

    result = await tool.ainvoke({"query": "某公司最新进展"})

    assert "搜索词：某公司最新进展" in result
    assert "综合答案" in result
    assert "1. 官方公告" in result
    assert "链接：https://example.com/news" in result  # 验收要求带来源
    assert "2. 媒体报道" in result
    await client.aclose()


async def test_handles_empty_results(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings(env={"TAVILY_API_KEY": "tvly-test"})
    client = _client({"results": []})
    tool = web_search.build_tools(settings, client=client)[0]

    result = await tool.ainvoke({"query": "没有结果的问题"})

    assert "没有检索到结果" in result
    await client.aclose()


async def test_handles_missing_fields(tools_settings) -> None:  # type: ignore[no-untyped-def]
    """接口字段缺失时不能崩，只要还能给出可用信息就行。"""
    settings = tools_settings(env={"TAVILY_API_KEY": "tvly-test"})
    client = _client({"results": [{"url": "https://example.com/x"}, "垃圾数据"]})
    tool = web_search.build_tools(settings, client=client)[0]

    result = await tool.ainvoke({"query": "q"})

    assert "（无标题）" in result
    await client.aclose()


async def test_truncates_long_snippet(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings(env={"TAVILY_API_KEY": "tvly-test"})
    client = _client({"results": [{"title": "t", "url": "u", "content": "长" * 5000}]})
    tool = web_search.build_tools(settings, client=client)[0]

    result = await tool.ainvoke({"query": "q"})

    assert "已截断" in result
    assert len(result) < 3000
    await client.aclose()


# --------------------------------------------------------------------------- 失败处理


async def test_client_error_is_raised_not_retried(tools_settings) -> None:  # type: ignore[no-untyped-def]
    """401/429 这类错误重试没有意义，直接抛出（由 Agent 转成工具失败）。"""
    settings = tools_settings(env={"TAVILY_API_KEY": "tvly-test"})
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"detail": "bad key"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = web_search.build_tools(settings, client=client)[0]

    with pytest.raises(httpx.HTTPStatusError):
        await tool.ainvoke({"query": "q"})

    assert len(calls) == 1
    await client.aclose()


async def test_transport_error_is_retried(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings(env={"TAVILY_API_KEY": "tvly-test"})
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("网络不可达", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = web_search.build_tools(settings, client=client)[0]

    with pytest.raises(httpx.ConnectError):
        await tool.ainvoke({"query": "q"})

    assert len(calls) == 3  # 默认 3 次尝试
    await client.aclose()


async def test_non_object_payload_is_rejected(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings(env={"TAVILY_API_KEY": "tvly-test"})
    client = _client([1, 2, 3])
    tool = web_search.build_tools(settings, client=client)[0]

    with pytest.raises(ValueError, match="非对象结构"):
        await tool.ainvoke({"query": "q"})
    await client.aclose()
