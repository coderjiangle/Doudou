"""Scheduled-task response models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ScheduledTaskOut(BaseModel):
    """一条定时任务。"""

    task_id: str
    name: str
    prompt: str
    kind: str = Field(description="at | every | cron")
    expr: str
    timezone: str | None = None
    channel: str = ""
    conversation_id: str | None = Field(
        default=None, description="投递目标会话；null 表示触发时解析（CLI/Web 任务）。"
    )
    created_by: str | None = None
    enabled: bool = True
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str | None = Field(
        default=None, description="ok | failed | skipped（没有可推送目标时跳过）。"
    )
    last_error: str | None = None
    run_count: int = 0


class ScheduledListResponse(BaseModel):
    count: int
    backend: str = Field(description="database | memory | disabled")
    tasks: list[ScheduledTaskOut] = Field(default_factory=list)
    note: str = ""


__all__ = ["ScheduledListResponse", "ScheduledTaskOut"]
