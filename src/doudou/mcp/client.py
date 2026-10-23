"""Wrap MultiServerMCPClient and adapt remote tools for LangChain.

## 一个必须知道的代价

``langchain-mcp-adapters`` 的 ``get_tools()`` 返回的工具是**无状态**的：
每次调用都会新建一个 MCP session（见其 docstring 的 note）。对 ``stdio`` server
而言，这等于**每次工具调用都重新拉起一个子进程**。

因此：

* ``stdio`` 适合本地轻量 server（自己写的 Python server 启动约百毫秒级）；
* ``npx`` / ``uvx`` 这类每次都要解析包再启动 node/python 的 server，单次调用会慢到秒级；
* 需要频繁调用的能力优先用 ``http`` transport（远端长驻进程，没有启动成本）。

持久 session 是后续优化项：那需要管理会话生命周期、重连与并发安全，
收益与复杂度都明显更高，M5 不引入。

## 为什么逐个 server 拉取

``get_tools()`` 不带参数时，任一 server 连不上都会整体失败。这里改成
**逐个 server 拉取并各自记录状态**，所以一个坏掉的 server 只影响它自己 ——
这正是 ``mcp.fail_fast: false`` 想要的降级行为。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, cast

from langchain_core.tools import BaseTool

from doudou.mcp.config import McpManifest, McpServerSpec
from doudou.mcp.loader import build_connection, rename_tool
from doudou.observability.logging import get_logger
from doudou.tools.decorators import annotate

logger = get_logger(__name__)


@dataclass(slots=True)
class ServerStatus:
    """一个 server 的拉取结果。"""

    name: str
    transport: str
    #: 是否成功拉到工具。
    ok: bool = False
    #: 实际暴露出来的工具名（已加前缀、已过滤）。
    tools: list[str] = field(default_factory=list)
    #: 被 include/exclude 过滤掉的工具名。
    filtered_out: list[str] = field(default_factory=list)
    error: str | None = None
    #: 拉取耗时（秒）。
    elapsed: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "ok": self.ok,
            "tools": self.tools,
            "filtered_out": self.filtered_out,
            "error": self.error,
            "elapsed": self.elapsed,
        }


@dataclass(slots=True)
class ServerOverview:
    """一个 server 的完整快照，给 CLI / API / JSON 共用。

    刻意做成强类型而不是 ``dict[str, object]``：后者会让每个消费方
    都在 ``object`` 上做类型断言，既难看又容易出错。
    """

    name: str
    transport: str
    enabled: bool = True
    read_only: bool = False
    description: str = ""
    #: stdio 的命令行或 http 的地址。
    target: str = ""
    include_tools: list[str] = field(default_factory=list)
    exclude_tools: list[str] = field(default_factory=list)
    ok: bool = False
    tools: list[str] = field(default_factory=list)
    filtered_out: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "enabled": self.enabled,
            "read_only": self.read_only,
            "description": self.description,
            "target": self.target,
            "include_tools": list(self.include_tools),
            "exclude_tools": list(self.exclude_tools),
            "ok": self.ok,
            "tools": list(self.tools),
            "filtered_out": list(self.filtered_out),
            "error": self.error,
            "elapsed": self.elapsed,
        }


@dataclass(slots=True)
class FetchResult:
    """一次整体拉取的结果。"""

    tools: list[BaseTool] = field(default_factory=list)
    statuses: list[ServerStatus] = field(default_factory=list)

    @property
    def failed(self) -> list[ServerStatus]:
        return [status for status in self.statuses if not status.ok]


class McpClientManager:
    """按清单构造连接，并逐个 server 拉取工具。"""

    def __init__(
        self,
        manifest: McpManifest,
        *,
        tool_name_prefix: bool = True,
    ) -> None:
        self.manifest = manifest
        self.tool_name_prefix = tool_name_prefix
        #: 延迟构造：``MultiServerMCPClient`` 本身很轻，但 import 它会把 mcp 拉起来，
        #: 而多数部署根本没配 MCP server。
        self._client: Any | None = None

    # ---------------------------------------------------------------- 连接

    @property
    def specs(self) -> list[McpServerSpec]:
        return self.manifest.enabled_servers

    @property
    def server_names(self) -> list[str]:
        return [spec.name for spec in self.specs]

    def _get_client(self) -> Any:
        if self._client is None:
            from langchain_mcp_adapters.client import MultiServerMCPClient

            connections = {spec.name: build_connection(spec) for spec in self.specs if spec.enabled}
            # 名称前缀由我们自己加（见 rename_tool）：这样过滤能按**原始**工具名匹配，
            # 也让前缀格式在我们的控制之下。
            # build_connection 返回的结构化 dict 形态与适配器的 Connection TypedDict
            # 一致（由 mcp/loader 的测试锚定），但 mypy 无法静态验证，故 cast。
            self._client = MultiServerMCPClient(cast("Any", connections), tool_name_prefix=False)
        return self._client

    # ---------------------------------------------------------------- 拉取

    async def fetch(self) -> FetchResult:
        """逐个 server 拉取工具，失败只影响它自己。"""
        result = FetchResult()
        if not self.specs:
            return result

        client = self._get_client()
        for spec in self.specs:
            status, tools = await self._fetch_one(client, spec)
            result.statuses.append(status)
            result.tools.extend(tools)
        return result

    async def _fetch_one(
        self, client: Any, spec: McpServerSpec
    ) -> tuple[ServerStatus, list[BaseTool]]:
        status = ServerStatus(name=spec.name, transport=spec.transport)
        started = time.perf_counter()
        try:
            raw_tools: list[BaseTool] = await client.get_tools(server_name=spec.name)
        except Exception as exc:
            status.elapsed = round(time.perf_counter() - started, 4)
            status.error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "mcp.server_failed",
                server=spec.name,
                transport=spec.transport,
                error=status.error,
            )
            return status, []

        kept: list[BaseTool] = []
        for tool in raw_tools:
            original = tool.name
            if not spec.tool_allowed(original):
                status.filtered_out.append(original)
                continue
            # 命名空间与只读声明都必须打在工具上：前者避免多 server 撞名，
            # 后者决定它是否会被自动拦截（默认按有副作用处理）。
            prepared = rename_tool(tool, server=spec.name) if self.tool_name_prefix else tool
            annotate(
                prepared,
                group=spec.source_label,
                side_effects=not spec.read_only,
                timeout=spec.timeout,
            )
            kept.append(prepared)
            status.tools.append(prepared.name)

        status.ok = True
        status.elapsed = round(time.perf_counter() - started, 4)
        logger.info(
            "mcp.server_ready",
            server=spec.name,
            transport=spec.transport,
            tools=len(kept),
            filtered=len(status.filtered_out),
            elapsed=status.elapsed,
        )
        return status, kept

    # ---------------------------------------------------------------- 元信息

    async def server_info(self) -> dict[str, Any]:
        """各 server 的 ``initialize`` 结果（名称、版本、协议能力）。"""
        if not self.specs:
            return {}
        try:
            info = await self._get_client().get_server_info()
        except Exception as exc:
            logger.warning("mcp.server_info_failed", error=str(exc))
            return {}
        return {
            name: {
                "server_name": getattr(getattr(result, "serverInfo", None), "name", None),
                "server_version": getattr(getattr(result, "serverInfo", None), "version", None),
                "protocol_version": getattr(result, "protocolVersion", None),
            }
            for name, result in (info or {}).items()
        }


__all__ = ["FetchResult", "McpClientManager", "ServerOverview", "ServerStatus"]
