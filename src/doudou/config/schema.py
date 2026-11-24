"""Typed models for YAML config: agent, channels, mcp, storage.

这里的模型既是 ``config/*.yaml`` 的结构定义，也是默认值来源：
``config.yaml`` 缺失时，仅靠本文件的默认值也能启动。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from doudou.config.paths import default_database_url, home_path

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
    #: 会话锁、消息队列和待确认请求在进程内，目前只支持单进程。
    workers: int = Field(default=1, ge=1, le=1)
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
    #: 主 provider 的 API Key。**只放 `.env`**，不要写进 config.yaml。
    #: 环境变量：``DOUDOU_MODEL_API_KEY``。
    api_key: str | None = None
    #: provider → API Key。给 fallbacks 或子 Agent 换 provider 时用；
    #: 环境变量：``DOUDOU_MODEL_API_KEYS``（JSON 对象）。
    api_keys: dict[str, str] = Field(default_factory=dict)
    #: provider → 自定义 base_url，优先于 ``base_url``；
    #: 环境变量：``DOUDOU_MODEL_BASE_URLS``（JSON 对象）。
    base_urls: dict[str, str] = Field(default_factory=dict)


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
    #: 搜索服务：``tavily`` 或 ``deepseek``（DeepSeek 自研搜索）。
    provider: Literal["tavily", "deepseek"] = "tavily"
    max_results: int = 5
    timeout: float = 20.0
    #: 当前 provider 的 API Key。**只放 `.env`**；
    #: 环境变量：``DOUDOU_TOOLS_WEB_SEARCH_API_KEY``。
    api_key: str | None = None
    #: 仅 Tavily：``advanced`` 更准但更慢、更贵。
    search_depth: Literal["basic", "advanced"] = "basic"
    #: 仅 Tavily：是否附带 Tavily 生成的摘要答案。
    include_answer: bool = True
    #: 仅 DeepSeek：是否在结果里返回网页正文（会显著增大上下文，默认只取 summary）。
    need_content: bool = False

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: Any) -> Any:
        """provider 是标识符，大小写不该影响配置能否启动（``DeepSeek`` 也认）。"""
        return value.strip().lower() if isinstance(value, str) else value


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
    #: 沙箱 backend：``""``（不隔离）或 ``"bwrap"``（Linux，需要 bubblewrap）。
    #: 打开后只有工作区可写，配置与密钥不在沙箱的可见范围内。
    sandbox: Literal["", "bwrap"] = ""
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


class BrowserToolConfig(_Section):
    """本地 Chrome 浏览器控制工具的配置（通过 CDP 连接或按需拉起）。"""

    enabled: bool = False
    #: Chrome DevTools Protocol 地址，默认连接本机 9222 调试端口。
    cdp_url: str = "http://127.0.0.1:9222"
    #: 调试端口未监听时，是否自动拉起本机 Google Chrome（独立进程，退出豆豆后仍保留窗口）。
    auto_launch: bool = True
    #: Chrome 可执行文件路径；留空时按系统常见路径自动探测。
    executable_path: str | None = None
    #: 独立 Chrome 用户数据目录，用于保留登录态与 Cookie；默认在 ``~/.doudou/data``。
    user_data_dir: str = Field(default_factory=lambda: home_path("data", "browser-profile"))
    timeout: float = 30.0
    #: 单次页面文本/元素摘要的最大字符数。
    max_read_chars: int = 12000
    #: 可选域名白名单；空列表表示允许访问任意 http/https 公网页面（``file://`` 等始终禁止）。
    allowed_domains: list[str] = Field(default_factory=list)


class ToolsConfig(_Section):
    #: 只启用这些分组；空列表表示全部启用。
    enabled_groups: list[str] = Field(default_factory=list)
    #: 逐个禁用的工具名（优先于 enabled_groups）。
    disabled: list[str] = Field(default_factory=list)
    http: HttpToolConfig = Field(default_factory=HttpToolConfig)
    web_search: WebSearchToolConfig = Field(default_factory=WebSearchToolConfig)
    files: FilesToolConfig = Field(default_factory=FilesToolConfig)
    shell: ShellToolConfig = Field(default_factory=ShellToolConfig)
    browser: BrowserToolConfig = Field(default_factory=BrowserToolConfig)


# --------------------------------------------------------------------------- agent


def _default_operations() -> list[PermissionOperation]:
    """默认同时允许读与写；具体放行与否由规则顺序决定。"""
    return ["read", "write"]


class PermissionRule(_Section):
    operations: list[PermissionOperation] = Field(default_factory=_default_operations)
    paths: list[str] = Field(default_factory=list)
    mode: PermissionMode = "allow"


#: 子 Agent 名字会成为 ``task`` 工具的参数取值，限制字符集。
_SUBAGENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class SubAgentSpec(_Section):
    """一个子 Agent 的声明（``agent.subagents[]``）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(pattern=_SUBAGENT_NAME_PATTERN)
    description: str
    #: 子 Agent 自己的系统提示词。**必须自洽** —— 它看不到主 Agent 的提示词。
    system_prompt: str = Field(default="", alias="prompt")
    #: 省略时继承主 Agent 工具（除 task）；显式 [] 表示不使用任何工具。
    tools: list[str] | None = None
    #: 子 Agent 专属技能目录（相对 ``config/`` 解析）。空表示不加载技能。
    skills: list[str] = Field(default_factory=list)
    #: 覆盖模型（``provider:model``）。省略或 null 表示继承主模型。
    model: str | None = None
    #: 子 Agent 的步数上限。它常做多步检索，所以比主 Agent 宽松些也无妨。
    max_steps: int = Field(default=6, ge=1)

    @property
    def label(self) -> str:
        return f"subagent:{self.name}"


class AgentConfig(_Section):
    name: str = "Doudou"
    system_prompt_file: str | None = "prompts/system.md"
    #: 记忆文件；``doudou init`` 生成的配置会指向 ``~/.doudou/config/AGENTS.md``。
    #: 默认留空，避免未初始化的环境凭空多出一个 ``remember`` 工具。
    memory_files: list[str] = Field(default_factory=list)
    #: Skill 根目录；默认指向 ``<config>/../skills/``（安装包布局即 ``~/.doudou/skills``）。
    skills: list[str] = Field(default_factory=lambda: ["../skills/"])
    #: 子 Agent 清单（不再单独一个 ``agents.yaml``）。
    subagents: list[SubAgentSpec] = Field(default_factory=list)
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
    #: ReAct 循环的单次运行最大步数（模型往返次数）
    max_steps: int = 8
    #: 单次模型返回中允许并发执行的工具调用数上限
    max_tool_calls_per_step: int = 8
    #: 多轮对话时每隔若干轮附在用户消息后并落库的人设锚点（用于保人设且命中 Prefix Cache）。
    persona_reminder: str | None = None
    #: 每隔多少轮用户消息插一次人设锚点（0 表示关闭；发生上下文摘要后会立即补一次）。
    persona_reminder_interval: int = Field(default=5, ge=0)
    #: 续聊旧会话跨天或间隔超过该小时数时固化一条时间上下文（0 表示关闭）。
    time_context_gap_hours: float = Field(default=4.0, ge=0.0)
    permissions: list[PermissionRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_subagents(self) -> AgentConfig:
        seen: set[str] = set()
        for spec in self.subagents:
            if spec.name in seen:
                raise ValueError(f"subagent 名重复：{spec.name!r}")
            if not spec.system_prompt.strip():
                raise ValueError(f"subagent {spec.name!r} 缺少 prompt（system_prompt 不能为空）")
            seen.add(spec.name)
        return self


# --------------------------------------------------------------------------- filesystem


class FilesystemConfig(_Section):
    #: 所有文件类工具的工作区；默认 ``~/.doudou/workspace``。
    root_dir: str = Field(default_factory=lambda: home_path("workspace"))


# --------------------------------------------------------------------------- context


class ContextConfig(_Section):
    """上下文工程：把「模型能装多少」真正用起来。

    三件事分别对应三个失败模式：

    * ``max_input_tokens`` —— 历史无限增长把请求顶坏（按 token 预算裁剪）；
    * 摘要压缩 —— 单纯裁剪会让助手「忘事」；
    * 大结果卸载 —— 一次 5 万字的工具返回会挤掉后续所有请求。
    """

    summarization: bool = True
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
    #: 默认 SQLite 放在 ``~/.doudou/data/doudou.db``。
    database_url: str = Field(default_factory=default_database_url)
    #: 会话历史的存储位置：``database`` 落库（重启可续），``memory`` 只在进程内。
    history: Literal["memory", "database"] = "database"
    #: 单个会话在库里保留的消息条数上限（**保留**，不是送进模型的窗口 ——
    #: 后者由 ``context.max_input_tokens`` 决定）。
    #: 保留得多一些是有意的：压缩是有损的，原始历史要能回溯。
    max_messages_per_thread: int = 500
    echo_sql: bool = False


# --------------------------------------------------------------------------- mcp

#: MCP transport。``http`` 在适配器里叫 ``streamable_http``（映射见 ``mcp/loader``）。
McpTransportName = Literal["stdio", "http", "sse"]

#: server 名会成为工具名前缀，因此限制字符集。
_MCP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class McpServerSpec(_Section):
    """一个 MCP server 的声明（``mcp.servers[]``）。

    transport 决定必填字段：``stdio`` 要 ``command``，``http`` / ``sse`` 要 ``url``；
    两者都给或都不给都会在**启动时**报错，而不是连不上时才让人猜。
    ``${ENV}`` 占位由 :mod:`doudou.mcp.config` 在构造连接前替换（密钥留在 ``.env``）。
    """

    name: str
    enabled: bool = True
    transport: McpTransportName = "stdio"
    description: str = ""

    # ---- stdio ----
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None

    # ---- http / sse ----
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    # ---- 工具面收敛 ----
    #: 白名单：非空时只暴露这些工具。
    include_tools: list[str] = Field(default_factory=list)
    #: 黑名单：显式屏蔽危险工具（删除、DDL、支付…）。
    exclude_tools: list[str] = Field(default_factory=list)
    #: 声明该 server 的工具都是只读的（免逐次人工确认）。
    #:
    #: 默认 ``False`` = 不确定 → 按「有副作用」处理、每次调用都要确认。
    #: 这是安全的默认：第三方 server 里既可能有查询也可能有删除，
    #: 猜错的方向必须是「多问一次」而不是「默默删了」。
    read_only: bool = False

    #: 拉取工具与调用工具的超时（秒）。
    timeout: float = 60.0
    #: 拉取失败时的重试次数（``mcp.defaults`` 可统一设置）。
    retries: int = 0

    @model_validator(mode="after")
    def _check(self) -> McpServerSpec:
        if not _MCP_NAME_PATTERN.match(self.name):
            raise ValueError(
                f"server 名 {self.name!r} 不合法：只允许字母、数字、下划线、连字符，"
                "且必须以字母或数字开头（它会成为工具名的前缀）。"
            )
        if self.transport == "stdio":
            if not self.command:
                raise ValueError(f"server {self.name!r} 使用 stdio 时必须提供 command。")
            if self.url:
                raise ValueError(f"server {self.name!r} 是 stdio，不应该有 url。")
        else:
            if not self.url:
                raise ValueError(f"server {self.name!r} 使用 {self.transport} 时必须提供 url。")
            if self.command:
                raise ValueError(f"server {self.name!r} 是 {self.transport}，不应该有 command。")
        overlap = set(self.include_tools) & set(self.exclude_tools)
        if overlap:
            raise ValueError(
                f"server {self.name!r} 的 include_tools 与 exclude_tools 同时包含 "
                f"{sorted(overlap)}；白名单优先，这样写只会造成困惑。"
            )
        return self

    @property
    def mcp_transport_name(self) -> str:
        """映射到 langchain-mcp-adapters 的 transport 字面量。"""
        return "streamable_http" if self.transport == "http" else self.transport

    @property
    def source_label(self) -> str:
        """工具来源标记：``mcp:<server>``。"""
        return f"mcp:{self.name}"

    def tool_allowed(self, tool_name: str) -> bool:
        """工具是否应当暴露。白名单优先于黑名单；两者都为空表示全部暴露。"""
        if self.include_tools:
            return tool_name in self.include_tools
        return tool_name not in self.exclude_tools


class McpDefaults(_Section):
    """``mcp.defaults``：被各 server 继承，**显式指定者优先**。

    只放「经常需要统一调整」的几个字段 —— 可继承字段越多，读配置时越难判断
    某个值到底来自哪里。
    """

    enabled: bool = True
    timeout: float = 60.0
    retries: int = 0
    read_only: bool = False


class McpConfig(_Section):
    """MCP 段：server 清单直接写在这里（不再单独一个 ``mcp.servers.yaml``）。"""

    enabled: bool = True
    #: 连不上某个 server 时是否让启动直接失败（默认只降级 + 告警）。
    fail_fast: bool = False
    #: 是否给 MCP 工具名加 ``<server>__`` 前缀。
    tool_name_prefix: bool = True
    call_timeout: float = 60.0
    defaults: McpDefaults = Field(default_factory=McpDefaults)
    servers: list[McpServerSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> McpConfig:
        seen: set[str] = set()
        for server in self.servers:
            if server.name in seen:
                raise ValueError(f"server 名重复：{server.name!r}")
            seen.add(server.name)
        # defaults 只填「没被显式写过」的字段（靠 model_fields_set 判断），
        # 因此「全局默认 60s，某个 server 单独写 5s」符合直觉。
        for server in self.servers:
            for field_name in ("enabled", "timeout", "retries", "read_only"):
                if field_name not in server.model_fields_set:
                    setattr(server, field_name, getattr(self.defaults, field_name))
        return self


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
    #: 应用凭据。留空时由环境变量注入（DOUDOU_CHANNELS_FEISHU_APP_ID / _APP_SECRET）。
    app_id: str | None = None
    app_secret: str | None = None
    #: 事件订阅校验 Token / 加密密钥；callback 模式需要，websocket 模式不用。
    verification_token: str | None = None
    encrypt_key: str | None = None
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


class VoiceChannelConfig(_Section):
    """本地电脑语音通道配置（sherpa-onnx 本地唤醒与断句 + DeepSeek ASR/TTS）。"""

    enabled: bool = False
    #: 唤醒词列表；只有以唤醒词开头的语音才会触发识别与执行。
    wake_words: list[str] = Field(default_factory=lambda: ["豆豆"])
    #: DeepSeek ASR 转写后的二次前缀校验（默认关闭：本地 KWS 已识别唤醒词，
    #: 开启后要求唤醒词与指令连着一口气说完且出现在同一段转写开头，适合极嘈杂环境）。
    strict_wake_prefix: bool = False
    #: 唤醒词常见同音字容错，只在二次前缀校验与剥离前缀时使用。
    wake_homophones: list[str] = Field(default_factory=lambda: ["豆豆", "逗逗", "都都"])
    #: 唤醒后等待用户继续说话的最大秒数（如只喊“豆豆”后短暂停顿再说指令）。
    command_wait_seconds: float = 4.0
    #: 说话尾部静音超过该秒数即判定一句话说完。
    silence_duration_seconds: float = 0.9
    #: 单条语音指令最长录制秒数（DeepSeek ASR 上限 120s）。
    max_record_seconds: float = 20.0
    #: sherpa-onnx KeywordSpotter 关键词打分与触发阈值。
    keywords_score: float = 1.5
    keywords_threshold: float = 0.25
    #: Silero VAD 人声判定阈值（0–1，越大越抗外放环境噪）。
    vad_threshold: float = 0.5
    #: 音箱播报结束后延迟恢复拾音的冷却秒数，防止自激唤醒。
    cooldown_seconds: float = 0.5
    #: 是否允许在音箱播报期间打断（支持本地打断词 + 终端按键兜底）。
    barge_in_enabled: bool = True
    #: 播报期间识别到这些词会立刻停止播报并回到待机。
    interrupt_words: list[str] = Field(default_factory=lambda: ["豆豆停", "停一下", "别说了"])
    #: 播报期间是否也允许直接喊唤醒词（如「豆豆」）打断并无缝接入下一句
    #: （外放音箱建议 false 防自激，戴耳机可设 true）。
    interrupt_with_wake_word: bool = False
    #: 语音接口凭据（放 .env 的 DOUDOU_CHANNELS_VOICE_API_KEY；留空回退 DeepSeek Key）。
    api_key: str | None = None
    asr_url: str = "https://api.deepseek.com/v1/tools/asr"
    tts_url: str = "https://api.deepseek.com/v1/tools/tts"
    tts_ws_url: str = "wss://api.deepseek.com/v1/tools/tts"
    voice_id: Literal["stella", "tide"] = "stella"
    filter_markdown: bool = True
    input_device: int | str | None = None
    output_device: int | str | None = None
    #: 本地 KWS/VAD 模型目录；默认 ``~/.doudou/data/models/voice``。
    models_dir: str = Field(default_factory=lambda: home_path("data", "models", "voice"))
    auto_download_models: bool = True


class ChannelsConfig(_Section):
    cli: CliChannelConfig = Field(default_factory=CliChannelConfig)
    feishu: FeishuChannelConfig = Field(default_factory=FeishuChannelConfig)
    webhook: WebhookChannelConfig = Field(default_factory=WebhookChannelConfig)
    voice: VoiceChannelConfig = Field(default_factory=VoiceChannelConfig)


# --------------------------------------------------------------------------- observability


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


class TracesConfig(_Section):
    """运行轨迹：一次运行一行，步骤明细存 JSON（供 ``/traces`` 与 ``/stats`` 用）。

    与审计/成本不同，轨迹会带上正文（每一步的模型文本、工具参数与结果），
    因此**默认开**（本地落库、正是统计页的数据源），但用 ``max_rows`` 压住增长、
    用 ``max_text_chars`` 截断单段文本。轨迹是可重建的派生数据，放心压制。
    """

    enabled: bool = True
    #: 落库时的行数上限，超出按时间清理（避免无限增长）。
    max_rows: int = 20000
    #: 无数据库时的内存上限（环形缓冲，超出丢最旧）。
    max_memory_entries: int = 2000
    #: 攒多少条写一次库（同 ``audit.batch_size``，绕开 SQLite 单写者瓶颈）。
    batch_size: int = 50
    #: 单段文本（步骤文本、工具参数/结果、输入输出摘要）的截断长度。
    max_text_chars: int = 1000


class ObservabilityConfig(_Section):
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    traces: TracesConfig = Field(default_factory=TracesConfig)


# --------------------------------------------------------------------------- scheduler


class SchedulerConfig(_Section):
    """定时任务：聊天里创建，到点过一遍 LLM 再把结果推到目标会话。"""

    enabled: bool = True
    #: 轮询间隔（秒）。到点任务的最小触发精度就是它，别设得太小。
    tick_seconds: float = 30.0
    #: 解释 cron/间隔时用的时区；留空继承 ``runtime.timezone``。
    timezone: str | None = None
    #: 单个部署最多保留多少条任务（超出时拒绝新建）。
    max_tasks: int = 200
    #: 单次触发允许的最长运行时间（秒）；超时当作本次失败，不再等。
    run_timeout_seconds: float = 600.0
    #: CLI / Web 建的任务推给谁的飞书私聊：填飞书 ``open_id``。
    #: 留空时只能靠「唯一一条私聊绑定」推断，多用户场景务必显式配置。
    owner_user_id: str | None = None
    #: 错过触发点（如服务停机）后，是否补跑一次。默认不补 —— 补跑会一次性
    #: 涌出一堆过期推送，通常不是用户想要的。
    catch_up: bool = False


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
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)


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
    "HttpToolConfig",
    "McpConfig",
    "MetricsConfig",
    "ModelConfig",
    "ObservabilityConfig",
    "PermissionRule",
    "RuntimeConfig",
    "SchedulerConfig",
    "ServerConfig",
    "ShellToolConfig",
    "StorageConfig",
    "SubAgentSpec",
    "ToolsConfig",
    "TracesConfig",
    "WebSearchToolConfig",
    "WebhookChannelConfig",
]
