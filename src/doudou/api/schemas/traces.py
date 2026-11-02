"""Run-trace and statistics response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TraceStepOut(BaseModel):
    """轨迹里的一步：这一步的文本 + 它调用的工具。"""

    step: int = 0
    text: str = ""
    tools: list[dict[str, Any]] = Field(default_factory=list)


class TraceSummaryOut(BaseModel):
    """一次运行的摘要（列表用，不含步骤明细）。"""

    run_id: str
    thread_id: str
    channel: str = ""
    conversation_id: str = ""
    user_id: str | None = None
    model: str = ""
    ok: bool = True
    error: str | None = None
    steps: int = 0
    tool_calls: int = 0
    approvals: int = 0
    rejections: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    elapsed: float = 0.0
    input_preview: str = ""
    output_preview: str = ""
    started_at: datetime | None = None


class TraceDetailOut(TraceSummaryOut):
    """单次运行的完整轨迹（含步骤明细）。"""

    steps_detail: list[TraceStepOut] = Field(default_factory=list)


class TraceListResponse(BaseModel):
    count: int
    total: int = Field(default=0, description="符合条件的总条数（用于页码/总页数）。")
    offset: int = Field(default=0, description="本次返回的起始偏移。")
    has_more: bool = False
    backend: str = Field(description="database | memory | disabled")
    traces: list[TraceSummaryOut] = Field(default_factory=list)
    note: str = ""


class StatsOverviewOut(BaseModel):
    runs: int = 0
    ok_runs: int = 0
    failed_runs: int = 0
    success_rate: float | None = None
    avg_elapsed: float = 0.0
    p95_elapsed: float = 0.0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class StatsDayOut(BaseModel):
    day: str
    runs: int = 0
    ok: int = 0
    failed: int = 0
    avg_elapsed: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost: float | None = Field(
        default=None, description="null 表示当天有模型没配价格（未知 ≠ 0）。"
    )


class StatsChannelOut(BaseModel):
    channel: str
    runs: int = 0
    ok: int = 0
    failed: int = 0


class StatsToolOut(BaseModel):
    name: str
    calls: int = 0
    ok: int = 0
    failed: int = 0
    rejected: int = 0
    avg_elapsed: float | None = None


class StatsApprovalsOut(BaseModel):
    required: int = 0
    approvals: int = 0
    rejections: int = 0
    auto: int = 0
    approval_rate: float | None = None


class StatsResponse(BaseModel):
    """`GET /stats` 的响应：一次给够统计页要的所有数据。"""

    days: int
    #: 统计范围：``None`` = 全部会话；给了值 = 只统计该会话。
    thread_id: str | None = None
    overview: StatsOverviewOut
    by_day: list[StatsDayOut] = Field(default_factory=list)
    by_channel: list[StatsChannelOut] = Field(default_factory=list)
    tools: list[StatsToolOut] = Field(default_factory=list)
    approvals: StatsApprovalsOut
    note: str = ""


__all__ = [
    "StatsApprovalsOut",
    "StatsChannelOut",
    "StatsDayOut",
    "StatsOverviewOut",
    "StatsResponse",
    "StatsToolOut",
    "TraceDetailOut",
    "TraceListResponse",
    "TraceStepOut",
    "TraceSummaryOut",
]
