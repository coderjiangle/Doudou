"""Startup and shutdown: agent, history store, database."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI

from doudou.agent.factory import AgentBundle, build_agent
from doudou.channels.base import Channel
from doudou.channels.registry import build_enabled_channels, start_channels, stop_channels
from doudou.config.loader import Settings, get_settings
from doudou.gateway.bus import MessageBus
from doudou.gateway.history import HistoryStore, InMemoryHistoryStore
from doudou.gateway.router import MessageRouter
from doudou.gateway.runner import AgentRunner
from doudou.observability.logging import configure_logging, get_logger
from doudou.observability.metrics import get_metrics
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
    """
    if settings.app.storage.history == "memory":
        logger.info("storage.history_backend", backend="memory")
        return InMemoryHistoryStore(), None

    database = create_database(settings.app.storage)
    logger.info(
        "storage.history_backend", backend="database", url=settings.app.storage.database_url
    )
    return SqlHistoryStore(database), database


async def create_state(settings: Settings | None = None) -> AppState:
    """构造运行期状态（首次需要时初始化数据库 schema，并按配置拉起常驻通道）。"""
    resolved = settings or get_settings()
    store, database = build_store(resolved)
    if database is not None:
        await database.create_schema()

    bundle = build_agent(resolved)
    runner = AgentRunner(bundle, store=store)

    # 常驻通道（飞书长连接）需要一条「不阻塞事件循环」的执行路径，
    # 因此装配 bus → router → channels 三级。
    bus = MessageBus()
    await bus.start()
    router = MessageRouter(runner, bus=bus)
    channels = await start_channels(build_enabled_channels(resolved, router))

    logger.info(
        "api.state_ready",
        model=bundle.metadata.get("model"),
        tools=len(bundle.tools),
        history_backend=resolved.app.storage.history,
        channels=[channel.name for channel in channels],
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
    )


async def dispose_state(state: AppState) -> None:
    """释放资源。先停通道（停止收新消息），再停 bus，最后关数据库。"""
    await stop_channels(state.channels)
    if state.bus is not None:
        await state.bus.stop()
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
