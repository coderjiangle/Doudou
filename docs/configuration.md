# 配置说明

## 1. 优先级与命名

```
代码内默认值  <  config/config.yaml  <  环境变量
```

环境变量只有一条规则：

> **`DOUDOU_` + `config.yaml` 的路径，层级用下划线连接。**

| config.yaml | 环境变量 |
| --- | --- |
| `server.port` | `DOUDOU_SERVER_PORT` |
| `model.primary` | `DOUDOU_MODEL_PRIMARY` |
| `model.max_tokens` | `DOUDOU_MODEL_MAX_TOKENS` |
| `tools.web_search.provider` | `DOUDOU_TOOLS_WEB_SEARCH_PROVIDER` |
| `channels.feishu.app_secret` | `DOUDOU_CHANNELS_FEISHU_APP_SECRET` |

字段名本身带下划线（`max_tokens`、`base_url`）也不会歧义 —— 加载器用
`AppConfig` 的叶子路径做精确匹配，而不是 pydantic 的嵌套分隔符。

- 进程环境变量 > `.env` > `config.yaml`。
- 列表/字典字段写 **JSON**：
  `DOUDOU_MODEL_FALLBACKS='["openai:gpt-5.5"]'`。
- 凭据也是配置字段，只是约定**只放 `.env`**，不要写进 `config.yaml`。
- 旧写法（`DOUDOU_MODEL__PRIMARY`、无前缀的 `OPENAI_API_KEY`）**不再读取**，
  迁移表见 §2.7。

## 2. 环境变量

下表列出常用项；没有列出的字段按同一规则推导即可（`config show` 能看到完整路径）。

### 2.1 运行环境

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUDOU_RUNTIME_ENV` | `local` | `local` / `dev` / `prod` |
| `DOUDOU_RUNTIME_DEBUG` | `true` | 调试模式，prod 应关闭 |
| `DOUDOU_RUNTIME_LOG_LEVEL` | `INFO` | 日志级别 |
| `DOUDOU_RUNTIME_LOG_JSON` | `false` | 输出 JSON 日志（prod 建议 true） |
| `DOUDOU_RUNTIME_TIMEZONE` | `Asia/Shanghai` | `now` 工具的默认时区，也是日志时间戳所用时区 |
| `DOUDOU_SERVER_HOST` / `DOUDOU_SERVER_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `DOUDOU_SERVER_BASE_PATH` | `/api/v1` | API 前缀 |
| `DOUDOU_SERVER_WORKERS` | `1` | 当前仅支持 1；其它值会在配置校验时拒绝 |
| `DOUDOU_FILESYSTEM_ROOT_DIR` | `./data/workspace` | 工作区根目录 |
| `DOUDOU_CONFIG_DIR` / `DOUDOU_CONFIG_FILE` | `./config` / `config.yaml` | 配置位置 |

### 2.2 模型

| 变量 | 说明 |
| --- | --- |
| `DOUDOU_MODEL_PRIMARY` | `provider:model`，如 `openai:gpt-5.5`、`anthropic:claude-sonnet-5`、`deepseek:deepseek-chat`；`scripted:demo` 为内置离线模型 |
| `DOUDOU_MODEL_FALLBACKS` | 备用模型列表（JSON），按列表顺序尝试 |
| `DOUDOU_MODEL_TEMPERATURE` / `_MAX_TOKENS` / `_TIMEOUT` / `_MAX_RETRIES` | 采样与限制 |
| `DOUDOU_MODEL_BASE_URL` | 自建/兼容网关地址 |
| `DOUDOU_MODEL_API_KEY` | 主 provider 的 API Key |
| `DOUDOU_MODEL_API_KEYS` | 多 provider 的 key（JSON）：`'{"openai": "sk-...", "anthropic": "sk-ant-..."}'` |
| `DOUDOU_MODEL_BASE_URLS` | 多 provider 的地址（JSON） |

`api_keys` / `base_urls` 优先于 `api_key` / `base_url`。非 OpenAI 厂商需要装对应包：
`uv sync --extra anthropic` / `--extra deepseek`。

`model.fallbacks` 用于主 Agent、摘要调用和子 Agent 的模型调用。每个 ReAct 步骤
先调用该 Agent 的主模型，失败后按列表顺序尝试备用模型；所有模型绑定同一组工具。
如果本步骤已经输出文本，则直接报告错误，不再切换，避免混合两份回答。
备用模型构造失败会告警并跳过；跨 provider 时请补齐 `api_keys` / `base_urls` 与对应依赖。
测试直接注入模型时不自动构造配置中的备用模型。

#### 接入兼容 OpenAI 协议的网关

```yaml
# config/config.yaml
model:
  primary: openai:<网关上的模型名>
  base_url: https://<网关域名>/v1
```

```dotenv
# .env
DOUDOU_MODEL_API_KEY=<网关签发的 Key>
```

`langchain-openai` 已随主依赖安装，无需额外 `--extra`。

### 2.3 Agent

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUDOU_AGENT_NAME` | `Doudou` | 助手名称（会渲染进提示词） |
| `DOUDOU_AGENT_MAX_STEPS` | `8` | ReAct 循环单次运行的最大步数 |
| `DOUDOU_AGENT_SKILL_INDEX_LIMIT` | `40` | 提示词里最多列几个 Skill |
| `DOUDOU_AGENT_INTERRUPT_ON` | `{}` | 额外需确认的工具（JSON 对象） |
| `DOUDOU_AGENT_PERMISSIONS` | `[]` | 路径权限规则（JSON 数组） |
| `DOUDOU_AGENT_PERSONA_REMINDER` | `null` | 多轮对话时附在最新用户消息后的轻量人设提醒（仅发给模型，不入库） |
| `DOUDOU_AGENT_PERSONA_REMINDER_AFTER_MESSAGES` | `2` | 历史非系统消息超过该条数后触发 `persona_reminder` |

### 2.4 上下文工程

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUDOU_CONTEXT_MAX_INPUT_TOKENS` | `80000` | 送进模型的历史 token 预算 |
| `DOUDOU_CONTEXT_SUMMARIZE_THRESHOLD_RATIO` | `0.7` | 超过预算多少比例开始压缩 |
| `DOUDOU_CONTEXT_KEEP_RECENT_MESSAGES` | `12` | 压缩时保留最近几条 |
| `DOUDOU_CONTEXT_SUMMARIZATION` | `true` | 是否启用摘要压缩 |
| `DOUDOU_CONTEXT_OFFLOAD_LARGE_RESULTS` | `true` | 超大工具结果落盘 |
| `DOUDOU_CONTEXT_OFFLOAD_THRESHOLD_BYTES` | `20000` | 落盘阈值 |
| `DOUDOU_CONTEXT_MAX_MEMORY_CHARS` | `8000` | 长期记忆注入上限 |

### 2.5 存储 / MCP

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUDOU_STORAGE_DATABASE_URL` | `sqlite+aiosqlite:///./data/doudou.db` | 业务库 |
| `DOUDOU_STORAGE_HISTORY` | `database` | `database`（重启可续）/ `memory` |
| `DOUDOU_STORAGE_MAX_MESSAGES_PER_THREAD` | `500` | 单会话保留的消息条数 |
| `DOUDOU_MCP_ENABLED` | `true` | MCP 总开关（server 清单在 `config.yaml` 的 `mcp.servers`） |

> SQLite 文件所在目录不存在时会自动创建；生产建议换 Postgres：
> `uv sync --extra postgres` 后把 URL 改成 `postgresql+asyncpg://...`。

### 2.6 飞书 / 可观测性

| 变量 | 说明 |
| --- | --- |
| `DOUDOU_CHANNELS_FEISHU_ENABLED` | 是否启用 |
| `DOUDOU_CHANNELS_FEISHU_MODE` | `callback`（事件回调）或 `websocket`（长连接） |
| `DOUDOU_CHANNELS_FEISHU_STREAM_REPLY` | 是否用卡片流式更新 |
| `DOUDOU_CHANNELS_FEISHU_ALLOWED_CHATS` | 白名单（JSON 数组） |
| `DOUDOU_CHANNELS_FEISHU_APP_ID` / `_APP_SECRET` | 应用凭证（只放 `.env`） |
| `DOUDOU_CHANNELS_FEISHU_VERIFICATION_TOKEN` | 事件订阅校验 Token |
| `DOUDOU_CHANNELS_FEISHU_ENCRYPT_KEY` | 加密模式密钥；prod 必须配置 |
| `DOUDOU_CHANNELS_FEISHU_BASE_URL` | 国内版 `https://open.feishu.cn`，国际版 `https://open.larksuite.com` |
| `DOUDOU_OBSERVABILITY_AUDIT_ENABLED` | 审计开关 |
| `DOUDOU_OBSERVABILITY_AUDIT_BATCH_SIZE` | 攒批写；`1` = 逐条持久化 |
| `DOUDOU_OBSERVABILITY_AUDIT_MAX_ROWS` | 审计表行数上限 |
| `DOUDOU_OBSERVABILITY_COST_ENABLED` | 成本统计开关 |
| `DOUDOU_OBSERVABILITY_COST_DAILY_BUDGET` | 日预算（美元）；`0` = 不设，超了只告警 |
| `DOUDOU_OBSERVABILITY_TRACES_ENABLED` | 运行轨迹落库（本地，默认开） |
| `DOUDOU_OBSERVABILITY_TRACES_MAX_ROWS` | 轨迹表行数上限 |
| `DOUDOU_OBSERVABILITY_TRACES_MAX_TEXT_CHARS` | 单段文本截断长度 |

### 2.7 从旧变量迁移

旧写法已不再读取，对照替换即可：

| 旧 | 新 |
| --- | --- |
| `DOUDOU_SERVER__PORT` | `DOUDOU_SERVER_PORT` |
| `DOUDOU_MODEL__PRIMARY` | `DOUDOU_MODEL_PRIMARY` |
| `OPENAI_API_KEY` | `DOUDOU_MODEL_API_KEY`（多 provider 用 `DOUDOU_MODEL_API_KEYS`） |
| `OPENAI_BASE_URL` | `DOUDOU_MODEL_BASE_URL` |
| `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` | `DOUDOU_MODEL_API_KEYS` |
| `TAVILY_API_KEY` / DeepSeek 搜索 key | `DOUDOU_TOOLS_WEB_SEARCH_API_KEY` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | `DOUDOU_CHANNELS_FEISHU_APP_ID` / `_APP_SECRET` |
| `FEISHU_BASE_URL` | `DOUDOU_CHANNELS_FEISHU_BASE_URL` |

> `LANGFUSE_*` / `DOUDOU_OBSERVABILITY_TRACING_*` 已随第三方追踪一并移除，
> 现在轨迹落本地库（`observability.traces`），无需凭据。

## 3. config.yaml 主要段落

`version` / `runtime` / `server` / `model` / `agent` / `tools` / `filesystem` / `context` / `storage` / `mcp` / `channels` / `observability`

逐字段含义见 `config/config.example.yaml` 内注释。

### tools

```yaml
tools:
  enabled_groups: []          # 空 = 全部启用；分组：web / http / files / clock …
  disabled: []                # 逐个禁用的工具名
  http:
    allowed_domains: []       # 空 = http_request 不注册（默认拒绝一切出站请求）
  web_search:
    provider: tavily          # tavily | deepseek
    api_key: null             # 只放 .env：DOUDOU_TOOLS_WEB_SEARCH_API_KEY
    max_results: 5
    need_content: false       # 仅 deepseek：是否返回网页正文（体积大）
  files:
    max_read_chars: 50000
```

要点：

- **web_search 的两个 provider** 共用一个 key 路径 `tools.web_search.api_key`，换 provider
  不必换字段名；`tavily` 支持 `search_depth` / `include_answer`，`deepseek` 走自研搜索，
  结果优先取 `summary`，`need_content: true` 才会返回网页正文。`provider` 大小写不敏感。
- **分组开关**：`enabled_groups` 非空时只有列出的分组生效；`disabled` 按工具名精确禁用，
  优先级更高。
- **依赖不满足就不注册**：没有 `DOUDOU_TOOLS_WEB_SEARCH_API_KEY` 就没有 `web_search`；
  `allowed_domains` 为空就没有 `http_request`。用 `doudou tools list` 查看原因。
- `files` 的根目录固定用 `filesystem.root_dir`，没有独立配置项 —— 避免出现「两个 root」。
- 域名匹配规则：`example.com` 含任意子域，`*.example.com` 只含子域。

### agent 里的确认策略

```yaml
agent:
  interrupt_on: {}                                  # 额外的需确认工具
  require_approval_for_side_effects: true           # 有副作用的工具自动需确认
```

工具用 `side_effects=True` 自声明（见 `docs/tools.md`），因此大多数情况下不需要写 `interrupt_on`。
显式写 `workspace_write: false` 可以关掉某个工具的确认。

几个容易踩坑的点：

- `agent.system_prompt_file` 相对于 `config/` 目录解析。
- `agent.skills` 里可以写多个根目录，全部会被扫成 skill 索引（只注入索引，正文按需读）。
- `filesystem.root_dir` 是所有文件类工具的边界，改它等于改「助手能碰哪里」。
- `agent.permissions` 首匹配生效，**没有匹配到任何规则时放行**，所以「默认拒绝」那条兜底
  必须写在最后（而显式拒绝要写在最前）。匹配的是相对工作区、以 `/` 开头的 POSIX 路径；
  `/**/.env` 能匹配根目录的 `/.env`，`/secrets/**` 也能匹配 `secrets` 目录本身。
- `tools.shell.enabled` 默认 `false`。打开之前请先读 `docs/security.md` §9 ——
  权限规则对它无效，能给的保护只有超时、输出上限、环境变量白名单与人工确认。
- `channels.feishu.reply_mode: card` 才能用流式更新；`text` 模式只有一次性回复。

## 4. MCP 清单

清单写在 `config/config.yaml` 的 `mcp.servers` 里（不再单独一个 `mcp.servers.yaml`），
格式与要点见 [`mcp.md`](mcp.md) §2。摘要：

- 支持 `${ENV_VAR}` 占位，启动时从环境变量替换；**变量缺失会报错**（而不是发出去换 401）。
- `include_tools` / `exclude_tools` 用于收敛工具面（推荐显式收窄，减少模型误调用）。
- `fail_fast: false` 时单个 server 连不上只告警，不影响整体启动。
- 字段名写错、transport 与必填字段对不上属于**配置错误**：启动前就报错。

## 5. 校验与查看

```bash
uv run doudou config check     # 校验配置合法性
uv run doudou config show      # 打印合并后的生效配置（脱敏）
```

## 6. 图片输入

Web 支持选图、粘贴截图、移除待发送图片和原图预览；只发图片也可。
飞书支持图片消息及富文本中的图片，需额外开通 `im:resource` 权限，见
[飞书图片输入](channels.md#210-图片输入)。CLI 仍只支持文字。

`model.primary` 与用于回退的模型需要具备视觉能力；配置方式与文字模型相同。
仅接通通道不会让纯文字模型具备看图能力。离线脚本模型可验证传输与存储链路，但不会识别图片。

两种聊天接口均支持 `images`：

```json
{
  "message": "请分析这张截图",
  "images": [
    {"data_url": "data:image/png;base64,图片的base64内容"}
  ]
}
```

- 每条最多 4 张，每张最多 5 MiB；仅接受 PNG、JPEG、GIF、WebP 的内嵌 base64 图片。
- 服务端检查大小、base64 和图片格式签名，不从任意 URL 或本地路径加载图片。
- `message` 可以为空，但文字与图片不能同时为空。超限或格式不符返回 422。
- 历史接口保持 `content` 为文字，并通过 `images` 数组返回图片 data URL。
  图片内容与文字一同持久化，旧的纯文字数据无需迁移。
- 历史 token 预算对图片使用每张 4096 token 的估算，不对 base64 文本编码计数；
  这不是实际计费 token 数。摘要只接收文字及图片占位标记，不会把 base64 当正文。
- 上下文压缩可能移除早期图片，但原始图片仍按历史存储的保留策略保存。
