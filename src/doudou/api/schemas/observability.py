"""Audit and usage response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AuditEntryOut(BaseModel):
    """一条审计记录。

    ``args`` 是**打码 + 截断后**的参数摘要；``args_truncated`` 说明被截掉多少字，
    所以看到它的人知道这里有缺口，不会把摘要当全文。
    """

    audit_id: str
    kind: str = Field(description="run | tool | error")
    thread_id: str
    channel: str = ""
    conversation_id: str = ""
    user_id: str | None = None
    tool_name: str = ""
    tool_call_id: str = ""
    step: int = 0
    args: dict[str, Any] = Field(default_factory=dict)
    args_truncated: int = 0
    result_preview: str = ""
    result_chars: int = 0
    status: str = Field(description="running | ok | failed | rejected")
    approved: bool | None = Field(
        default=None, description="是否经人工确认；null 表示该工具不需要确认。"
    )
    elapsed: float | None = None
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AuditListResponse(BaseModel):
    count: int
    backend: str = Field(description="database | memory")
    entries: list[AuditEntryOut] = Field(default_factory=list)
    note: str = ""


class UsageBucketOut(BaseModel):
    """一段时间 / 一条会话的用量。"""

    day: str = ""
    thread_id: str = ""
    channel: str = ""
    model: str = ""
    runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_calls: int = 0
    approvals: int = 0
    rejections: int = 0
    cost: float | None = Field(default=None, description="null 表示该模型没配价格（未知 ≠ 0）。")
    last_at: datetime | None = None


class UsageTotalOut(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost: float | None = None


class UsageBudgetOut(BaseModel):
    daily: float = 0.0
    exceeded: bool = False
    cost_known: bool = True


class UsageResponse(BaseModel):
    """`GET /usage` 的响应。"""

    model: str = ""
    price_unit_tokens: int = 1_000_000
    prices_known: list[str] = Field(default_factory=list)
    unknown_models: list[str] = Field(default_factory=list)
    total: UsageTotalOut
    today: UsageTotalOut
    by_day: list[UsageBucketOut] = Field(default_factory=list)
    by_thread: list[UsageBucketOut] = Field(default_factory=list)
    budget: UsageBudgetOut
    note: str = ""


__all__ = [
    "AuditEntryOut",
    "AuditListResponse",
    "UsageBucketOut",
    "UsageBudgetOut",
    "UsageResponse",
    "UsageTotalOut",
]
