"""Observability wiring: audit + cost + traces assembled from config.

一个入口，三种能力，**零配置时零开销**：没打开的分组直接不构造观察者，
runner 里的循环因此完全不跑。

依赖不满足就不启用（与工具注册同一条原则）：

* 审计/成本需要「库或内存」，两者都有实现，所以默认开；
* 轨迹也默认开 —— 它是**本地落库**（``run_traces`` 表），不把对话发到任何外部服务。
  无库时用内存实现（有容量上限）。

历史：这里曾经构造 Langfuse 追踪（第三方上报）。现已改为本地落库 + 自建统计页
（``/traces`` / ``/stats``），见 docs/observability.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from doudou.config.loader import Settings
from doudou.observability.audit import AuditObserver, AuditStore, MemoryAuditStore
from doudou.observability.buffering import (
    BufferedAuditStore,
    BufferedTraceStore,
    BufferedUsageStore,
)
from doudou.observability.cost import MemoryUsageStore, PriceTable, UsageObserver, UsageStore
from doudou.observability.logging import get_logger
from doudou.observability.observers import RunObserver
from doudou.observability.traces import MemoryTraceStore, TraceObserver, TraceStore
from doudou.storage.database import Database

logger = get_logger(__name__)


@dataclass(slots=True)
class ObservabilityStack:
    """装配好的观测能力。lifespan 持有它，API 从它读存储。"""

    observers: list[RunObserver] = field(default_factory=list)
    audit_store: AuditStore | None = None
    usage_store: UsageStore | None = None
    usage: UsageObserver | None = None
    trace_store: TraceStore | None = None

    @property
    def names(self) -> list[str]:
        return [getattr(item, "name", type(item).__name__) for item in self.observers]

    @property
    def audit_backend(self) -> str | None:
        """``database`` / ``memory``。

        必须从栈上问，而不是让调用方拿 store 去 ``type(...).__name__`` ——
        攒批写把真身包在 ``BufferedAuditStore.inner`` 里，
        直接看类型会把 ``database`` 误报成 ``memory``（那是「看起来能用、
        其实数据在内存里」的错误信息）。
        """
        return _backend_name(self.audit_store)

    @property
    def usage_backend(self) -> str | None:
        return _backend_name(self.usage_store)

    @property
    def trace_backend(self) -> str | None:
        return _backend_name(self.trace_store)

    async def aclose(self) -> None:
        for observer in self.observers:
            closer = getattr(observer, "aclose", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:
                logger.warning("observability.close_failed", error=str(exc))
        self.observers.clear()
        # 缓冲的写入必须在这里落盘：攒批的代价就是「不主动 flush 就可能丢」。
        for store in (self.audit_store, self.usage_store, self.trace_store):
            flush = getattr(store, "flush", None)
            if flush is None:
                continue
            try:
                await flush()
            except Exception as exc:
                logger.warning("observability.flush_failed", error=str(exc))

    def describe(self) -> dict[str, Any]:
        return {
            "observers": self.names,
            "audit_backend": self.audit_backend,
            "usage_backend": self.usage_backend,
            "trace_backend": self.trace_backend,
        }


def build_observability(
    settings: Settings, *, database: Database | None = None
) -> ObservabilityStack:
    """按配置装配审计 / 成本 / 轨迹。"""
    config = settings.app.observability
    stack = ObservabilityStack()

    if config.audit.enabled:
        audit_store: AuditStore = (
            _sql_audit_store(database, config.audit.max_rows)
            if database is not None
            else MemoryAuditStore(max_entries=config.audit.max_memory_entries)
        )
        # 攒批写：SQLite 只允许一个写事务，逐条提交会把吞吐压到个位数。
        # 见 observability/buffering.py 与 docs/performance.md。
        audit_store = BufferedAuditStore(audit_store, max_pending=config.audit.batch_size)
        stack.audit_store = audit_store
        stack.observers.append(
            AuditObserver(
                audit_store,
                max_arg_chars=config.audit.max_arg_chars,
                max_result_chars=config.audit.max_result_chars,
            )
        )

    if config.cost.enabled:
        usage_store: UsageStore = (
            _sql_usage_store(database) if database is not None else MemoryUsageStore()
        )
        usage_store = BufferedUsageStore(usage_store, max_pending=config.cost.batch_size)
        stack.usage_store = usage_store
        stack.usage = UsageObserver(
            usage_store,
            model=settings.app.model.primary,
            prices=PriceTable(config.cost.prices),
            daily_budget=config.cost.daily_budget,
        )
        stack.observers.append(stack.usage)

    if config.traces.enabled:
        trace_store: TraceStore = (
            _sql_trace_store(database, config.traces.max_rows)
            if database is not None
            else MemoryTraceStore(max_entries=config.traces.max_memory_entries)
        )
        trace_store = BufferedTraceStore(trace_store, max_pending=config.traces.batch_size)
        stack.trace_store = trace_store
        stack.observers.append(
            TraceObserver(
                trace_store,
                model=settings.app.model.primary,
                max_text_chars=config.traces.max_text_chars,
            )
        )

    logger.info("observability.ready", **stack.describe())
    return stack


def _backend_name(store: object | None) -> str | None:
    if store is None:
        return None
    # 攒批写会把真身在 ``inner`` 里，别把中间层误报成后端名。
    target = getattr(store, "inner", store)
    return "database" if type(target).__name__.startswith("Sql") else "memory"


def _sql_audit_store(database: Database, max_rows: int) -> AuditStore:
    from doudou.storage.audit import SqlAuditStore

    return SqlAuditStore(database, max_rows=max_rows)


def _sql_usage_store(database: Database) -> UsageStore:
    from doudou.storage.usage import SqlUsageStore

    return SqlUsageStore(database)


def _sql_trace_store(database: Database, max_rows: int) -> TraceStore:
    from doudou.storage.traces import SqlTraceStore

    return SqlTraceStore(database, max_rows=max_rows)


__all__ = ["ObservabilityStack", "build_observability"]
