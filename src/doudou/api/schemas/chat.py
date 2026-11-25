"""Chat request, chunk and response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from doudou.agent.messages import MAX_IMAGES, ImageInput

#: SSE 事件名。与 ``RunEvent`` 及确认请求一一对应，客户端只需认这一组名字。
StreamEventName = Literal[
    "start",
    "delta",
    "tool_call",
    "approval_required",
    "tool_result",
    "done",
    "error",
]


class ChatRequest(BaseModel):
    """一次对话请求。"""

    message: str = Field(default="", description="用户输入；发送图片时可为空。")
    images: list[ImageInput] = Field(
        default_factory=list,
        max_length=MAX_IMAGES,
        description="图片，最多 4 张，每张不超过 5 MiB。",
    )
    thread_id: str | None = Field(
        default=None,
        description="指定会话；留空时由服务端按 conversation_id 分配（并持久绑定）。",
    )
    conversation_id: str | None = Field(
        default=None,
        description="渠道会话标识；用于在不显式传 thread_id 时复用同一个会话。",
    )
    channel: str = Field(default="api", description="来源渠道标记，仅用于记录与日志。")
    new_thread: bool = Field(default=False, description="为 True 时强制开启新会话。")

    @model_validator(mode="after")
    def require_content(self) -> ChatRequest:
        if not self.message.strip() and not self.images:
            raise ValueError("请输入文字或发送图片。")
        return self


class MessageOutLite(BaseModel):
    """响应里回带的工具调用摘要。"""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    elapsed: float | None = None


class ChatResponse(BaseModel):
    """非流式对话响应。"""

    thread_id: str
    text: str
    ok: bool = True
    steps: int = 0
    error: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    tool_calls: list[MessageOutLite] = Field(default_factory=list)


# --------------------------------------------------------------------------- SSE 载荷


class StreamStart(BaseModel):
    """``event: start`` —— 会话已就绪，客户端可据此建立 UI。"""

    thread_id: str
    channel: str


class StreamTextDelta(BaseModel):
    """``event: delta`` —— 增量文本。"""

    text: str


class StreamToolCall(BaseModel):
    """``event: tool_call`` —— 即将执行某个工具。"""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    call_id: str
    step: int


class StreamApprovalRequired(BaseModel):
    """``event: approval_required`` —— 等待人工确认。"""

    approval_id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    step: int = 0
    call_id: str = ""
    thread_id: str | None = None


class StreamToolResult(BaseModel):
    """``event: tool_result`` —— 工具执行结束。

    ``result`` 超过服务端上限时会被截断，并置 ``truncated``。
    """

    name: str
    call_id: str
    ok: bool = True
    result: str = ""
    truncated: bool = False
    elapsed: float | None = None


class StreamDone(BaseModel):
    """``event: done`` —— 本次运行的最终答复。"""

    thread_id: str
    text: str
    steps: int = 0
    usage: dict[str, int] = Field(default_factory=dict)


class StreamError(BaseModel):
    """``event: error`` —— 运行以错误终止。"""

    thread_id: str | None = None
    message: str


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "MessageOutLite",
    "StreamApprovalRequired",
    "StreamDone",
    "StreamError",
    "StreamEventName",
    "StreamStart",
    "StreamTextDelta",
    "StreamToolCall",
    "StreamToolResult",
]
