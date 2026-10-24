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
    channels: list[str] = Field(default_factory=list, description="已启动的常驻通道。")
    mcp: dict[str, Any] = Field(default_factory=dict, description="MCP server 与工具概况。")
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
    side_effects: bool = False
    timeout: float | None = None


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


class ContextReport(BaseModel):
    """`GET /threads/{id}/context` 的响应。

    回答排查时最想问的问题：这条会话占多少上下文、有没有被压缩过、
    原始历史还在不在。
    """

    thread_id: str
    stored_messages: int = Field(description="库里保留的原始消息数（压缩不会删除它们）。")
    stored_tokens: int = Field(description="原始历史的估算 token 数。")
    budget: int = Field(description="送进模型的历史 token 预算。")
    last_run: dict[str, Any] | None = Field(default=None, description="上一次运行的占用统计。")
    summary: dict[str, Any] = Field(default_factory=dict, description="摘要是否存在与长度。")
    note: str = ""


class McpServerOut(BaseModel):
    """单个 MCP server 的状态。"""

    name: str
    transport: str
    enabled: bool = True
    read_only: bool = False
    description: str = ""
    target: str = Field(default="", description="stdio 的命令行或 http 的地址。")
    include_tools: list[str] = Field(default_factory=list)
    exclude_tools: list[str] = Field(default_factory=list)
    ok: bool = False
    tools: list[str] = Field(default_factory=list, description="实际暴露的工具名（含前缀）。")
    filtered_out: list[str] = Field(
        default_factory=list, description="被 include/exclude 过滤掉的工具名。"
    )
    error: str | None = None
    elapsed: float | None = None


class McpStatusResponse(BaseModel):
    """`GET /mcp` 的响应。"""

    enabled: bool
    started: bool
    servers: list[McpServerOut] = Field(default_factory=list)
    note: str | None = None


class SkillOut(BaseModel):
    """单个 Skill 的索引条目（正文按需返回）。"""

    name: str
    description: str
    path: str
    directory: str
    body_chars: int = 0
    body: str | None = Field(default=None, description="仅当 include_body=true 时返回。")
    files: list[str] = Field(default_factory=list, description="附属文件相对路径。")
    extra: dict[str, Any] = Field(default_factory=dict, description="frontmatter 里的其它字段。")


class SkillsResponse(BaseModel):
    """`GET /skills` 的响应。"""

    count: int
    roots: list[str] = Field(default_factory=list, description="扫描的根目录。")
    errors: list[str] = Field(default_factory=list, description="被跳过的 Skill 及原因。")
    skills: list[SkillOut] = Field(default_factory=list)


class SkillReloadResponse(BaseModel):
    """`POST /skills/reload` 的响应。"""

    count: int
    errors: list[str] = Field(default_factory=list)
    system_prompt_chars: int = 0
    note: str | None = None


__all__ = [
    "ChecksOut",
    "ContextReport",
    "ErrorBody",
    "ErrorResponse",
    "HealthResponse",
    "McpServerOut",
    "McpStatusResponse",
    "MessageOut",
    "SkillOut",
    "SkillReloadResponse",
    "SkillsResponse",
    "ThreadDetail",
    "ThreadListResponse",
    "ThreadSummary",
    "ToolInfoOut",
    "ToolListResponse",
]
