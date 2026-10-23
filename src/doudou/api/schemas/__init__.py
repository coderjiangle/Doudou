"""Request / response schema package."""

from __future__ import annotations

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
    ErrorBody,
    ErrorResponse,
    HealthResponse,
    McpServerOut,
    McpStatusResponse,
    MessageOut,
    ThreadDetail,
    ThreadListResponse,
    ThreadSummary,
    ToolInfoOut,
    ToolListResponse,
)

__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ChecksOut",
    "ErrorBody",
    "ErrorResponse",
    "HealthResponse",
    "McpServerOut",
    "McpStatusResponse",
    "MessageOut",
    "MessageOutLite",
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
