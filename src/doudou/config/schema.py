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


# --------------------------------------------------------------------------- tools


class HttpToolConfig(_Section):
    """``http_request`` 的出站策略。

    **默认拒绝一切**：``allowed_domains`` 为空时该工具根本不会注册，
    因为一个永远失败的工具有害无益（占提示词、诱导模型乱试）。
    """

    #: 允许访问的域名。``example.com`` 同时匹配其子域；``*.example.com`` 只匹配子域。
    allowed_domains: list[str] = Field(default_factory=list)
    timeout: float = 20.0
    max_response_chars: int = 20000
    max_redirects: int = 2
    user_agent: str = "Doudou/0.1 (+personal-assistant)"


class WebSearchToolConfig(_Section):
    provider: Literal["tavily"] = "tavily"
    max_results: int = 5
    timeout: float = 20.0
    #: ``advanced`` 更准但更慢、更贵。
    search_depth: Literal["basic", "advanced"] = "basic"
    #: 是否附带 Tavily 生成的摘要答案。
    include_answer: bool = True


class NotesToolConfig(_Section):
    #: 笔记目录（相对当前工作目录解析，不存在时自动创建）。
    directory: str = "./data/notes"
    max_list: int = 50
    max_read_chars: int = 20000


class FilesToolConfig(_Section):
    """文件工具的限制。

    读取根目录固定为 ``filesystem.root_dir``（即工作区），不单独配置 ——
    避免出现「两个 root」的困惑；要与权限规则对齐时只需要改一处。
    """

    max_read_chars: int = 50000
    max_list: int = 200
    #: 单次写入（或替换后）的字符上限。
    max_write_chars: int = 200000


class ShellToolConfig(_Section):
    """``shell_exec`` 的配置。

    **默认禁用**：这是本仓库唯一没有任何约束机制的出口 ——
    路径权限规则对它无效（它不经过我们的路径解析），只有人工确认拦一道。
    因此默认不开，开了就要自己承担风险。
    """

    enabled: bool = False
    #: 工作目录，默认取 ``filesystem.root_dir``。
    cwd: str | None = None
    timeout: float = 60.0
    #: 输出上限（stdout + stderr 合计）。
    max_output_chars: int = 20000
    #: 环境变量白名单。为空表示只透传 PATH 等最小集合，
    #: 避免把 API Key 之类泄漏给子进程。
    inherit_env: list[str] = Field(
        default_factory=lambda: ["PATH", "HOME", "LANG", "LC_ALL", "TZ", "TEMP", "TMP"]
    )


class ToolsConfig(_Section):
    #: 只启用这些分组；空列表表示全部启用。
    enabled_groups: list[str] = Field(default_factory=list)
    #: 逐个禁用的工具名（优先于 enabled_groups）。
    disabled: list[str] = Field(default_factory=list)
    http: HttpToolConfig = Field(default_factory=HttpToolConfig)
    web_search: WebSearchToolConfig = Field(default_factory=WebSearchToolConfig)
    notes: NotesToolConfig = Field(default_factory=NotesToolConfig)
    files: FilesToolConfig = Field(default_factory=FilesToolConfig)
    shell: ShellToolConfig = Field(default_factory=ShellToolConfig)


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
    #: 需要人工确认的工具：``{工具名: true}``。
    #: 只写需要**额外**确认的；声明了副作用的工具会被自动加进来（见下一项）。
    interrupt_on: dict[str, bool] = Field(default_factory=dict)
    #: 是否自动拦截声明了副作用（``side_effects=True``）的工具。
    #: 关掉它意味着「写文件、发消息」这类操作不经确认直接执行。
    require_approval_for_side_effects: bool = True
    #: 等待人工确认的时长（秒）。等不到就按**拒绝**处理 ——
    #: 悬着的确认等于没有确认，默认必须偏向安全的一侧。
    approval_timeout_seconds: float = 300.0
    #: 系统提示词里最多列出几个技能。索引必须**有界**，
    #: 否则技能一多，光是索引就把上下文顶坏了。
    skill_index_limit: int = 40
    #: 保留字段。**未使用**：原为接入 deepagents/LangGraph 预留，该方案已放弃，
    #: 保留只为兼容既有配置文件。
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
    # 下面三个字段**未被任何代码读取**：它们来自早期「虚拟文件系统后端」的设想
    # （后端选择 / 虚拟路径模式 / composite 路由），该方案已放弃。保留字段
    # 只是为了兼容既有配置文件 —— 真正生效的是 root_dir + 工具层的 resolve 校验。
    backend: FilesystemBackendName = "state"
    root_dir: str = "./data/workspace"
    virtual_mode: bool = True
    routes: list[FilesystemRouteConfig] = Field(default_factory=list)


# --------------------------------------------------------------------------- context


class ContextConfig(_Section):
    """上下文工程：把「模型能装多少」真正用起来。

    三件事分别对应三个失败模式：

    * ``max_input_tokens`` —— 历史无限增长把请求顶坏（按 token 预算裁剪）；
    * 摘要压缩 —— 单纯裁剪会让助手「忘事」；
    * 大结果卸载 —— 一次 5 万字的工具返回会挤掉后续所有请求。
    """

    #: **未使用**：原为 deepagents 的 ``write_todos`` 预留，计划已取消。
    #: 需要任务清单时用 ``note_*`` 写进工作区/笔记 —— 模型能看到，人也能改。
    todo_list: bool = True
    summarization: bool = True
    #: 次要触发条件：消息条数超过它时也考虑压缩。
    summarize_after_messages: int = 40
    #: 压缩时**始终保留**的最近消息条数（它们对当前对话最相关）。
    keep_recent_messages: int = 12
    offload_large_results: bool = True
    #: 单条工具结果超过这个字节数就落盘，上下文里只留预览 + 路径。
    offload_threshold_bytes: int = 20000
    #: 送进模型的历史 token 预算。**这是最关键的一个数**：
    #: 默认值保守（成本可预期），大上下文模型应该调高。
    max_input_tokens: int = 80000
    #: 用量超过预算的这个比例时触发压缩（0–1）。
    summarize_threshold_ratio: float = 0.7
    #: 卸载目录。默认 `<filesystem.root_dir>/.offload` ——
    #: 必须落在工作区内，否则模型拿不到路径也读不回来。
    offload_dir: str | None = None
    #: 注入系统提示词的记忆上限（记忆是**每轮都带**的，必须精简）。
    max_memory_chars: int = 8000


# --------------------------------------------------------------------------- storage


class StorageConfig(_Section):
    database_url: str = "sqlite+aiosqlite:///./data/doudou.db"
    #: 会话历史的存储位置：``database`` 落库（重启可续），``memory`` 只在进程内。
    history: Literal["memory", "database"] = "database"
    #: 单个会话在库里保留的消息条数上限（**保留**，不是送进模型的窗口 ——
    #: 后者由 ``context.max_input_tokens`` 决定）。
    #: 保留得多一些是有意的：压缩是有损的，原始历史要能回溯。
    max_messages_per_thread: int = 500
    # ``checkpointer`` / ``store`` 是 LangGraph 的概念，**两个字段都没有被使用**：
    # 原本预留给「接入 deepagents 引擎」的设想，该方案已放弃。会话续跑与历史一致性
    # 由本仓库自己的 ``gateway.session`` + ``storage`` 负责，保留字段以兼容既有配置文件。
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
    #: ``websocket`` 用长连接收事件（无需公网地址，推荐本地开发与内网部署）；
    #: ``callback`` 用 HTTP 事件回调（需要公网 HTTPS 地址与验签解密）。
    mode: Literal["callback", "websocket"] = "callback"
    base_url: str = "https://open.feishu.cn"
    bot_name: str = "Doudou"
    #: 机器人自己的 open_id。留空时按 ``bot_name`` 或事件的 mentioned_type 判定 @。
    bot_open_id: str | None = None
    #: 应用凭据。留空时由 .env 的 FEISHU_APP_ID / FEISHU_APP_SECRET 注入。
    app_id: str | None = None
    app_secret: str | None = None
    event_path: str = "/webhooks/feishu/events"
    reply_mode: Literal["card", "text"] = "card"
    stream_reply: bool = True
    card_update_interval_ms: int = 800
    allowed_chats: list[str] = Field(default_factory=list)
    allowed_users: list[str] = Field(default_factory=list)
    group_require_at: bool = True
    #: 是否按话题（thread_id）隔离上下文。
    #: 默认 ``False`` = 同一个群共享一个上下文（群里的助手像一个同事）。
    isolate_by_thread: bool = False
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
    """**未使用**：早期预留的 LangSmith 开关。追踪改用 Langfuse（见下）。
    保留字段只为兼容既有配置文件。"""

    enabled: bool = False
    project: str = "doudou"


class OtelConfig(_Section):
    """**未使用**：早期预留的 OTel 导出配置。"""

    enabled: bool = False
    endpoint: str | None = None


class MetricsConfig(_Section):
    enabled: bool = True


class AuditConfig(_Section):
    """审计：每次工具调用都留一条痕。"""

    enabled: bool = True
    #: 参数摘要的**单字段**长度上限（先打码再截断）。
    max_arg_chars: int = 2000
    #: 结果只留开头一段，全文在历史消息与日志里。
    max_result_chars: int = 500
    #: 无数据库时的内存上限（环形缓冲，超出丢最旧）。
    max_memory_entries: int = 5000
    #: 有数据库时保留的总条数，超出按时间清理（避免无限增长）。
    max_rows: int = 50000
    #: 攒多少条写一次库。``1`` = 逐条严格持久化（慢但丢得最少）；
    #: 默认 50 是为了绕开 SQLite 的单写者瓶颈，见 docs/performance.md。
    batch_size: int = 50


class CostConfig(_Section):
    """成本核算：按会话/按天累计，超预算只告警。"""

    enabled: bool = True
    currency: str = "USD"
    #: 每日预算；``0`` 表示不设。超了只告警（见 docs/observability.md）。
    daily_budget: float = 0.0
    #: 模型 → 每 100 万 token 的价格。键可以是 ``provider:model`` 或模型短名，
    #: ``default`` 兜底。**查不到价格时 cost 记为 null，绝不按 0 算。**
    prices: dict[str, dict[str, float]] = Field(default_factory=dict)
    #: 攒多少条用量写一次库（同 ``audit.batch_size``，合并写入还能少几次读改写）。
    batch_size: int = 50


class TracingConfig(_Section):
    """Langfuse 追踪。"""

    #: 显式开关：默认关闭，避免无意间把对话内容发到第三方。
    enabled: bool = False
    host: str = "https://cloud.langfuse.com"
    #: 打进 trace 标签，便于区分环境（prod / staging）。
    environment: str | None = None
    release: str | None = None
    #: 采样率（0–1）。全量追踪很贵时压低它。
    sample_rate: float = 1.0


class ObservabilityConfig(_Section):
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)
    #: 下面两个**未使用**，保留以兼容既有配置（见各自的 docstring）。
    langsmith: LangSmithConfig = Field(default_factory=LangSmithConfig)
    otel: OtelConfig = Field(default_factory=OtelConfig)


# --------------------------------------------------------------------------- root


class AppConfig(_Section):
    """``config/config.yaml`` 的根结构。"""

    version: int = 1
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    filesystem: FilesystemConfig = Field(default_factory=FilesystemConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)


__all__ = [
    "AgentConfig",
    "AppConfig",
    "AuditConfig",
    "ChannelsConfig",
    "CliChannelConfig",
    "ContextConfig",
    "CostConfig",
    "FeishuChannelConfig",
    "FilesToolConfig",
    "FilesystemConfig",
    "FilesystemRouteConfig",
    "HttpToolConfig",
    "LangSmithConfig",
    "McpConfig",
    "MetricsConfig",
    "ModelConfig",
    "NotesToolConfig",
    "ObservabilityConfig",
    "OtelConfig",
    "PermissionRule",
    "RuntimeConfig",
    "ServerConfig",
    "ShellToolConfig",
    "StorageConfig",
    "ToolsConfig",
    "TracingConfig",
    "WebSearchToolConfig",
    "WebhookChannelConfig",
]
