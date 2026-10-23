"""Typed models for YAML config: agent, channels, mcp, storage.

这里的模型既是 ``config/*.yaml`` 的结构定义，也是默认值来源：
``config.yaml`` 缺失时，仅靠本文件的默认值也能启动。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FilesystemBackendName = Literal["state", "store", "filesystem", "composite"]
CheckpointerName = Literal["memory", "sqlite", "postgres"]
PermissionMode = Literal["allow", "deny"]
PermissionOperation = Literal["read", "write"]


class _Section(BaseModel):
    """所有配置段的基类：拒绝未知字段，避免拼写错误被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- runtime


class RuntimeConfig(_Section):
    env: Literal["local", "dev", "prod"] = "local"
    debug: bool = True
    log_level: str = "INFO"
    log_json: bool = False
    timezone: str = "Asia/Shanghai"


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: int = 8000
    base_path: str = "/api/v1"
    workers: int = 1
    cors_origins: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- model


class ModelConfig(_Section):
    """``primary`` 使用 ``provider:model`` 形式，例如 ``openai:gpt-5.5``。"""

    primary: str = "openai:gpt-5.5"
    fallbacks: list[str] = Field(default_factory=list)
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: float = 120.0
    max_retries: int = 2
    base_url: str | None = None


# --------------------------------------------------------------------------- agent


def _default_operations() -> list[PermissionOperation]:
    """默认同时允许读与写；具体放行与否由规则顺序决定。"""
    return ["read", "write"]


class PermissionRule(_Section):
    operations: list[PermissionOperation] = Field(default_factory=_default_operations)
    paths: list[str] = Field(default_factory=list)
    mode: PermissionMode = "allow"


class AgentConfig(_Section):
    name: str = "Doudou"
    system_prompt_file: str | None = "prompts/system.md"
    memory_files: list[str] = Field(default_factory=list)
    #: Skill 根目录；默认指向仓库根的 ``skills/``（相对 ``config/`` 解析）。
    skills: list[str] = Field(default_factory=lambda: ["../skills/"])
    subagents_file: str | None = "agents.yaml"
    #: 需要人工确认的工具：``{工具名: true}``
    interrupt_on: dict[str, bool] = Field(default_factory=dict)
    recursion_limit: int = 60
    #: ReAct 循环的单次运行最大步数（模型往返次数）
    max_steps: int = 8
    #: 单次模型返回中允许并发执行的工具调用数上限
    max_tool_calls_per_step: int = 8
    permissions: list[PermissionRule] = Field(default_factory=list)


# --------------------------------------------------------------------------- filesystem


class FilesystemRouteConfig(_Section):
    prefix: str
    backend: FilesystemBackendName


class FilesystemConfig(_Section):
    backend: FilesystemBackendName = "state"
    root_dir: str = "./data/workspace"
    virtual_mode: bool = True
    routes: list[FilesystemRouteConfig] = Field(default_factory=list)


# --------------------------------------------------------------------------- context


class ContextConfig(_Section):
    todo_list: bool = True
    summarization: bool = True
    summarize_after_messages: int = 40
    keep_recent_messages: int = 12
    offload_large_results: bool = True
    offload_threshold_bytes: int = 20000


# --------------------------------------------------------------------------- storage


class StorageConfig(_Section):
    database_url: str = "sqlite+aiosqlite:///./data/doudou.db"
    #: 会话历史的存储位置：``database`` 落库（重启可续），``memory`` 只在进程内。
    history: Literal["memory", "database"] = "database"
    checkpointer: CheckpointerName = "sqlite"
    store: CheckpointerName = "sqlite"
    echo_sql: bool = False


# --------------------------------------------------------------------------- mcp


class McpConfig(_Section):
    enabled: bool = True
    config_file: str = "mcp.servers.yaml"
    fail_fast: bool = False
    tool_name_prefix: bool = True
    call_timeout: float = 60.0


# --------------------------------------------------------------------------- channels


class CliChannelConfig(_Section):
    enabled: bool = True


class FeishuChannelConfig(_Section):
    enabled: bool = False
    mode: Literal["callback", "websocket"] = "callback"
    base_url: str = "https://open.feishu.cn"
    bot_name: str = "Doudou"
    event_path: str = "/webhooks/feishu/events"
    reply_mode: Literal["card", "text"] = "card"
    stream_reply: bool = True
    card_update_interval_ms: int = 800
    allowed_chats: list[str] = Field(default_factory=list)
    allowed_users: list[str] = Field(default_factory=list)
    group_require_at: bool = True
    max_reply_chars: int = 4000
    dedup_ttl_seconds: int = 600


class WebhookChannelConfig(_Section):
    enabled: bool = False
    #: 校验入站请求的 header 名（不是密钥本身）。
    token_header: str = "X-Doudou-Token"  # noqa: S105


class ChannelsConfig(_Section):
    cli: CliChannelConfig = Field(default_factory=CliChannelConfig)
    feishu: FeishuChannelConfig = Field(default_factory=FeishuChannelConfig)
    webhook: WebhookChannelConfig = Field(default_factory=WebhookChannelConfig)


# --------------------------------------------------------------------------- observability


class LangSmithConfig(_Section):
    enabled: bool = False
    project: str = "doudou"


class OtelConfig(_Section):
    enabled: bool = False
    endpoint: str | None = None


class MetricsConfig(_Section):
    enabled: bool = True


class ObservabilityConfig(_Section):
    langsmith: LangSmithConfig = Field(default_factory=LangSmithConfig)
    otel: OtelConfig = Field(default_factory=OtelConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)


# --------------------------------------------------------------------------- root


class AppConfig(_Section):
    """``config/config.yaml`` 的根结构。"""

    version: int = 1
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    filesystem: FilesystemConfig = Field(default_factory=FilesystemConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)


__all__ = [
    "AgentConfig",
    "AppConfig",
    "ChannelsConfig",
    "CliChannelConfig",
    "ContextConfig",
    "FeishuChannelConfig",
    "FilesystemConfig",
    "FilesystemRouteConfig",
    "LangSmithConfig",
    "McpConfig",
    "MetricsConfig",
    "ModelConfig",
    "ObservabilityConfig",
    "OtelConfig",
    "PermissionRule",
    "RuntimeConfig",
    "ServerConfig",
    "StorageConfig",
    "WebhookChannelConfig",
]
