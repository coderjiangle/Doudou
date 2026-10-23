"""HTTP request tool with allowlist and timeout guards.

这是本仓库最容易变成 SSRF 入口的地方，因此策略是**默认拒绝**：

1. ``tools.http.allowed_domains`` 为空 → 工具**根本不注册**；
2. 域名必须命中允许列表（精确或子域）；
3. 解析出来的 IP 若是内网/回环/链路本地地址一律拒绝 ——
   光校验域名挡不住「公网域名解析到 10.0.0.1」这类绕过；
4. 只允许 GET / HEAD，且限制重定向次数、响应体大小与超时。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx
from langchain_core.tools import BaseTool, tool

from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.tools.decorators import annotate
from doudou.utils.async_utils import retry_async
from doudou.utils.text import html_to_text, truncate

logger = get_logger(__name__)

GROUP = "http"

#: 只允许无副作用的读方法。写方法留到 M8 与权限规则一起放行。
ALLOWED_METHODS = ("GET", "HEAD")

#: 按内容类型决定是否能把响应体当文本交给模型。
TEXTUAL_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml",
    "application/rss",
    "application/atom",
    "application/javascript",
    "application/yaml",
)


class HttpToolError(ValueError):
    """出站请求被策略拒绝或参数非法。消息会直接回填给模型，必须可操作。"""


def build_tools(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> Sequence[BaseTool]:
    """构造 HTTP 请求工具；未配置允许域名时返回空列表。"""
    config = settings.app.tools.http
    if not config.allowed_domains:
        logger.debug("tools.http_skipped", reason="tools.http.allowed_domains is empty")
        return []

    injected = client

    @tool
    async def http_request(url: str, method: str = "GET") -> str:
        """抓取一个网页或接口，返回纯文本内容。

        Args:
            url: 完整地址，必须带 http:// 或 https:// 前缀。
            method: 只支持 GET 与 HEAD（默认 GET）。

        Returns:
            响应正文的纯文本（HTML 会被剥成文本），或 HEAD 请求的响应头摘要。

        需要读取某个**已知**链接的完整内容时使用本工具；
        只想知道「有哪些相关页面」应该先用 web_search。
        只能访问配置里允许的域名，越界会返回明确的拒绝原因。
        """
        resolved = str(method).strip().upper() or "GET"
        if resolved not in ALLOWED_METHODS:
            raise HttpToolError(
                f"不支持 {resolved} 方法，本工具只允许 {'、'.join(ALLOWED_METHODS)}。"
            )

        await _guard_url(url, allowed=config.allowed_domains)
        response = await _fetch(
            url,
            method=resolved,
            timeout_seconds=config.timeout,
            max_redirects=config.max_redirects,
            user_agent=config.user_agent,
            client=injected,
        )
        return _format_response(url, response, limit=config.max_response_chars)

    annotate(http_request, group=GROUP, timeout=config.timeout + 5)
    return [http_request]


# --------------------------------------------------------------------------- 校验


def domain_allowed(host: str, allowed: list[str]) -> bool:
    """判断域名是否命中允许列表。

    规则（大小写不敏感）：

    - ``example.com``  匹配 ``example.com`` 及其任意子域 ``api.example.com``；
    - ``*.example.com`` 只匹配子域，不匹配 ``example.com`` 本身。
    """
    normalized = host.strip().lower().rstrip(".")
    if not normalized:
        return False
    for entry in allowed:
        pattern = entry.strip().lower().rstrip(".")
        if not pattern:
            continue
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if suffix and normalized.endswith("." + suffix):
                return True
        elif normalized == pattern or normalized.endswith("." + pattern):
            return True
    return False


def is_public_address(host: str) -> bool:
    """解析域名并确认**所有**地址都是公网地址。

    解析失败按不安全处理。这是防 SSRF 的关键一步：
    允许列表里的域名也可能解析到内网地址。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        logger.warning("tools.http_dns_failed", host=host, error=str(exc))
        return False

    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            logger.warning("tools.http_private_address", host=host, address=raw)
            return False
    return True


async def _guard_url(url: str, *, allowed: list[str]) -> None:
    """出站前的全部校验，失败时抛出可读的 :class:`HttpToolError`。"""
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"}:
        raise HttpToolError(
            f"只支持 http/https，收到 {parts.scheme or '（空）'!r}。请给出完整网址。"
        )
    host = parts.hostname or ""
    if not host:
        raise HttpToolError("网址里没有主机名，请给出完整地址。")
    if not domain_allowed(host, allowed):
        raise HttpToolError(
            f"域名 {host} 不在允许列表中，已拒绝访问。"
            f"当前允许：{'、'.join(allowed)}。"
            "如需访问请让用户在 config.yaml 的 tools.http.allowed_domains 里添加。"
        )
    # getaddrinfo 是阻塞调用，但在 DNS 命中缓存时极快；这里放到线程里避免卡住事件循环。
    is_public = await asyncio.to_thread(is_public_address, host)
    if not is_public:
        raise HttpToolError(f"域名 {host} 解析到了内网或不可用地址，出于安全考虑已拒绝访问。")


# --------------------------------------------------------------------------- 请求


async def _fetch(
    url: str,
    *,
    method: str,
    timeout_seconds: float,
    max_redirects: int,
    user_agent: str,
    client: httpx.AsyncClient | None,
) -> httpx.Response:
    headers = {"User-Agent": user_agent, "Accept": "text/html,application/json,text/*;q=0.9"}

    async def _call() -> httpx.Response:
        if client is not None:
            response = await client.request(method, url, headers=headers, timeout=timeout_seconds)
        else:
            async with httpx.AsyncClient(
                timeout=timeout_seconds, follow_redirects=True, max_redirects=max_redirects
            ) as owned:
                response = await owned.request(method, url, headers=headers)
        # 4xx 是客户端错误（重试无意义），只对 5xx 与网络层重试。
        if response.status_code >= 500:
            response.raise_for_status()
        return response

    return await retry_async(
        _call,
        attempts=2,
        retry_on=(httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError),
        label="http_request",
    )


def _format_response(url: str, response: httpx.Response, *, limit: int) -> str:
    """把响应转成模型可读的文本。"""
    status = f"{response.status_code} {response.reason_phrase}".strip()
    if response.status_code >= 400:
        return f"请求失败：{url} 返回 {status}。可以换个地址或稍后再试。"

    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type and not any(content_type.startswith(kind) for kind in TEXTUAL_CONTENT_TYPES):
        size = response.headers.get("content-length", "未知")
        return f"{url} 返回的是 {content_type} 类型（大小 {size}），不是文本内容，无法在此展示。"

    if response.request and response.request.method == "HEAD":
        interesting = ("content-type", "content-length", "last-modified", "etag")
        lines = [f"HEAD {url} → {status}"]
        lines.extend(
            f"{key}: {response.headers[key]}" for key in interesting if key in response.headers
        )
        return "\n".join(lines)

    body = response.text
    text = html_to_text(body) if content_type.startswith("text/html") else body
    text = text.strip()
    if not text:
        return f"{url} → {status}，但响应体为空。"

    original_length = len(text)
    text = truncate(text, limit)
    suffix = "" if original_length <= limit else "\n（内容过长已截断，需要更多请直接访问原始链接）"
    return f"{url} → {status}\n\n{text}{suffix}"


__all__ = [
    "ALLOWED_METHODS",
    "GROUP",
    "HttpToolError",
    "build_tools",
    "domain_allowed",
    "is_public_address",
]
