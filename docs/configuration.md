# 配置说明

## 1. 优先级

```
代码内默认值  <  config/config.yaml  <  config/config.local.yaml  <  环境变量
```

- 密钥（App Secret、API Key、Token）**只放 `.env`**，绝不进 `config.yaml`。
- 环境变量前缀 `DOUDOU_`，嵌套用 `__` 分隔：`DOUDOU_SERVER__PORT=8080`。
- 模型厂商相关环境变量沿用官方命名（`OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 等），不加前缀。

## 2. 关键环境变量

命名约定：配置项一律 `DOUDOU_` 前缀 + 双下划线表示层级；凭据**无前缀**，沿用官方名。

### 运行环境

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUDOU_RUNTIME__ENV` | `local` | `local` / `dev` / `prod` |
| `DOUDOU_RUNTIME__DEBUG` | `true` | 调试模式，prod 应关闭 |
| `DOUDOU_RUNTIME__LOG_LEVEL` | `INFO` | 日志级别 |
| `DOUDOU_RUNTIME__LOG_JSON` | `false` | 输出 JSON 日志（prod 建议 true） |
| `DOUDOU_RUNTIME__TIMEZONE` | `Asia/Shanghai` | 默认时区，`now` 工具会用到 |
| `DOUDOU_SERVER__HOST` / `DOUDOU_SERVER__PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `DOUDOU_SERVER__BASE_PATH` | `/api/v1` | API 前缀 |
| `DOUDOU_FILESYSTEM__ROOT_DIR` | `./data/workspace` | 工作区根目录 |
| `DOUDOU_CONFIG_DIR` / `DOUDOU_CONFIG_FILE` | `./config` / `config.yaml` | 配置位置 |

### Agent

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUDOU_AGENT__NAME` | `Doudou` | 助手名称（会渲染进提示词） |
| `DOUDOU_AGENT__MAX_STEPS` | `8` | ReAct 循环单次运行的最大步数（模型往返次数） |
| `DOUDOU_AGENT__RECURSION_LIMIT` | `60` | **未使用**（原为接入 deepagents 预留，计划已取消） |

### 模型

| 变量 | 说明 |
| --- | --- |
| `DOUDOU_MODEL__PRIMARY` | `provider:model`，如 `openai:gpt-5.5`、`anthropic:claude-sonnet-5`、`deepseek:deepseek-chat`；`scripted:demo` 为内置离线模型 |
| `DOUDOU_MODEL__TEMPERATURE` / `__MAX_TOKENS` / `__TIMEOUT` / `__MAX_RETRIES` | 采样与限制 |
| `DOUDOU_MODEL__BASE_URL` | 自建/兼容网关地址（也可用 `OPENAI_BASE_URL`） |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` | 各家凭据（无前缀） |
| `TAVILY_API_KEY` | 内置 `web_search` 工具 |

> 非 OpenAI 厂商需要装对应包：`uv sync --extra anthropic` / `--extra deepseek`。

#### 接入兼容 OpenAI 协议的网关

自建网关或第三方聚合服务（只要实现了 OpenAI 的 `/chat/completions`）一律用
**`openai:` 前缀 + 自定义 `base_url`**，不要另找 provider 包：

```yaml
# config/config.yaml
model:
  primary: openai:<网关上的模型名>
  base_url: https://<网关域名>/v1
```

```dotenv
# .env
OPENAI_API_KEY=<网关签发的 Key>
```

`langchain-openai` 已随主依赖安装，无需额外 `--extra`。
`base_url` 也可以改用 `OPENAI_BASE_URL` 环境变量提供，但显式写在配置文件里
能让 `doudou config show` 直接看到实际生效的地址，排查时更省事。

#### 多模态（原生图片输入）

配置层目前**没有**多模态开关。若网关模型支持原生图片输入，需要先补两处：

1. `utils/text.py` 的 `content_to_text` 只抽取 text 块，会丢掉图片块；
2. ReAct 循环的输入是 `HumanMessage(content: str)`，还没有附件入口。

也就是说图片链路要等 M4（飞书收发图片）一并打通，届时图片块需要原样透传给模型，
不能走 `content_to_text`。

### 存储

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUDOU_STORAGE__DATABASE_URL` | `sqlite+aiosqlite:///./data/doudou.db` | 业务库 |
| `DOUDOU_STORAGE__HISTORY` | `database` | 会话历史存哪：`database`（重启可续）/ `memory`（进程内，轻量） |
| `DOUDOU_STORAGE__CHECKPOINTER` | `sqlite` | **未使用**：LangGraph 概念，接入 deepagents 的计划已取消（同 `store`） |
| `DOUDOU_OBSERVABILITY__AUDIT__ENABLED` | `true` | 审计开关（M9） |
| `DOUDOU_OBSERVABILITY__AUDIT__BATCH_SIZE` | `50` | 攒批写；`1` = 逐条严格持久化（SQLite 下慢 20 倍，见 `docs/performance.md`） |
| `DOUDOU_OBSERVABILITY__AUDIT__MAX_ROWS` | `50000` | 审计表行数上限，超出按时间清理 |
| `DOUDOU_OBSERVABILITY__COST__ENABLED` | `true` | 成本统计开关 |
| `DOUDOU_OBSERVABILITY__COST__DAILY_BUDGET` | `0` | 日预算（美元）；`0` = 不设。**超了只告警不拦截** |
| `DOUDOU_OBSERVABILITY__TRACING__ENABLED` | `false` | Langfuse 追踪；需同时有 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` |
| `DOUDOU_OBSERVABILITY__TRACING__HOST` | `https://cloud.langfuse.com` | 自建填自己的地址 |
| `DOUDOU_OBSERVABILITY__LANGSMITH__ENABLED` | `false` | **未使用**（早期预留） |
| `DOUDOU_STORAGE__STORE` | `sqlite` | 长期记忆存储（M7） |

> SQLite 文件所在目录不存在时会自动创建；生产建议换 Postgres：
> `uv sync --extra postgres` 后把 URL 改成 `postgresql+asyncpg://...`。
>
> **表结构**：M2 用 `create_all` 建表（`threads` / `messages` / `session_bindings`）。
> 第一次需要改表结构时再引入 Alembic，见 `src/doudou/storage/migrations/README.md`。

### 飞书

| 变量 | 说明 |
| --- | --- |
| `DOUDOU_CHANNELS__FEISHU__ENABLED` | 是否启用 |
| `DOUDOU_CHANNELS__FEISHU__MODE` | `callback`（事件回调）或 `websocket`（长连接） |
| `DOUDOU_CHANNELS__FEISHU__STREAM_REPLY` | 是否用卡片流式更新 |
| `DOUDOU_CHANNELS__FEISHU__ALLOWED_CHATS` | 白名单，逗号分隔 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 应用凭证（无前缀，必填） |
| `FEISHU_VERIFICATION_TOKEN` | 事件订阅校验 Token |
| `FEISHU_ENCRYPT_KEY` | 加密模式密钥；prod 必须配置 |
| `FEISHU_BASE_URL` | 国内版 `https://open.feishu.cn`，国际版 `https://open.larksuite.com` |

### MCP

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUDOU_MCP__ENABLED` | `true` | 总开关 |
| `DOUDOU_MCP__CONFIG_FILE` | `mcp.servers.yaml` | 清单路径（相对 `config/`） |

## 3. config.yaml 主要段落

`version` / `runtime` / `server` / `model` / `agent` / `tools` / `filesystem` / `context` / `storage` / `mcp` / `channels` / `observability`

逐字段含义见 `config/config.example.yaml` 内注释。

### tools

```yaml
tools:
  enabled_groups: []          # 空 = 全部启用；分组：web / http / files / notes / clock
  disabled: []                # 逐个禁用的工具名
  http:
    allowed_domains: []       # 空 = http_request 不注册（默认拒绝一切出站请求）
  web_search:
    max_results: 5
  notes:
    directory: ./data/notes
  files:
    max_read_chars: 50000
```

要点：

- **分组开关**：`enabled_groups` 非空时只有列出的分组生效；`disabled` 按工具名精确禁用，
  优先级更高。
- **依赖不满足就不注册**：没有 `TAVILY_API_KEY` 就没有 `web_search`；
  `allowed_domains` 为空就没有 `http_request`。
  用 `doudou tools list` 查看「哪个分组为什么不在」。
- `files` 的根目录固定用 `filesystem.root_dir`，没有独立配置项 —— 避免出现「两个 root」。
- 域名匹配规则：`example.com` 含任意子域，`*.example.com` 只含子域。

### agent 里的确认策略

```yaml
agent:
  interrupt_on: {}                                  # 额外的需确认工具
  require_approval_for_side_effects: true           # 有副作用的工具自动需确认
```

工具用 `side_effects=True` 自声明（见 `docs/tools.md`），因此大多数情况下不需要写 `interrupt_on`。
显式写 `note_save: false` 可以关掉某个工具的确认。

几个容易踩坑的点：

- `agent.system_prompt_file` 相对于 `config/` 目录解析。
- `agent.skills` 里可以写多个根目录，全部会被扫成 skill 索引（只注入索引，正文按需读）。
- `filesystem.root_dir` 是所有文件类工具的边界，改它等于改「助手能碰哪里」。
  同一段里还有几个字段（`backend` / `virtual_mode` / `routes`）**未被使用** ——
  它们来自「接入 deepagents 虚拟文件系统」的计划，该计划已取消，保留只为兼容旧配置。
- `agent.permissions` 首匹配生效，**没有匹配到任何规则时放行**，所以「默认拒绝」那条兜底
  必须写在最后（而显式拒绝要写在最前）。匹配的是相对工作区、以 `/` 开头的 POSIX 路径；
  `/**/.env` 能匹配根目录的 `/.env`，`/secrets/**` 也能匹配 `secrets` 目录本身。
- `tools.shell.enabled` 默认 `false`。打开之前请先读 `docs/security.md` §9 ——
  权限规则对它无效，能给的保护只有超时、输出上限、环境变量白名单与人工确认。
- `channels.feishu.reply_mode: card` 才能用流式更新；`text` 模式只有一次性回复。

## 4. MCP 清单

见 `config/mcp.servers.example.yaml`。要点：

- 支持 `${ENV_VAR}` 占位，启动时从环境变量替换。
- `include_tools` / `exclude_tools` 用于收敛工具面（推荐显式收窄，减少模型误调用）。
- `fail_fast: false` 时单个 server 连不上只告警，不影响整体启动。

## 5. 校验与查看

```powershell
uv run doudou config check     # 校验配置合法性
uv run doudou config show      # 打印合并后的生效配置（脱敏）
```
