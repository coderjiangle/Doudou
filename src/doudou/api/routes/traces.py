"""Run-trace and statistics endpoints.

轨迹是**只读**的排查/回放面：一次运行一条记录，能点进去看「每一步说了什么、
调了哪些工具、结果如何、有没有经过人」。统计页（``GET /stats``）把三份数据
拼在一起 —— 运行（轨迹）、工具与确认（审计）、token 与费用（用量）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query

from doudou.api.deps import StateDep
from doudou.api.errors import ApiError
from doudou.api.schemas.traces import (
    StatsResponse,
    TraceDetailOut,
    TraceListResponse,
    TraceSummaryOut,
)
from doudou.api.stats import RunSample, ToolSample, build_stats
from doudou.observability.audit import AuditEntry
from doudou.observability.logging import get_logger
from doudou.observability.traces import TraceRecord

logger = get_logger(__name__)

router = APIRouter(tags=["observability"])

#: 统计窗口内最多取多少行（个人助手量级下足够；超出的部分会被截断）。
STATS_ROW_LIMIT = 5000


@router.get("/traces", response_model=TraceListResponse, summary="运行轨迹列表")
async def list_traces(
    state: StateDep,
    thread_id: str | None = Query(default=None, description="只看某条会话。"),
    channel: str | None = Query(default=None, description="只看某个渠道（cli / api / feishu）。"),
    days: int = Query(default=7, ge=1, le=90, description="统计最近多少天。"),
    only_failed: bool = Query(default=False, description="只看失败的运行。"),
    limit: int = Query(default=50, ge=1, le=500, description="单页条数。"),
    cursor: str | None = Query(
        default=None, description="取更早一页：传上一页返回的 next_cursor。"
    ),
) -> TraceListResponse:
    """按时间倒序列出运行轨迹（不含步骤明细 —— 点进单条才展开）。

    用**游标（keyset）分页**而不是 ``offset``：按 ``(started_at, run_id)`` 取更早的记录，
    翻页期间新产生的运行不会造成漏项或重复。``next_cursor`` 为 ``null`` 表示到底了。
    """
    store = state.observability.trace_store
    if store is None:
        return TraceListResponse(
            count=0,
            backend="disabled",
            traces=[],
            next_cursor=None,
            note="轨迹未启用（observability.traces.enabled 为 false）。",
        )
    before = _decode_cursor(cursor) if cursor else None
    since = _since(days)
    # 多取一条用来判断「还有没有下一页」，返回时再裁掉。
    records = await store.list_recent(
        limit=limit + 1,
        thread_id=thread_id,
        channel=channel,
        since=since,
        only_failed=only_failed,
        before=before,
    )
    has_more = len(records) > limit
    page = records[:limit]
    next_cursor = _encode_cursor(page[-1]) if has_more and page else None
    return TraceListResponse(
        count=len(page),
        backend=state.observability.trace_backend or "memory",
        traces=[TraceSummaryOut.model_validate(item.as_summary()) for item in page],
        next_cursor=next_cursor,
        note="一次运行一条记录；点进单条可看逐步的推理文本与工具调用（/traces/{run_id}）。",
    )


@router.get("/traces/{run_id}", response_model=TraceDetailOut, summary="单次运行的完整轨迹")
async def get_trace(run_id: str, state: StateDep) -> TraceDetailOut:
    store = state.observability.trace_store
    if store is None:
        raise ApiError(
            503, "traces_disabled", "轨迹未启用（observability.traces.enabled 为 false）"
        )
    record = await store.get(run_id)
    if record is None:
        raise ApiError(404, "trace_not_found", f"没有轨迹 {run_id}")
    return TraceDetailOut.model_validate(record.as_dict())


@router.get("/stats", response_model=StatsResponse, summary="运行统计（自建统计页数据源）")
async def get_stats(
    state: StateDep,
    days: int = Query(default=7, ge=1, le=90, description="统计最近多少天。"),
    thread_id: str | None = Query(default=None, description="只看某条会话；留空 = 全部会话。"),
) -> StatsResponse:
    """运行量 / 成功率 / 耗时 / 工具分布 / 确认比例 / token 与费用趋势。

    数据源各管一段：运行来自轨迹表，工具与确认来自审计表，token/费用来自用量表。
    任一未启用时对应部分为空，但接口不报错 —— 统计页应能在任意配置下打开。

    默认是**全部会话**的汇总；给了 ``thread_id`` 就只统计那一条会话
    （三份数据都按它过滤）。
    """
    since = _since(days)
    observability = state.observability

    runs: list[RunSample] = []
    store = observability.trace_store
    if store is not None:
        records = await store.list_recent(limit=STATS_ROW_LIMIT, since=since, thread_id=thread_id)
        runs = [
            RunSample(
                started_at=item.started_at,
                ok=item.ok,
                elapsed=item.elapsed,
                tool_calls=item.tool_calls,
                approvals=item.approvals,
                rejections=item.rejections,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
                channel=item.channel,
                thread_id=item.thread_id,
            )
            for item in records
        ]

    tools: list[ToolSample] = []
    audit_store = observability.audit_store
    if audit_store is not None:
        entries = await audit_store.list_recent(limit=STATS_ROW_LIMIT, thread_id=thread_id)
        tools = [_tool_sample(entry) for entry in entries if _in_window(entry, since)]

    usage_by_day: dict[str, tuple[int, int, float | None]] = {}
    usage_store = observability.usage_store
    if usage_store is not None:
        for record in await usage_store.by_day(days=days, thread_id=thread_id):
            usage_by_day[record.day] = (record.input_tokens, record.output_tokens, record.cost)

    report = build_stats(runs=runs, tools=tools, usage_by_day=usage_by_day, days=days)
    return StatsResponse.model_validate(
        {
            **report,
            "thread_id": thread_id,
            "note": (
                "运行数/成功率/耗时来自 run_traces；工具分布与确认比例来自 audit_events；"
                "token/费用来自 usage_daily。费用按 observability.cost.prices 估算，"
                "cost 为 null 表示未配价格（未知 ≠ 0）。"
            ),
        }
    )


def _tool_sample(entry: AuditEntry) -> ToolSample:
    return ToolSample(
        tool_name=entry.tool_name,
        status=entry.status,
        approved=entry.approved,
        elapsed=entry.elapsed,
        created_at=entry.created_at,
    )


def _in_window(entry: AuditEntry, since: datetime) -> bool:
    if entry.kind != "tool":
        return False
    created = entry.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created >= since


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=max(1, days))


def _encode_cursor(record: TraceRecord) -> str:
    """游标 = 上一页最后一条的 ``(started_at, run_id)``，可读、可调试。"""
    return f"{record.started_at.isoformat()}|{record.run_id}"


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    timestamp, sep, run_id = cursor.partition("|")
    if not sep or not run_id:
        raise ApiError(400, "bad_cursor", "游标格式不正确，请用上一页返回的 next_cursor。")
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ApiError(400, "bad_cursor", "游标格式不正确，请用上一页返回的 next_cursor。") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment, run_id


__all__ = ["router"]
