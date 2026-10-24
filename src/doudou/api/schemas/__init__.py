"""Request / response schema package."""

from __future__ import annotations

from doudou.api.schemas.approvals import (
    ApprovalDecisionRequest,
    ApprovalListResponse,
    ApprovalOut,
)
from doudou.api.schemas.chat import (
    ChatRequest,
    ChatResponse,
    MessageOutLite,
    StreamDone,
    StreamError,
    StreamStart,
    StreamTextDelta,
    StreamToolCall,
    StreamToolResult,
)
from doudou.api.schemas.common import (
    ChecksOut,
    ContextReport,
    ErrorBody,
    ErrorResponse,
    HealthResponse,
    McpServerOut,
    McpStatusResponse,
    MessageOut,
    SkillOut,
    SkillReloadResponse,
    SkillsResponse,
    ThreadDetail,
    ThreadListResponse,
    ThreadSummary,
    ToolInfoOut,
    ToolListResponse,
)

__all__ = [
    "ApprovalDecisionRequest",
    "ApprovalListResponse",
    "ApprovalOut",
    "ChatRequest",
    "ChatResponse",
    "ChecksOut",
    "ContextReport",
    "ErrorBody",
    "ErrorResponse",
    "HealthResponse",
    "McpServerOut",
    "McpStatusResponse",
    "MessageOut",
    "MessageOutLite",
    "SkillOut",
    "SkillReloadResponse",
    "SkillsResponse",
    "StreamDone",
    "StreamError",
    "StreamStart",
    "StreamTextDelta",
    "StreamToolCall",
    "StreamToolResult",
    "ThreadDetail",
    "ThreadListResponse",
    "ThreadSummary",
    "ToolInfoOut",
    "ToolListResponse",
]
