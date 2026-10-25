"""Pydantic Settings: env vars and .env loading.

两类来源分开建模，避免前缀规则互相干扰：

* :class:`EnvSettings` —— 带 ``DOUDOU_`` 前缀，嵌套字段用 ``__`` 分隔，
  例如 ``DOUDOU_SERVER__PORT=8080``、``DOUDOU_MODEL__PRIMARY=openai:gpt-5.5``。
  结构与 :class:`~doudou.config.schema.AppConfig` 一一对应，可直接深合并。
* :class:`Secrets` —— **无前缀**，沿用各家官方环境变量名
  （``OPENAI_API_KEY``、``FEISHU_APP_SECRET`` ...），只用于凭据。
"""

from __future__ import annotations

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = ".env"


class _EnvSection(BaseModel):
    """环境变量覆盖段的基类。

    字段全部可选：未设置的项不会出现在 ``exclude_none`` 的导出结果里，
    因此不会覆盖 YAML 中的值。
    """

    model_config = {"extra": "ignore"}


class EnvRuntime(_EnvSection):
    env: str | None = None
    debug: bool | None = None
    log_level: str | None = None
    log_json: bool | None = None
    timezone: str | None = None


class EnvServer(_EnvSection):
    host: str | None = None
    port: int | None = None
    base_path: str | None = None
    workers: int | None = None
    cors_origins: list[str] | None = None


class EnvModel(_EnvSection):
    primary: str | None = None
    fallbacks: list[str] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: float | None = None
    max_retries: int | None = None
    base_url: str | None = None


class EnvAgent(_EnvSection):
    name: str | None = None
    system_prompt_file: str | None = None
    skills: list[str] | None = None
    memory_files: list[str] | None = None
    max_steps: int | None = None
    recursion_limit: int | None = None
    require_approval_for_side_effects: bool | None = None
    approval_timeout_seconds: float | None = None
    skill_index_limit: int | None = None


class EnvHttpTool(_EnvSection):
    allowed_domains: list[str] | None = None
    timeout: float | None = None
    max_response_chars: int | None = None


class EnvWebSearchTool(_EnvSection):
    provider: str | None = None
    max_results: int | None = None
    search_depth: str | None = None


class EnvFilesTool(_EnvSection):
    max_read_chars: int | None = None


class EnvTools(_EnvSection):
    enabled_groups: list[str] | None = None
    disabled: list[str] | None = None
    http: EnvHttpTool | None = None
    web_search: EnvWebSearchTool | None = None
    files: EnvFilesTool | None = None


class EnvFilesystem(_EnvSection):
    backend: str | None = None
    root_dir: str | None = None
    virtual_mode: bool | None = None


class EnvContext(_EnvSection):
    summarization: bool | None = None
    todo_list: bool | None = None
    max_input_tokens: int | None = None
    summarize_threshold_ratio: float | None = None
    keep_recent_messages: int | None = None
    offload_large_results: bool | None = None
    offload_threshold_bytes: int | None = None
    offload_dir: str | None = None
    max_memory_chars: int | None = None


class EnvStorage(_EnvSection):
    database_url: str | None = None
    history: str | None = None
    max_messages_per_thread: int | None = None
    checkpointer: str | None = None
    store: str | None = None
    echo_sql: bool | None = None


class EnvMcp(_EnvSection):
    enabled: bool | None = None
    config_file: str | None = None
    fail_fast: bool | None = None


class EnvCliChannel(_EnvSection):
    enabled: bool | None = None


class EnvFeishuChannel(_EnvSection):
    enabled: bool | None = None
    mode: str | None = None
    base_url: str | None = None
    bot_name: str | None = None
    bot_open_id: str | None = None
    stream_reply: bool | None = None
    isolate_by_thread: bool | None = None
    allowed_chats: list[str] | None = None
    allowed_users: list[str] | None = None


class EnvWebhookChannel(_EnvSection):
    enabled: bool | None = None


class EnvChannels(_EnvSection):
    cli: EnvCliChannel | None = None
    feishu: EnvFeishuChannel | None = None
    webhook: EnvWebhookChannel | None = None


class EnvAudit(_EnvSection):
    enabled: bool | None = None
    max_arg_chars: int | None = None
    max_result_chars: int | None = None


class EnvCost(_EnvSection):
    enabled: bool | None = None
    daily_budget: float | None = None


class EnvTracing(_EnvSection):
    enabled: bool | None = None
    host: str | None = None
    environment: str | None = None
    release: str | None = None
    sample_rate: float | None = None


class EnvObservability(_EnvSection):
    audit: EnvAudit | None = None
    cost: EnvCost | None = None
    tracing: EnvTracing | None = None


class EnvSettings(BaseSettings):
    """``DOUDOU_*`` 环境变量。优先级高于 YAML。"""

    model_config = SettingsConfigDict(
        env_prefix="DOUDOU_",
        env_nested_delimiter="__",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    #: 配置文件目录，默认 ``./config``
    config_dir: str | None = None
    #: 显式指定主配置文件（相对 ``config_dir`` 或绝对路径）
    config_file: str | None = None
    #: 覆盖 ``config.local.yaml`` 的名字，测试用
    local_config_file: str | None = None
    #: HTTP API 访问令牌（非空即开启鉴权）
    api_token: str | None = None

    runtime: EnvRuntime | None = None
    server: EnvServer | None = None
    model: EnvModel | None = None
    agent: EnvAgent | None = None
    tools: EnvTools | None = None
    filesystem: EnvFilesystem | None = None
    context: EnvContext | None = None
    storage: EnvStorage | None = None
    mcp: EnvMcp | None = None
    channels: EnvChannels | None = None
    observability: EnvObservability | None = None


class Secrets(BaseSettings):
    """无前缀的凭据，沿用各家官方环境变量名。"""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    openai_api_key: str | None = None
    openai_base_url: str | None = None
    anthropic_api_key: str | None = None
    deepseek_api_key: str | None = None
    tavily_api_key: str | None = None

    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_verification_token: str | None = None
    feishu_encrypt_key: str | None = None
    feishu_base_url: str | None = None

    langsmith_api_key: str | None = None
    #: Langfuse 凭据（``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``）。
    #: 两者齐备且 ``observability.tracing.enabled`` 为真时才会发 trace。
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        """导出为字典，便于日志与 ``config show`` 脱敏展示。"""
        return self.model_dump()


__all__ = ["EnvSettings", "Secrets"]
