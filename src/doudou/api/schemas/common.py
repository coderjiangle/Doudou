"""Shared envelopes, pagination and error models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    """错误体。"""

    code: str = Field(description="机器可读的错误码，如 invalid_request / unauthorized。")
    message: str = Field(description="给人看的说明。")
    details: Any | None = Field(
        default=None, description="可选的补充信息（校验失败时是字段列表）。"
    )


class ErrorResponse(BaseModel):
    """所有非 2xx 响应的统一结构。"""

    error: ErrorBody
    request_id: str | None = None


class ChecksOut(BaseModel):
    """健康检查的依赖项状态。"""

    model_config = ConfigDict(extra="allow")


class HealthResponse(BaseModel):
    """`GET /health` 的响应。"""

    status: str = Field(description="ok | degraded")
    version: str
    env: str
    engine: str = Field(description="当前生效的 Agent 引擎。")
    model: str
    uptime_seconds: float
    tools: list[str] = Field(default_factory=list)
    checks: dict[str, str] = Field(default_factory=dict, description="各依赖项的 ok / 说明。")
    metrics: dict[str, Any] = Field(default_factory=dict, description="运行计数与耗时摘要。")


class ToolInfoOut(BaseModel):
    """单个工具的展示信息。"""

    name: str
    description: str
    group: str
    source: str
    args: dict[str, str] = Field(default_factory=dict)
    requires_approval: bool = False


class ToolListResponse(BaseModel):
    tools: list[ToolInfoOut]


class ThreadSummary(BaseModel):
    """线程概要。"""

    thread_id: str
    channel: str | None = None
    conversation_id: str | None = None
    message_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ThreadListResponse(BaseModel):
    threads: list[ThreadSummary]


class MessageOut(BaseModel):
    """线程内的一条消息。"""

    role: str = Field(description="human | ai | tool | system")
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    status: str | None = None


class ThreadDetail(BaseModel):
    """`GET /threads/{id}` 的响应。"""

    thread: ThreadSummary
    messages: list[MessageOut]


__all__ = [
    "ChecksOut",
    "ErrorBody",
    "ErrorResponse",
    "HealthResponse",
    "MessageOut",
    "ThreadDetail",
    "ThreadListResponse",
    "ThreadSummary",
    "ToolInfoOut",
    "ToolListResponse",
]
