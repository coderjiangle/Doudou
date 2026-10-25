"""审计、成本、追踪。

这里钉住的是三条容易写错、且错了**不会立刻暴露**的规则：

1. 参数与结果必须**先打码再截断**（顺序反了会把打码结果切坏）；
2. 「费用未知」不等于 0（把 ``None`` 当 0 会让人以为成本在掌控之中）；
3. 观察者失败**绝不能**影响运行（审计系统坏了，聊天也得能继续）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from doudou.agent.events import RunEvent
from doudou.config.loader import Settings
from doudou.config.schema import StorageConfig
from doudou.observability.audit import (
    AuditEntry,
    AuditObserver,
    MemoryAuditStore,
    summarize_args,
)
from doudou.observability.buffering import (
    BufferedAuditStore,
    BufferedUsageStore,
    pending_count,
)
from doudou.observability.cost import (
    MemoryUsageStore,
    PriceTable,
    UsageObserver,
    UsageRecord,
    build_usage_report,
    today_key,
)
from doudou.observability.observers import (
    BaseObserver,
    RunContext,
    RunSummary,
    notify_end,
    notify_event,
    notify_start,
)
from doudou.observability.setup import build_observability
from doudou.observability.tracing import LangfuseTracer, TracingObserver
from doudou.storage.audit import SqlAuditStore
from doudou.storage.database import Database, create_database
from doudou.storage.usage import SqlUsageStore


def ctx(thread: str = "t1", **extra: Any) -> RunContext:
    return RunContext(thread_id=thread, channel="cli", user_id="u1", text="你好", **extra)


# --------------------------------------------------------------------------- 参数摘要


def test_summarize_args_redacts_secrets() -> None:
    """``api_key`` / ``token`` 之类必须打码 —— 审计表不是密钥仓库。"""
    args, truncated = summarize_args({"api_key": "sk-abcdef123456", "path": "a.md"}, 200)

    assert args["api_key"] != "sk-abcdef123456"
    assert "****" in args["api_key"]
    assert args["path"] == "a.md"
    assert truncated == 0


def test_summarize_args_truncates_per_field() -> None:
    """逐字段截断，且如实报告截掉了多少字。

    整体截断 JSON 会让结果不可解析，审计记录也就查不动了。
    """
    args, truncated = summarize_args({"content": "x" * 500, "path": "a.md"}, 100)

    assert args["path"] == "a.md"  # 短字段不受影响
    assert "截断 400 字" in str(args["content"])
    assert truncated == 400


def test_summarize_args_keeps_non_string_values() -> None:
    args, _ = summarize_args({"max_results": 5, "flag": True}, 100)

    assert args["max_results"] == 5
    assert args["flag"] is True


# --------------------------------------------------------------------------- 审计


async def test_audit_merges_call_and_result_into_one_row() -> None:
    """一次工具调用只应留下一行 —— 分开写审计就变成噪声了。"""
    store = MemoryAuditStore()
    observer = AuditObserver(store)
    context = ctx()

    await observer.on_run_start(context)
    await observer.on_event(
        context,
        RunEvent.tool_call(step=1, name="workspace_write", call_id="c1", args={"path": "a.md"}),
    )
    await observer.on_event(
        context,
        RunEvent.tool_result(
            step=1, name="workspace_write", call_id="c1", result="已创建", ok=True
        ),
    )

    rows = [item for item in await store.list_recent() if item.kind == "tool"]

    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].tool_name == "workspace_write"
    assert rows[0].result_preview == "已创建"
    assert rows[0].result_chars == len("已创建")


async def test_audit_marks_rejections() -> None:
    """被拒绝的调用要能一眼看出来 —— 它和「参数错误」「超时」都是 ok=False。"""
    store = MemoryAuditStore()
    observer = AuditObserver(store)
    context = ctx()

    await observer.on_run_start(context)
    await observer.on_event(
        context, RunEvent.tool_call(step=1, name="workspace_delete", call_id="c1", args={})
    )
    await observer.on_event(
        context,
        RunEvent.approval(
            step=1, name="workspace_delete", call_id="c1", approved=False, reason="用户拒绝"
        ),
    )
    await observer.on_event(
        context,
        RunEvent.tool_result(
            step=1, name="workspace_delete", call_id="c1", result="用户拒绝执行", ok=False
        ),
    )

    row = next(item for item in await store.list_recent() if item.kind == "tool")

    assert row.approved is False
    assert row.status == "rejected"


async def test_audit_records_approval_as_true() -> None:
    store = MemoryAuditStore()
    observer = AuditObserver(store)
    context = ctx()

    await observer.on_event(
        context,
        RunEvent.tool_call(step=2, name="workspace_write", call_id="c9", args={"path": "a.md"}),
    )
    await observer.on_event(
        context, RunEvent.approval(step=2, name="workspace_write", call_id="c9", approved=True)
    )
    await observer.on_event(
        context,
        RunEvent.tool_result(
            step=2, name="workspace_write", call_id="c9", result="已写入", ok=True
        ),
    )

    row = next(item for item in await store.list_recent() if item.kind == "tool")

    assert row.approved is True
    assert row.status == "ok"


async def test_audit_keeps_untouched_tools_as_null_approval() -> None:
    """不需要确认的工具 ``approved`` 必须是 ``None``，不能写成 ``True``。

    写成 ``True`` 会让审计报告「全都经过人确认」，那是假的。
    """
    store = MemoryAuditStore()
    observer = AuditObserver(store)

    await observer.on_event(ctx(), RunEvent.tool_call(step=1, name="now", call_id="c1", args={}))
    await observer.on_event(
        ctx(), RunEvent.tool_result(step=1, name="now", call_id="c1", result="12:00", ok=True)
    )

    row = next(item for item in await store.list_recent() if item.kind == "tool")

    assert row.approved is None


async def test_audit_records_run_and_final_usage() -> None:
    store = MemoryAuditStore()
    observer = AuditObserver(store)
    context = ctx()

    await observer.on_run_start(context)
    await observer.on_run_end(
        context,
        RunSummary(
            ok=True,
            steps=3,
            tool_calls=2,
            approvals=1,
            usage={"input_tokens": 120, "output_tokens": 30},
        ),
    )

    row = next(item for item in await store.list_recent() if item.kind == "run")

    assert row.status == "ok"
    assert row.args["tool_calls"] == 2
    assert row.args["input_tokens"] == 120


async def test_audit_marks_failed_run() -> None:
    store = MemoryAuditStore()
    observer = AuditObserver(store)
    context = ctx()

    await observer.on_run_start(context)
    await observer.on_run_end(context, RunSummary(ok=False, error="达到最大步数"))

    row = next(item for item in await store.list_recent() if item.kind == "run")

    assert row.status == "failed"
    assert row.error == "达到最大步数"


async def test_memory_audit_store_evicts_oldest() -> None:
    store = MemoryAuditStore(max_entries=2)

    for index in range(3):
        await store.append(
            AuditEntry(audit_id=f"a{index}", kind="tool", thread_id="t1", tool_name="now")
        )

    remaining = await store.list_recent(limit=10)

    assert [item.audit_id for item in remaining] == ["a2", "a1"]


async def test_memory_audit_store_filters_by_thread() -> None:
    store = MemoryAuditStore()
    await store.append(AuditEntry(audit_id="a1", kind="tool", thread_id="t1"))
    await store.append(AuditEntry(audit_id="a2", kind="tool", thread_id="t2"))

    assert [item.audit_id for item in await store.list_recent(thread_id="t2")] == ["a2"]


# --------------------------------------------------------------------------- 审计（落库）


@pytest.fixture
async def database(tmp_path: Path):  # type: ignore[no-untyped-def]
    config = StorageConfig(database_url=f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}")
    db: Database = create_database(config)
    await db.create_schema()
    yield db
    await db.dispose()


async def test_sql_audit_upsert_single_row(database: Database) -> None:
    store = SqlAuditStore(database)
    observer = AuditObserver(store)
    context = ctx()

    await observer.on_event(
        context, RunEvent.tool_call(step=1, name="now", call_id="c1", args={"timezone": "UTC"})
    )
    await observer.on_event(
        context, RunEvent.tool_result(step=1, name="now", call_id="c1", result="12:00", ok=True)
    )

    rows = await store.list_recent()

    assert len(rows) == 1
    assert rows[0].args == {"timezone": "UTC"}
    assert rows[0].result_preview == "12:00"


async def test_sql_audit_trim_keeps_newest(database: Database) -> None:
    """审计表不设上限就是磁盘泄漏 —— trim 必须留最近的。"""
    store = SqlAuditStore(database, max_rows=3)

    for index in range(6):
        await store.append(
            AuditEntry(audit_id=f"a{index}", kind="tool", thread_id="t1", tool_name="now")
        )

    removed = await store.trim()
    remaining = await store.list_recent(limit=10)

    assert removed == 3
    assert {item.audit_id for item in remaining} == {"a3", "a4", "a5"}


async def test_sql_audit_get(database: Database) -> None:
    store = SqlAuditStore(database)
    await store.append(AuditEntry(audit_id="a1", kind="run", thread_id="t1"))

    assert (await store.get("a1")) is not None
    assert (await store.get("nope")) is None


# --------------------------------------------------------------------------- 价格


def test_price_table_matches_full_and_short_name() -> None:
    table = PriceTable({"openai:gpt-x": {"input": 1.0, "output": 2.0}})

    assert table.price_for("openai:gpt-x") is not None
    assert table.price_for("gpt-x") is not None  # 短名
    assert table.price_for("OPENAI:GPT-X") is not None  # 大小写不敏感


def test_price_table_unknown_is_none() -> None:
    """查不到价格就返回 ``None`` —— 编一个数字出来比不显示更糟。"""
    assert PriceTable({}).price_for("who-knows") is None
    assert PriceTable({}).cost("who-knows", input_tokens=10, output_tokens=10) is None


def test_price_table_default() -> None:
    table = PriceTable({"default": {"input": 1.0, "output": 1.0}})

    assert table.cost("anything", input_tokens=1_000_000, output_tokens=0) == 1.0
    assert table.known == []  # default 不算「已知模型」


def test_price_table_computes_cost() -> None:
    table = PriceTable({"m": {"input": 3.0, "output": 15.0}})

    # 每 100 万 token 计价
    assert table.cost("m", input_tokens=1_000_000, output_tokens=1_000_000) == 18.0
    assert table.cost("m", input_tokens=500_000, output_tokens=0) == 1.5


# --------------------------------------------------------------------------- 用量


async def test_usage_observer_accumulates() -> None:
    store = MemoryUsageStore()
    observer = UsageObserver(
        store, model="m", prices=PriceTable({"m": {"input": 1.0, "output": 2.0}})
    )
    context = ctx()

    await observer.on_run_end(
        context,
        RunSummary(ok=True, tool_calls=2, usage={"input_tokens": 1000, "output_tokens": 500}),
    )
    await observer.on_run_end(
        context, RunSummary(ok=True, usage={"input_tokens": 1000, "output_tokens": 500})
    )

    input_tokens, output_tokens, cost = await store.total()

    assert (input_tokens, output_tokens) == (2000, 1000)
    assert cost == pytest.approx(2000 / 1_000_000 + 1000 * 2 / 1_000_000)


async def test_usage_observer_unknown_price_is_none_not_zero() -> None:
    """未知价格必须一路保持 ``None``，不能在中途被当成 0。"""
    store = MemoryUsageStore()
    observer = UsageObserver(store, model="mystery", prices=PriceTable({}))

    await observer.on_run_end(ctx(), RunSummary(usage={"input_tokens": 10, "output_tokens": 5}))

    _input, _output, cost = await store.total()

    assert cost is None
    assert observer.unknown_models == ["mystery"]


async def test_usage_merges_known_with_unknown_as_unknown() -> None:
    """已知 + 未知 = 未知。把未知当 0 会少算钱，那比不显示更危险。"""
    store = MemoryUsageStore()
    known = UsageObserver(store, model="m", prices=PriceTable({"m": {"input": 1.0, "output": 0.0}}))
    unknown = UsageObserver(store, model="mystery", prices=PriceTable({}))

    await known.on_run_end(ctx(), RunSummary(usage={"input_tokens": 1_000_000}))
    await unknown.on_run_end(ctx(), RunSummary(usage={"input_tokens": 1_000_000}))

    assert (await store.total())[2] is None


async def test_usage_budget_warns_each_day_once(caplog: pytest.LogCaptureFixture) -> None:
    store = MemoryUsageStore()
    observer = UsageObserver(
        store,
        model="m",
        prices=PriceTable({"m": {"input": 1_000_000.0, "output": 0.0}}),
        daily_budget=1.0,
    )

    for _ in range(3):
        await observer.on_run_end(ctx(), RunSummary(usage={"input_tokens": 10}))

    assert observer._warned_on == today_key()


async def test_usage_budget_not_exceeded_keeps_quiet() -> None:
    store = MemoryUsageStore()
    observer = UsageObserver(
        store,
        model="m",
        prices=PriceTable({"m": {"input": 100.0, "output": 0.0}}),
        daily_budget=10.0,
    )

    await observer.on_run_end(ctx(), RunSummary(usage={"input_tokens": 10}))

    assert observer._warned_on is None


async def test_memory_usage_store_groups_by_day_and_thread() -> None:
    """按天/按会话聚合，且只统计最近 ``days`` 天。"""
    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    store = MemoryUsageStore()

    await store.add(
        UsageRecord(day=yesterday.isoformat(), thread_id="t1", input_tokens=10, cost=0.1)
    )
    await store.add(UsageRecord(day=today.isoformat(), thread_id="t1", input_tokens=5, cost=0.2))
    await store.add(UsageRecord(day=today.isoformat(), thread_id="t2", input_tokens=7, cost=0.3))
    # 超出窗口的历史数据不该被算进来
    await store.add(
        UsageRecord(day=(today - timedelta(days=30)).isoformat(), thread_id="t3", input_tokens=999)
    )

    by_day = {item.day: item for item in await store.by_day(days=2)}
    by_thread = {item.thread_id: item for item in await store.by_thread(days=2)}

    assert by_day[today.isoformat()].input_tokens == 12
    assert by_day[today.isoformat()].cost == pytest.approx(0.5)
    assert by_thread["t1"].input_tokens == 15
    assert "t3" not in by_thread


async def test_sql_usage_accumulates_and_aggregates(database: Database) -> None:
    store = SqlUsageStore(database)
    day = today_key()

    await store.add(UsageRecord(day=day, thread_id="t1", channel="cli", input_tokens=100, cost=0.5))
    await store.add(UsageRecord(day=day, thread_id="t1", input_tokens=50, runs=1, cost=0.25))
    await store.add(UsageRecord(day=day, thread_id="t2", input_tokens=7, cost=None))

    assert (await store.total(day=day))[:2] == (157, 0)
    by_thread = {item.thread_id: item for item in await store.by_thread(days=2)}
    assert by_thread["t1"].input_tokens == 150
    assert by_thread["t1"].cost == pytest.approx(0.75)
    # t2 的价格未知 → 它的汇总费用是未知，而不是 0.75
    assert by_thread["t2"].cost is None
    assert (await store.total(day=day))[2] is None


async def test_build_usage_report_budget_boundary() -> None:
    """预算判定的边界：``>`` 才算超，恰好等于不算。"""
    common = {
        "records_by_day": [],
        "records_by_thread": [],
        "total": (0, 0, 0.0),
        "model": "m",
        "unknown_models": [],
        "prices_known": ["m"],
    }

    exact = build_usage_report(today=(0, 0, 1.0), budget=1.0, **common)  # type: ignore[arg-type]
    over = build_usage_report(today=(0, 0, 1.01), budget=1.0, **common)  # type: ignore[arg-type]
    unknown = build_usage_report(today=(0, 0, None), budget=1.0, **common)  # type: ignore[arg-type]

    assert exact["budget"]["exceeded"] is False
    assert over["budget"]["exceeded"] is True
    # 费用未知时不该报「超预算」—— 告警的前提是知道数字
    assert unknown["budget"]["exceeded"] is False
    assert unknown["budget"]["cost_known"] is False


# --------------------------------------------------------------------------- 攒批写


async def test_buffered_audit_store_batches_writes() -> None:
    """攒批的意义：把 N 次写合并成 1 次提交（SQLite 只允许一个写事务）。"""
    inner = MemoryAuditStore()
    calls: list[int] = []
    original = inner.append_many

    async def counting(entries: list[AuditEntry]) -> None:
        calls.append(len(entries))
        await original(entries)

    inner.append_many = counting  # type: ignore[method-assign]
    store = BufferedAuditStore(inner, max_pending=10)

    for index in range(5):
        await store.append(AuditEntry(audit_id=f"a{index}", kind="tool", thread_id="t1"))

    assert calls == []  # 还没到阈值，一条都没落盘
    assert pending_count(store) == 5

    await store.flush()

    assert calls == [5]
    assert pending_count(store) == 0
    assert len(await store.list_recent()) == 5


async def test_buffered_audit_store_dedupes_same_id() -> None:
    """同一次工具调用的两次写入合到一批时只应保留最终状态。"""
    inner = MemoryAuditStore()
    store = BufferedAuditStore(inner, max_pending=10)

    await store.append(AuditEntry(audit_id="a1", kind="tool", thread_id="t1", status="running"))
    await store.append(AuditEntry(audit_id="a1", kind="tool", thread_id="t1", status="ok"))
    await store.flush()

    rows = await store.list_recent()

    assert len(rows) == 1
    assert rows[0].status == "ok"


async def test_buffered_audit_store_flushes_before_read() -> None:
    """读之前先落盘：不能让「刚发生的事情查不到」。"""
    store = BufferedAuditStore(MemoryAuditStore(), max_pending=100)
    await store.append(AuditEntry(audit_id="a1", kind="tool", thread_id="t1"))

    assert [item.audit_id for item in await store.list_recent()] == ["a1"]
    assert await store.get("a1") is not None


async def test_buffered_audit_store_flushes_on_threshold() -> None:
    inner = MemoryAuditStore()
    store = BufferedAuditStore(inner, max_pending=3)

    for index in range(3):
        await store.append(AuditEntry(audit_id=f"a{index}", kind="tool", thread_id="t1"))

    assert pending_count(store) == 0  # 到阈值自动落盘
    assert len(inner._entries) == 3


async def test_buffered_usage_store_merges_same_bucket() -> None:
    """同一 (day, thread) 的多次运行在缓冲区里就合并，少几次读改写。"""
    inner = MemoryUsageStore()
    store = BufferedUsageStore(inner, max_pending=10)
    day = today_key()

    await store.add(UsageRecord(day=day, thread_id="t1", input_tokens=10, runs=1, cost=0.1))
    await store.add(UsageRecord(day=day, thread_id="t1", input_tokens=5, runs=1, cost=0.2))

    assert pending_count(store) == 1
    await store.flush()

    by_thread = await store.by_thread(days=2)

    assert by_thread[0].input_tokens == 15
    assert by_thread[0].runs == 2
    assert by_thread[0].cost == pytest.approx(0.3)


async def test_buffered_usage_store_keeps_unknown_cost_unknown() -> None:
    inner = MemoryUsageStore()
    store = BufferedUsageStore(inner, max_pending=10)
    day = today_key()

    await store.add(UsageRecord(day=day, thread_id="t1", input_tokens=10, cost=0.5))
    await store.add(UsageRecord(day=day, thread_id="t1", input_tokens=10, cost=None))
    await store.flush()

    assert (await store.by_thread(days=2))[0].cost is None


async def test_buffered_usage_total_reads_pending_without_flushing() -> None:
    """预算检查要「看到缓冲区但不落盘」；默认读则必须先落盘。

    这两个需求方向相反，因此分成 ``include_pending`` 显式区分 ——
    靠「调用方记得先调 by_thread」维持顺序是典型的静默陷阱。
    """
    inner = MemoryUsageStore()
    store = BufferedUsageStore(inner, max_pending=100)
    day = today_key()
    await store.add(UsageRecord(day=day, thread_id="t1", input_tokens=10, cost=0.1))

    # 预算检查：算上缓冲区，但不落盘
    assert await store.total(day=day, include_pending=True) == (10, 0, 0.1)
    assert pending_count(store) == 1

    # 普通读取：先落盘再看
    assert await store.total(day=day) == (10, 0, 0.1)
    assert pending_count(store) == 0


async def test_buffered_store_survives_inner_failure() -> None:
    """内层写失败只告警，不能把异常抛回运行链路。"""

    class Broken:
        async def append(self, entry: AuditEntry) -> None:
            raise RuntimeError("库挂了")

        async def list_recent(self, **kwargs: Any) -> list[AuditEntry]:
            return []

        async def get(self, audit_id: str) -> AuditEntry | None:
            return None

    store = BufferedAuditStore(Broken(), max_pending=1)  # type: ignore[arg-type]

    await store.append(AuditEntry(audit_id="a1", kind="tool", thread_id="t1"))

    assert pending_count(store) == 0  # 丢掉了，但没把异常抛出去


# --------------------------------------------------------------------------- 观察者协议


class _Boom(BaseObserver):
    name = "boom"

    async def on_event(self, context: RunContext, event: RunEvent) -> None:
        raise RuntimeError("观察者坏了")

    async def on_run_end(self, context: RunContext, summary: RunSummary) -> None:
        raise RuntimeError("观察者坏了")


async def test_observer_failure_never_breaks_the_run() -> None:
    """核心约束：审计/追踪坏掉时，聊天必须照常。"""
    calls: list[str] = []

    class Good(BaseObserver):
        name = "good"

        async def on_event(self, context: RunContext, event: RunEvent) -> None:
            calls.append(event.type)

    observers = [_Boom(), Good()]  # type: ignore[list-item]

    await notify_start(observers, ctx())
    await notify_event(observers, ctx(), RunEvent.run_start())
    await notify_end(observers, ctx(), RunSummary())

    # 坏的那个先执行，但好的那个照样收到事件
    assert calls == ["run_start"]


async def test_build_observability_defaults(settings: Settings) -> None:
    """默认（无库）时：审计与成本开启、追踪关闭。"""
    stack = build_observability(settings)

    assert stack.names == ["audit", "usage"]
    assert stack.describe()["audit_backend"] == "memory"
    assert stack.tracer is None


async def test_build_observability_with_database(database: Database, settings: Settings) -> None:
    stack = build_observability(settings, database=database)

    assert stack.describe()["audit_backend"] == "database"
    assert stack.describe()["usage_backend"] == "database"


async def test_build_observability_can_be_disabled(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(
        config_yaml="observability:\n  audit:\n    enabled: false\n  cost:\n    enabled: false\n"
    )

    stack = build_observability(settings)

    assert stack.names == []


async def test_tracing_needs_credentials(settings_factory) -> None:  # type: ignore[no-untyped-def]
    """打开追踪但没给 key → 安静降级，而不是崩掉或偷偷发出去。"""
    settings = settings_factory(
        config_yaml="observability:\n  tracing:\n    enabled: true\n", use_env=False
    )

    stack = build_observability(settings)

    assert stack.tracer is None
    assert "tracing" not in stack.names


# --------------------------------------------------------------------------- 追踪


class FakeSpan:
    """假的 Langfuse span：记录调用，不做任何 IO。"""

    def __init__(self, kind: str, name: str, recorder: list[dict[str, Any]], **kw: Any) -> None:
        self.record = {"kind": kind, "name": name, **kw}
        self.recorder = recorder
        self.recorder.append(self.record)

    def __enter__(self) -> FakeSpan:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def update(self, **kw: Any) -> None:
        self.record.setdefault("updates", []).append(kw)

    def update_trace(self, **kw: Any) -> None:
        self.record.setdefault("traces", []).append(kw)

    def start_as_current_span(self, **kw: Any) -> FakeSpan:
        return FakeSpan("span", kw.pop("name"), self.recorder, **kw)

    def start_as_current_generation(self, **kw: Any) -> FakeSpan:
        return FakeSpan("generation", kw.pop("name"), self.recorder, **kw)


class FakeLangfuse:
    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.flushed = False
        self.shut = False

    def start_as_current_span(self, **kw: Any) -> FakeSpan:
        return FakeSpan("span", kw.pop("name"), self.spans, **kw)

    def start_as_current_generation(self, **kw: Any) -> FakeSpan:
        return FakeSpan("generation", kw.pop("name"), self.spans, **kw)

    def create_event(self, **kw: Any) -> None:
        self.events.append(kw)

    def flush(self) -> None:
        self.flushed = True

    def shutdown(self) -> None:
        self.shut = True

    def auth_check(self) -> bool:
        return True


def sample_events() -> list[RunEvent]:
    return [
        RunEvent.run_start(thread_id="t1"),
        RunEvent.step_start(1, thread_id="t1"),
        RunEvent.text_delta("我查一下", step=1, thread_id="t1"),
        RunEvent.tool_call(step=1, name="web_search", call_id="c1", args={"query": "x"}),
        RunEvent.tool_result(step=1, name="web_search", call_id="c1", result="结果", ok=True),
        RunEvent.step_start(2, thread_id="t1"),
        RunEvent.tool_call(step=2, name="workspace_write", call_id="c2", args={"path": "a.md"}),
        RunEvent.approval(
            step=2, name="workspace_write", call_id="c2", approved=False, reason="用户拒绝"
        ),
        RunEvent.tool_result(
            step=2, name="workspace_write", call_id="c2", result="用户拒绝执行", ok=False
        ),
        RunEvent.run_end(
            "好了", step=2, usage={"input_tokens": 10, "output_tokens": 5}, thread_id="t1"
        ),
    ]


def test_tracer_emits_nested_spans() -> None:
    client = FakeLangfuse()
    LangfuseTracer(client).emit(ctx(), RunSummary(ok=True, steps=2, tool_calls=2), sample_events())

    root = client.spans[0]
    assert root["name"] == "t1" or root["name"].startswith("run:")  # type: ignore[union-attr]
    tool_spans = [item for item in client.spans if str(item["name"]).startswith("tool:")]
    assert {item["name"] for item in tool_spans} == {"tool:web_search", "tool:workspace_write"}


def test_tracer_marks_rejected_tool_as_warning() -> None:
    """被拒绝的调用在 trace 上要显眼：它是「模型想做、人没让做」。"""
    client = FakeLangfuse()
    LangfuseTracer(client).emit(ctx(), RunSummary(ok=True), sample_events())

    rejected = next(item for item in client.spans if item["name"] == "tool:workspace_write")

    assert rejected["level"] == "WARNING"
    assert rejected["metadata"]["approved"] is False


def test_tracer_attaches_usage_to_root() -> None:
    client = FakeLangfuse()
    LangfuseTracer(client).emit(
        ctx(),
        RunSummary(ok=True, usage={"input_tokens": 100, "output_tokens": 20}),
        sample_events(),
    )

    updates = client.spans[0]["updates"]

    assert updates[0]["usage_details"] == {"input": 100, "output": 20}


def test_tracer_emits_error_event_on_failure() -> None:
    client = FakeLangfuse()
    LangfuseTracer(client).emit(ctx(), RunSummary(ok=False, error="达到最大步数"), sample_events())

    assert client.events[0]["level"] == "ERROR"
    assert client.spans[0]["level"] == "ERROR"


def test_tracer_trims_long_payloads() -> None:
    """一万字的文件内容贴进 trace 只会让界面难读。"""
    client = FakeLangfuse()
    events = [
        RunEvent.tool_call(step=1, name="workspace_read", call_id="c1", args={"path": "a" * 5000}),
        RunEvent.tool_result(
            step=1, name="workspace_read", call_id="c1", result="y" * 5000, ok=True
        ),
    ]
    LangfuseTracer(client).emit(ctx(), RunSummary(ok=True), events)

    tool = next(item for item in client.spans if item["name"] == "tool:workspace_read")

    assert "截断" in str(tool["output"])
    assert len(str(tool["input"])) < 1500


async def test_tracing_observer_buffers_and_flushes() -> None:
    client = FakeLangfuse()
    observer = TracingObserver(LangfuseTracer(client))
    context = ctx()

    await observer.on_run_start(context)
    for event in sample_events():
        await observer.on_event(context, event)
    await observer.on_run_end(context, RunSummary(ok=True))

    assert client.spans  # 攒到运行结束才发
    await observer.aclose()
    assert client.shut is True


async def test_tracing_observer_ignores_events_without_start() -> None:
    """没有 run_start 也能跑（事件流被截断时不该抛异常）。"""
    observer = TracingObserver(LangfuseTracer(FakeLangfuse()))

    await observer.on_event(ctx(), RunEvent.run_start())
    await observer.on_run_end(ctx(), RunSummary())


async def test_tracing_observer_caps_buffer() -> None:
    from doudou.observability.tracing import MAX_BUFFERED_EVENTS

    client = FakeLangfuse()
    observer = TracingObserver(LangfuseTracer(client))
    context = ctx()

    await observer.on_run_start(context)
    for _ in range(MAX_BUFFERED_EVENTS + 10):
        await observer.on_event(context, RunEvent.text_delta("x", step=1))
    await observer.on_run_end(context, RunSummary(ok=True))

    assert observer._events == {}
