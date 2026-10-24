"""Human approval request / decision models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ApprovalOut(BaseModel):
    """一条确认请求（待决或已决）。"""

    approval_id: str
    thread_id: str | None = None
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict, description="模型给出的调用参数。")
    step: int = 0
    call_id: str = ""
    status: str = Field(description="pending | approved | rejected | expired")
    reason: str = ""
    created_at: datetime | None = None
    expires_at: datetime | None = None
    decided_at: datetime | None = None


class ApprovalListResponse(BaseModel):
    """`GET /approvals` 的响应。"""

    pending: list[ApprovalOut] = Field(
        default_factory=list, description="正在阻塞某条会话、等待决策的请求。"
    )
    recent: list[dict[str, Any]] = Field(
        default_factory=list,
        description="库里的最近记录（含已决策的），**进程重启后仍可查**。",
    )
    timeout_seconds: float = Field(description="超过这个时长未决策即按拒绝处理。")
    note: str = ""


class ApprovalDecisionRequest(BaseModel):
    """`POST /approvals/{id}` 的请求体。"""

    approved: bool = Field(description="true 同意执行，false 拒绝。")
    reason: str = Field(default="", description="可选说明，会记进审计记录。")


__all__ = ["ApprovalDecisionRequest", "ApprovalListResponse", "ApprovalOut"]
