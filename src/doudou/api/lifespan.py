"""Startup and shutdown: agent, history store, database."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI

from doudou.agent.approvals import ApprovalBroker
from doudou.agent.factory import AgentBundle, build_agent
from doudou.channels.base import Channel
from doudou.channels.registry import build_enabled_channels, start_channels, stop_channels
from doudou.config.loader import Settings, get_settings
from doudou.gateway.bus import MessageBus
from doudou.gateway.history import HistoryStore, InMemoryHistoryStore
from doudou.gateway.router import MessageRouter
from doudou.gateway.runner import AgentRunner
from doudou.mcp.lifecycle import McpRuntime
from doudou.observability.logging import configure_logging, get_logger
from doudou.observability.metrics import get_metrics
from doudou.observability.setup import ObservabilityStack, build_observability
from doudou.storage.approvals import SqlApprovalStore
from doudou.storage.database import Database, create_database
from doudou.storage.history import SqlHistoryStore
from doudou.version import __version__

logger = get_logger(__name__)

#: 依赖健康状态。
CHECK_OK = "ok"
CHECK_FAILED = "failed"


@dataclass
class AppState:
    """挂在 ``app.state.doudou`` 上的运行期状态。

    Agent 与数据库连接都是**进程级单例**：请求进来只做会话路由与事件转发，
    不在请求里重复构造模型客户端。
    """

    settings: Settings
    bundle: AgentBundle
    runner: AgentRunner
    database: Database | None = None
    store: HistoryStore | None = None
    bus: MessageBus | None = None
    router: MessageRouter | None = None
    channels: list[Channel] = field(default_factory=list)
    mcp: McpRuntime | None = None
    #: 人工确认的中介。所有通道共用它 —— 确认动作可能来自 CLI、HTTP 或飞书卡片。
    broker: ApprovalBroker | None = None
    approval_store: SqlApprovalStore | None = None
    #: 审计 / 成本 / 追踪。API 从它读存储（``/audit``、``/usage``）。
    observability: ObservabilityStack = field(default_factory=ObservabilityStack)
    started_at: float = field(default_factory=time.monotonic)

    @property
    def uptime_seconds(self) -> float:
        return round(time.monotonic() - self.started_at, 3)

    @property
    def version(self) -> str:
        return __version__

    def metrics_snapshot(self) -> dict[str, Any]:
        snapshot = get_metrics().snapshot()
        return {
            "counters": snapshot.counters,
            "durations": snapshot.durations,
            "bus": self.bus.stats.as_dict() if self.bus is not None else {},
        }

    @property
    def channel_names(self) -> list[str]:
        return [channel.name for channel in self.channels]


def build_store(settings: Settings) -> tuple[HistoryStore, Database | None]:
    """按配置选择历史存储。

    ``storage.history: database`` 时落库（重启可续），``memory`` 时只在进程内。

    **保留条数**由 ``storage.max_messages_per_thread`` 决定，它管的是库的增长；
    送进模型的窗口由 ``context.max_input_tokens`` 决定 —— 两者分开：
    压缩是有损的，原始历史要留得下、能回溯。
    """
    retention = settings.app.storage.max_messages_per_thread
    if settings.app.storage.history == "memory":
        logger.info("storage.history_backend", backend="memory", retention=retention)
        return InMemoryHistoryStore(max_messages=retention), None

    database = create_database(settings.app.storage)
    logger.info(
        "storage.history_backend",
        backend="database",
        url=settings.app.storage.database_url,
        retention=retention,
    )
    return SqlHistoryStore(database, max_messages=retention), database


async def create_state(settings: Settings | None = None) -> AppState:
    """构造运行期状态（初始化 schema、装确认中介、拉 MCP 工具、拉起通道）。"""
    resolved = settings or get_settings()
    store, database = build_store(resolved)
    if database is not None:
        await database.create_schema()

    # 确认中介要在建 Agent **之前**装好：Agent 构造时就固定了确认回调。
    # 决策落库 → 同一个 tool_call_id 不会问第二次；待决请求可查（见 /approvals）。
    approval_store = SqlApprovalStore(database) if database is not None else None
    broker = ApprovalBroker(
        timeout=resolved.app.agent.approval_timeout_seconds,
        recorder=approval_store.record if approval_store else None,
        lookup=approval_store.lookup if approval_store else None,
    )

    # MCP 工具必须在建 Agent **之前**拉好：工具列表是 Agent 的一部分，
    # 建完之后再补工具就只能重建 Agent 了。
    mcp = McpRuntime(resolved)
    mcp_tools = await mcp.start()

    bundle = build_agent(resolved, extra_tools=mcp_tools, approval_hook=broker.hook())

    # 审计 / 成本 / 追踪在 runner 之前装好：观察者是在构造时固定的。
    observability = build_observability(resolved, database=database)
    runner = AgentRunner(bundle, store=store, observers=observability.observers)

    # 常驻通道（飞书长连接）需要一条「不阻塞事件循环」的执行路径，
    # 因此装配 bus → router → channels 三级。
    bus = MessageBus()
    await bus.start()
    router = MessageRouter(runner, bus=bus)
    channels = await start_channels(build_enabled_channels(resolved, router, broker=broker))

    logger.info(
        "api.state_ready",
        model=bundle.metadata.get("model"),
        tools=len(bundle.tools),
        mcp_tools=len(mcp_tools),
        history_backend=resolved.app.storage.history,
        channels=[channel.name for channel in channels],
        approvals_backend="database" if approval_store else "memory",
        observers=observability.names,
    )
    return AppState(
        settings=resolved,
        bundle=bundle,
        runner=runner,
        database=database,
        store=store,
        bus=bus,
        router=router,
        channels=channels,
        mcp=mcp,
        broker=broker,
        approval_store=approval_store,
        observability=observability,
    )


async def dispose_state(state: AppState) -> None:
    """释放资源。先停通道（停止收新消息），再停 bus / MCP / 观测，最后关数据库。"""
    await stop_channels(state.channels)
    if state.bus is not None:
        await state.bus.stop()
    if state.mcp is not None:
        await state.mcp.stop()
    # 观测放在数据库之前关：追踪还要 flush 一次，而 flush 可能读库。
    await state.observability.aclose()
    if state.database is not None:
        await state.database.dispose()
    logger.info("api.state_disposed", uptime_seconds=state.uptime_seconds)


def make_lifespan(settings: Settings | None = None):  # type: ignore[no-untyped-def]
    """构造 lifespan 上下文管理器。

    抽成工厂是为了让 :func:`doudou.api.app.create_app` 能注入测试用的 settings ——
    ``FastAPI(lifespan=...)`` 只在构造时接受一次。
    """

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = settings or get_settings()
        configure_logging(
            level=resolved.app.runtime.log_level,
            json_logs=resolved.app.runtime.log_json,
        )
        get_metrics().reset()
        app.state.doudou = await create_state(resolved)
        try:
            yield
        finally:
            state: AppState | None = getattr(app.state, "doudou", None)
            if state is not None:
                await dispose_state(state)

    return _lifespan


__all__ = [
    "CHECK_FAILED",
    "CHECK_OK",
    "AppState",
    "build_store",
    "create_state",
    "dispose_state",
    "make_lifespan",
]
