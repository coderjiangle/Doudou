"""Observability wiring: audit + cost + tracing assembled from config.

一个入口，三种能力，**零配置时零开销**：没打开的分组直接不构造观察者，
runner 里的循环因此完全不跑。

依赖不满足就不启用（与工具注册同一条原则）：

* 审计/成本需要「库或内存」，两者都有实现，所以默认开；
* 追踪需要 ``langfuse`` 包**且**两个 key **且** 显式 ``enabled: true`` ——
  把对话内容发到第三方服务必须是一次明确的决定，不能靠「装了包就发」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from doudou.config.loader import Settings
from doudou.observability.audit import AuditObserver, AuditStore, MemoryAuditStore
from doudou.observability.buffering import BufferedAuditStore, BufferedUsageStore
from doudou.observability.cost import MemoryUsageStore, PriceTable, UsageObserver, UsageStore
from doudou.observability.logging import get_logger
from doudou.observability.observers import RunObserver
from doudou.observability.tracing import LangfuseTracer, TracingObserver
from doudou.storage.database import Database

logger = get_logger(__name__)


@dataclass(slots=True)
class ObservabilityStack:
    """装配好的观测能力。lifespan 持有它，API 从它读存储。"""

    observers: list[RunObserver] = field(default_factory=list)
    audit_store: AuditStore | None = None
    usage_store: UsageStore | None = None
    usage: UsageObserver | None = None
    tracer: LangfuseTracer | None = None

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
        for store in (self.audit_store, self.usage_store):
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
            "tracing": self.tracer is not None,
        }


def build_observability(
    settings: Settings, *, database: Database | None = None
) -> ObservabilityStack:
    """按配置装配审计 / 成本 / 追踪。"""
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

    tracer = _build_tracer(settings)
    if tracer is not None:
        stack.tracer = tracer
        stack.observers.append(TracingObserver(tracer))

    logger.info("observability.ready", **stack.describe())
    return stack


def _build_tracer(settings: Settings) -> LangfuseTracer | None:
    """构造 Langfuse tracer；任何一处不满足都**安静降级**。"""
    config = settings.app.observability.tracing
    if not config.enabled:
        logger.debug("tracing.disabled")
        return None

    public_key = settings.secrets.langfuse_public_key
    secret_key = settings.secrets.langfuse_secret_key
    if not public_key or not secret_key:
        logger.warning(
            "tracing.missing_credentials",
            hint="observability.tracing.enabled 为真，但缺少 LANGFUSE_PUBLIC_KEY / "
            "LANGFUSE_SECRET_KEY，追踪已关闭",
        )
        return None

    try:
        # 延迟 import：没装 langfuse 时**不应该**让整个服务起不来。
        from langfuse import Langfuse
    except ImportError:
        logger.warning(
            "tracing.sdk_missing",
            hint="未安装 langfuse，请执行 uv sync --extra tracing（或 --all-extras）",
        )
        return None

    try:
        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=config.host,
            environment=config.environment,
            release=config.release,
            sample_rate=_clamp(config.sample_rate),
        )
    except Exception as exc:
        logger.error("tracing.init_failed", error=str(exc), exc_info=True)
        return None

    logger.info(
        "tracing.enabled",
        host=config.host,
        environment=config.environment,
        sample_rate=config.sample_rate,
    )
    return LangfuseTracer(client, release=config.release)


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


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


__all__ = ["ObservabilityStack", "build_observability"]
