"""Start, health check and shut down MCP sessions with the app.

:class:`McpRuntime` 是 MCP 对外的**唯一门面**：上层（lifespan / CLI / API 路由）
只跟它打交道，不认识 ``langchain-mcp-adapters``。

生命周期很轻，因为适配器的工具是**无状态**的（每次调用自建 session）：
我们只需要在启动时拉一次工具列表，之后按需刷新。因此 ``stop()`` 不做「断开连接」——
没有长连接可断，这里只做记录，避免给人「已经清理干净」的错觉。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from doudou.config.loader import Settings
from doudou.mcp.client import FetchResult, McpClientManager, ServerOverview, ServerStatus
from doudou.mcp.config import McpConfigError, McpManifest, load_manifest
from doudou.observability.logging import get_logger
from doudou.observability.metrics import get_metrics

logger = get_logger(__name__)


class McpRuntime:
    """MCP 的启停、刷新与状态查询。"""

    def __init__(
        self,
        settings: Settings,
        *,
        manager: McpClientManager | None = None,
        manifest: McpManifest | None = None,
    ) -> None:
        self.settings = settings
        self.config = settings.app.mcp
        self.manifest = manifest if manifest is not None else self._load_manifest()
        self.manager = manager or McpClientManager(
            self.manifest, tool_name_prefix=self.config.tool_name_prefix
        )
        self._tools: list[BaseTool] = []
        self._statuses: list[ServerStatus] = []
        self._started = False

    def _load_manifest(self) -> McpManifest:
        """读清单；清单本身不合法时**按没有 MCP** 处理并告警。

        理由：清单是可选配置，一个 YAML 拼写错误不该让整个服务起不来。
        但必须**显式告警**，否则用户会以为 MCP 生效了。
        """
        try:
            return load_manifest(self.settings.mcp_config_path)
        except McpConfigError as exc:
            logger.error("mcp.manifest_invalid", error=str(exc))
            return McpManifest()

    # ---------------------------------------------------------------- 属性

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def started(self) -> bool:
        return self._started

    @property
    def tools(self) -> list[BaseTool]:
        """已拉取到的工具（启动后才有值）。"""
        return list(self._tools)

    @property
    def statuses(self) -> list[ServerStatus]:
        return list(self._statuses)

    @property
    def server_names(self) -> list[str]:
        return [status.name for status in self._statuses] or self.manager.server_names

    # ---------------------------------------------------------------- 生命周期

    async def start(self) -> list[BaseTool]:
        """拉取工具。``mcp.enabled`` 为假时什么都不做。"""
        if not self.enabled:
            logger.debug("mcp.disabled")
            return []
        if not self.manager.specs:
            logger.info("mcp.no_servers")
            self._started = True
            return []

        await self.refresh(reason="startup")
        self._started = True
        return self.tools

    async def refresh(self, *, reason: str = "manual") -> list[BaseTool]:
        """重新拉取工具列表（``POST /mcp/refresh`` 与 ``doudou mcp list`` 用）。

        ``fail_fast`` 为假（默认）时，连不上的 server 只记状态；
        为真时把失败抛出去，让启动直接失败 —— 适合「MCP 是核心依赖」的部署。
        """
        if not self.enabled:
            return []

        result: FetchResult = await self.manager.fetch()
        self._tools = result.tools
        self._statuses = result.statuses

        metrics = get_metrics()
        metrics.incr("mcp.refresh")
        metrics.incr("mcp.tools", len(result.tools))
        metrics.incr("mcp.server_failed", len(result.failed))

        if result.failed:
            names = ", ".join(status.name for status in result.failed)
            message = f"MCP server 拉取失败：{names}"
            if self.config.fail_fast:
                raise McpConfigError(message)
            logger.warning("mcp.degraded", reason=reason, failed=names)

        logger.info(
            "mcp.refreshed",
            reason=reason,
            servers=len(result.statuses),
            tools=len(result.tools),
            failed=len(result.failed),
        )
        return self.tools

    async def stop(self) -> None:
        """标记停止。

        适配器的工具是无状态的（每次调用自建 session），因此这里**没有**长连接要断。
        之所以保留这个方法：一是让上层有统一的启停形状，二是明确记录「停止」，
        避免读者以为存在被遗漏的连接清理。正在执行中的调用会随进程退出而结束。
        """
        self._started = False
        logger.info("mcp.stopped", servers=len(self._statuses))

    # ---------------------------------------------------------------- 检视

    def describe(self) -> list[ServerOverview]:
        """给 ``GET /mcp`` 与 ``doudou mcp list`` 用的结构化快照。"""
        entries: list[ServerOverview] = []
        for spec in self.manager.specs:
            status = next((item for item in self._statuses if item.name == spec.name), None)
            entries.append(
                ServerOverview(
                    name=spec.name,
                    transport=spec.transport,
                    enabled=spec.enabled,
                    read_only=spec.read_only,
                    description=spec.description,
                    target=spec.url or f"{spec.command} {' '.join(spec.args)}".strip(),
                    include_tools=list(spec.include_tools),
                    exclude_tools=list(spec.exclude_tools),
                    ok=status.ok if status else False,
                    tools=list(status.tools) if status else [],
                    filtered_out=list(status.filtered_out) if status else [],
                    error=status.error if status else "尚未拉取",
                    elapsed=status.elapsed if status else None,
                )
            )
        return entries


__all__ = ["McpRuntime"]
