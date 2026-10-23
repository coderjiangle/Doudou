"""http_request：域名白名单、SSRF 防护与响应处理。

这些用例必须**离线可跑**：出网路径靠 ``httpx.MockTransport`` 拦截，
DNS 则只对 ``localhost`` 这类 hosts 文件就能解析的名字做真实查询。
"""

from __future__ import annotations

import httpx
import pytest

from doudou.tools.builtin import http
from doudou.tools.builtin.http import HttpToolError, build_tools, domain_allowed, is_public_address

# --------------------------------------------------------------------------- 域名白名单


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("example.com", ["example.com"]),
        ("api.example.com", ["example.com"]),
        ("deep.api.example.com", ["example.com"]),
        ("EXAMPLE.COM", ["example.com"]),
        ("api.example.com", ["*.example.com"]),
        ("example.com", ["other.com", "example.com"]),
    ],
)
def test_domain_allowed(host: str, allowed: list[str]) -> None:
    assert domain_allowed(host, allowed) is True


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("example.com", ["*.example.com"]),  # 通配只匹配子域
        ("evil-example.com", ["example.com"]),  # 后缀拼接不算子域
        ("example.com.evil.net", ["example.com"]),
        ("other.com", ["example.com"]),
        ("", ["example.com"]),
        ("example.com", []),
        ("example.com", [""]),
    ],
)
def test_domain_denied(host: str, allowed: list[str]) -> None:
    assert domain_allowed(host, allowed) is False


# --------------------------------------------------------------------------- SSRF 防护


def test_localhost_is_not_public() -> None:
    """允许列表挡不住「公网域名解析到内网」——必须额外看解析结果。"""
    assert is_public_address("localhost") is False


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "::1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "0.0.0.0"],
)
def test_private_literal_addresses_rejected(host: str) -> None:
    assert is_public_address(host) is False


def test_unresolvable_host_is_rejected() -> None:
    """解析不了就按不安全处理（不能因为 DNS 挂了就放行）。"""
    assert is_public_address("this-host-should-not-exist.invalid") is False


# --------------------------------------------------------------------------- 工具装配


def test_no_tools_without_allowlist(settings) -> None:  # type: ignore[no-untyped-def]
    """没配允许列表 → 工具不存在，而不是「存在但永远失败」。"""
    assert build_tools(settings) == []


def test_tool_registered_with_allowlist(tools_settings) -> None:  # type: ignore[no-untyped-def]
    tools = build_tools(tools_settings(allowed_domains=("example.com",)))

    assert [item.name for item in tools] == ["http_request"]


# --------------------------------------------------------------------------- 出站校验


async def test_rejects_non_http_scheme(tools_settings) -> None:  # type: ignore[no-untyped-def]
    tool = build_tools(tools_settings())[0]

    with pytest.raises(Exception, match="只支持 http/https"):
        await tool.ainvoke({"url": "file:///etc/passwd"})


async def test_rejects_domain_outside_allowlist(tools_settings) -> None:  # type: ignore[no-untyped-def]
    tool = build_tools(tools_settings(allowed_domains=("example.com",)))[0]

    with pytest.raises(Exception, match="不在允许列表中"):
        await tool.ainvoke({"url": "https://evil.test/secret"})


async def test_rejects_private_address_even_if_allowed(tools_settings) -> None:  # type: ignore[no-untyped-def]
    """``localhost`` 在允许列表里也必须被拒 —— 否则这就是一个内网探针。"""
    tool = build_tools(tools_settings(allowed_domains=("localhost",)))[0]

    with pytest.raises(Exception, match="内网或不可用地址"):
        await tool.ainvoke({"url": "http://localhost:8000/admin"})


async def test_rejects_unsupported_method(tools_settings) -> None:  # type: ignore[no-untyped-def]
    tool = build_tools(tools_settings())[0]

    with pytest.raises(Exception, match="不支持 POST 方法"):
        await tool.ainvoke({"url": "https://example.com", "method": "POST"})


# --------------------------------------------------------------------------- 正常路径


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_fetches_and_converts_html(tools_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # DNS 是外部依赖，这里直接放行；真正的公网/内网判定已在上面的单测覆盖。
    monkeypatch.setattr(http, "is_public_address", lambda host: True)
    settings = tools_settings(allowed_domains=("example.com",))
    client = _client(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            html="<html><head><style>p{}</style></head><body><h1>标题</h1><script>x()</script><p>正文</p></body></html>",
        )
    )
    tool = build_tools(settings, client=client)[0]

    result = await tool.ainvoke({"url": "https://example.com/doc"})

    assert "200" in result
    assert "标题" in result
    assert "正文" in result
    assert "x()" not in result  # script 内容必须被丢掉
    assert "<h1>" not in result  # 标签不能进上下文
    await client.aclose()


async def test_returns_json_as_is(tools_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(http, "is_public_address", lambda host: True)
    client = _client(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"ok": True, "items": [1, 2]},
        )
    )
    tool = build_tools(tools_settings(), client=client)[0]

    result = await tool.ainvoke({"url": "https://example.com/api"})

    assert '"ok":true' in result
    await client.aclose()


async def test_truncates_long_body(tools_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(http, "is_public_address", lambda host: True)
    client = _client(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="x" * 5000,
        )
    )
    settings = tools_settings(max_response_chars=500)
    tool = build_tools(settings, client=client)[0]

    result = await tool.ainvoke({"url": "https://example.com/big"})

    assert "已截断" in result
    assert len(result) < 1000
    await client.aclose()


async def test_stops_at_client_error(tools_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """4xx 是客户端错误，重试没有意义；直接把状态码告诉模型。"""
    monkeypatch.setattr(http, "is_public_address", lambda host: True)
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        return httpx.Response(404, headers={"content-type": "text/plain"}, text="nope")

    client = _client(handler)
    tool = build_tools(tools_settings(), client=client)[0]

    result = await tool.ainvoke({"url": "https://example.com/missing"})

    assert "请求失败" in result and "404" in result
    assert len(attempts) == 1
    await client.aclose()


async def test_retries_on_server_error(tools_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """5xx 是服务端问题，值得重试一次。"""
    monkeypatch.setattr(http, "is_public_address", lambda host: True)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, headers={"content-type": "text/plain"}, text="busy")

    client = _client(handler)
    tool = build_tools(tools_settings(), client=client)[0]

    with pytest.raises(Exception, match="503"):
        await tool.ainvoke({"url": "https://example.com/flaky"})

    assert len(calls) == 2
    await client.aclose()


async def test_rejects_binary_content_type(tools_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(http, "is_public_address", lambda host: True)
    client = _client(
        lambda request: httpx.Response(
            200, headers={"content-type": "application/pdf", "content-length": "999"}
        )
    )
    tool = build_tools(tools_settings(), client=client)[0]

    result = await tool.ainvoke({"url": "https://example.com/a.pdf"})

    assert "application/pdf" in result
    await client.aclose()


async def test_head_returns_headers(tools_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(http, "is_public_address", lambda host: True)
    client = _client(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html", "content-length": "42"}
        )
    )
    tool = build_tools(tools_settings(), client=client)[0]

    result = await tool.ainvoke({"url": "https://example.com/page", "method": "head"})

    assert result.startswith("HEAD ")
    assert "content-length: 42" in result
    await client.aclose()


async def test_empty_body_is_reported(tools_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(http, "is_public_address", lambda host: True)
    client = _client(lambda request: httpx.Response(204, headers={"content-type": "text/plain"}))
    tool = build_tools(tools_settings(), client=client)[0]

    result = await tool.ainvoke({"url": "https://example.com/empty"})

    assert "响应体为空" in result
    await client.aclose()


def test_http_tool_error_is_a_value_error() -> None:
    """错误类型要能被上层统一处理成「工具执行失败」而不是崩溃。"""
    assert issubclass(HttpToolError, ValueError)
