# Doudou

基于 [deepagents](https://github.com/langchain-ai/deepagents) 构建的**个人助手 Agent**。

支持自定义 **Tool**、**MCP**（Model Context Protocol）、**Skill**（Agent Skills 标准），
并通过**飞书（Lark）通道**与用户对话；需要网络服务时使用 **FastAPI** 对外暴露 REST / SSE 接口。

> **当前进度：M4 已完成** —— 配置、ReAct 循环、工具集（检索 / 抓取 / 笔记 / 只读文件）、
> 会话网关、CLI、**HTTP 服务（REST + SSE + 持久化 + 鉴权）** 与
> **飞书通道（WebSocket 长连接 + 流式卡片）** 均端到端可用，
> 339 个离线测试 + `ruff` + `mypy --strict` 全绿。
> MCP（M5）、Skill 渐进式加载（M6）尚在路线图中，详见 [`docs/roadmap.md`](docs/roadmap.md)。
>
> 不需要 API Key 就能验证完整链路：`uv run doudou ask "现在几点了？" --model scripted:demo`

---

## 1. 核心能力

| 能力 | 实现方式 | 对应模块 | 状态 |
| --- | --- | --- | --- |
| ReAct 循环 | 自研：推理 → 工具调用 → 观察 → 收敛 | `doudou.agent.react` | ✅ M1 |
| 工具调用 | LangChain `@tool` + 注册表装配 | `doudou.tools` | ✅ M1 |
| 多轮会话 | 会话→thread 映射 + 内存历史 | `doudou.gateway` | ✅ M1 |
| CLI 通道 | rich 渲染流式输出与工具日志 | `doudou.channels.cli_channel` | ✅ M1 |
| 人在回路 | `interrupt_on` + 终端确认 | `doudou.agent.react` | ✅ M1 |
| HTTP API | FastAPI + SSE + 落库 + Bearer 鉴权 | `doudou.api` | ✅ M2 |
| 会话持久化 | 消息/线程/渠道绑定落库，重启可续 | `doudou.storage` | ✅ M2 |
| 实用工具集 | 检索 / 抓取 / 笔记 / 只读文件，默认拒绝 | `doudou.tools.builtin` | ✅ M3 |
| 飞书通道 | 长连接收事件 + 流式卡片 + 群里共享上下文 | `doudou.channels.feishu` | ✅ M4 |
| 消息总线 | 按会话串行、跨会话并发、积压拒绝 | `doudou.gateway.bus` | ✅ M4 |
| MCP 工具 | `langchain-mcp-adapters` | `doudou.mcp` | ⏳ M5 |
| Skill 加载 | Agent Skills 标准（`SKILL.md`）渐进式披露 | `doudou.skills` | 🚧 发现 ✅ / 加载 ⏳ M6 |
| 上下文工程 | token 预算、摘要压缩、长期记忆 | `doudou.gateway` / `doudou.agent.memory` | ⏳ M7 |
| 权限与子 Agent | 路径权限、子 Agent 委派、跨重启确认 | `doudou.agent.permissions` | ⏳ M8 |
| 任务规划 | `write_todos`（deepagents 引擎） | `doudou.agent.middleware` | ⏳ M9 |
| 虚拟文件系统 | 可插拔 backend | `doudou.agent.backends` | ⏳ M9 |

## 1.1 立刻可跑（不需要任何 API Key）

```powershell
uv run doudou config check                                       # 校验配置
uv run doudou tools list                                         # 看已装载的工具
uv run doudou ask "现在几点了？" --model scripted:demo            # 跑一轮带工具调用的对话
uv run doudou chat --model scripted:demo                         # 交互式多轮
```

`scripted:demo` 是内置的确定性离线模型（`doudou.agent.testing`）：它会按关键词决定是否
调用 `now` 工具，因此**完整的 ReAct 循环（推理 → 工具调用 → 观察 → 收敛）无需联网即可验证**。
接入真实模型只需在 `.env` 里配好 `DOUDOU_MODEL__PRIMARY` 与对应 API Key。

## 2. 架构总览

```
                     ┌──────────────── 通道层 Channels ────────────────┐
   飞书用户 ──事件──▶ │  feishu   │   webhook   │   cli               │
                     └────┬─────────────┬──────────────┬────────────┘
                          │             │              │
                          ▼             ▼              ▼
                  ┌──────────── Gateway（会话 / 路由 / 运行）────────────┐
   HTTP 客户端 ──▶ │  session 映射 · 消息总线 · agent 执行 · 事件回流    │
   (FastAPI)      └───────────────────────┬───────────────────────────┘
                                          ▼
                        ┌──────── Agent Core（deepagents）────────┐
                        │  create_deep_agent                      │
                        │  ├─ middleware: todo / skills / fs      │
                        │  ├─ subagents                           │
                        │  ├─ tools  ◀── Tool Registry            │
                        │  │            ├─ builtin tools          │
                        │  │            └─ MCP tools              │
                        │  └─ checkpointer / store                │
                        └────────────────────────────────────────┘
```

设计原则：

- **通道与 Agent 解耦**：通道只负责「收消息 / 发消息」，所有编排逻辑集中在 `gateway`。
- **能力可插拔**：工具、MCP server、Skill 均通过配置声明，不写死在代码里。
- **配置驱动**：`config/*.yaml` 描述 Agent、子 Agent、MCP、通道；环境变量与密钥走 `.env`。
- **同一套 Agent 服务多渠道**：飞书、Webhook、CLI、HTTP API 共享同一个 Agent 实例。

## 3. 目录结构

```
Doudou/
├─ config/                    # 声明式配置（可提交，密钥放 .env）
│  ├─ config.example.yaml     # 主配置模板
│  ├─ mcp.servers.example.yaml# MCP server 清单模板
│  └─ agents.yaml             # 主 Agent 提示词 + 子 Agent 定义
├─ docs/                      # 设计文档
├─ skills/                    # Agent Skills（每个子目录一个 SKILL.md）
├─ deploy/                    # 部署相关（Docker / K8s）
├─ scripts/                   # 开发脚本
├─ src/doudou/
│  ├─ cli/                    # 命令行入口
│  ├─ config/                 # 配置加载与校验
│  ├─ agent/                  # deepagents 组装：模型/中间件/后端/子 Agent
│  ├─ tools/                  # 工具注册表与内置工具
│  ├─ mcp/                    # MCP 客户端与生命周期
│  ├─ skills/                 # Skill 发现与索引
│  ├─ channels/               # 通道实现（feishu / webhook / cli）
│  ├─ gateway/                # 会话路由、消息总线、运行器
│  ├─ api/                    # FastAPI 应用与路由
│  ├─ storage/                # 持久化（typing / 消息 / 通道绑定）
│  ├─ observability/          # 日志、链路追踪、指标
│  ├─ security/               # 鉴权与密钥处理
│  └─ utils/                  # 通用工具
└─ tests/                     # 单元 + 集成测试
```

各目录职责详见 [`docs/architecture.md`](docs/architecture.md)。

## 4. 快速开始

### 4.1 零配置先跑起来（离线，不需要 API Key）

```powershell
uv sync

uv run doudou tools list                                  # 已装载的工具
uv run doudou ask "现在几点了？" --model scripted:demo      # 一轮带工具调用的对话
uv run doudou chat --model scripted:demo                   # 交互式多轮
```

`scripted:demo` 是内置的确定性离线模型，会按关键词决定是否调用 `now` 工具，
因此完整的 ReAct 循环（推理 → 工具调用 → 观察 → 收敛）无需联网即可验证。

### 4.2 接入真实模型

```powershell
Copy-Item .env.example .env
Copy-Item config/config.example.yaml config/config.yaml
Copy-Item config/mcp.servers.example.yaml config/mcp.servers.yaml   # M5 起需要

# 在 .env 里设置 DOUDOU_MODEL__PRIMARY 与对应的 API Key，例如：
#   DOUDOU_MODEL__PRIMARY=openai:gpt-5.5
#   OPENAI_API_KEY=sk-...

uv run doudou config check          # 校验配置与凭据，输出可操作的问题清单
uv run doudou chat
```

非 OpenAI 厂商需要装对应集成包：`uv sync --extra anthropic` / `--extra deepseek`。

**兼容 OpenAI 协议的网关**（自建服务、聚合平台）统一用 `openai:` 前缀 + `base_url`，
不需要额外的 provider 包：

```yaml
model:
  primary: openai:<网关上的模型名>
  base_url: https://<网关域名>/v1
```

详见 [`docs/configuration.md`](docs/configuration.md) 的「接入兼容 OpenAI 协议的网关」。

### 4.2 开启联网能力（可选）

默认配置下**联网工具不注册**：没有凭据的工具只会占提示词、诱导模型乱试。
想启用就补上对应的钥匙：

```dotenv
# 联网检索（Tavily）
TAVILY_API_KEY=tvly-...
```

```yaml
# config/config.yaml —— 出站抓取的域名白名单
tools:
  http:
    allowed_domains:
      - docs.python.org      # 含任意子域
      - "*.github.com"       # 只含子域
```

| 工具 | 启用条件 |
| --- | --- |
| `web_search` | `TAVILY_API_KEY` 非空 |
| `http_request` | `tools.http.allowed_domains` 非空，且目标域名命中（解析到内网地址也会被拒） |
| `workspace_read` / `workspace_list` | 始终可用，根目录 = `filesystem.root_dir` |
| `note_save` / `note_list` / `note_read` | 始终可用；**写笔记需要人工确认** |
| `now` / `days_between` | 始终可用 |

```powershell
uv run doudou tools list          # 看哪些工具已装载、哪些分组未启用及原因
uv run doudou config check        # 一次性检查配置、凭据、工具与 Skill
```

### 4.3 HTTP 服务

```powershell
uv run doudou serve                 # 默认 127.0.0.1:8000，接口前缀 /api/v1
uv run doudou serve --port 9000 --reload
```

启动后：

| 接口 | 说明 |
| --- | --- |
| `POST /api/v1/chat` | 单轮对话，一次返回完整答复 |
| `POST /api/v1/chat/stream` | SSE 流式：`start → (delta\|tool_call\|tool_result)* → done` |
| `GET /api/v1/threads` / `GET /api/v1/threads/{id}` | 会话列表 / 会话详情（含完整历史） |
| `DELETE /api/v1/threads/{id}` | 删除会话 |
| `GET /api/v1/tools` | 工具清单 |
| `GET /api/v1/health` / `/health/ready` | 健康 / 就绪检查（**不需要鉴权**） |
| `GET /docs` | OpenAPI 交互文档 |

会话历史默认落库（`storage.history: database`），**进程重启后同一 thread 仍可续聊**。
设置 `DOUDOU_API_TOKEN` 后，除健康检查外的接口都要求
`Authorization: Bearer <token>`。

```powershell
# 匿名单轮（响应里带回 thread_id，下次带上它即可续聊）
curl -X POST http://127.0.0.1:8000/api/v1/chat -H "Content-Type: application/json" `
     -d '{"message":"现在几点了？"}'
```

### 4.4 接入飞书（可选）

飞书通道用 **WebSocket 长连接**，不需要公网地址、不需要内网穿透：

```dotenv
# .env
DOUDOU_CHANNELS__FEISHU__ENABLED=true
FEISHU_APP_ID=cli_xxxxxxxx
FEISHU_APP_SECRET=xxxxxxxx
```

飞书开放平台里把事件订阅设为「**使用长连接接收事件**」并订阅
`im.message.receive_v1`，然后 `uv run doudou serve` —— 长连接随服务一起托管。

会话隔离默认是「**同一个群共享一个上下文**」（`isolate_by_thread: false`），
群里的助手像一个同事。完整步骤、@ 判定、卡片流式与已知限制见
[`docs/channels.md`](docs/channels.md)。

### 4.5 其他入口
```powershell
uv run doudou ask "问题" --quiet     # 只输出最终答复，便于脚本调用
```

飞书接入步骤见 [`docs/channels.md`](docs/channels.md)，MCP 接入见 [`docs/mcp.md`](docs/mcp.md)，
Skill 编写见 [`docs/skills.md`](docs/skills.md)。

## 5. 常用命令

```powershell
uv run doudou serve                        # 启动 HTTP 服务
uv run doudou chat                         # 终端交互式对话
uv run doudou ask "问题"                    # 单轮对话
uv run doudou ask "问题" --quiet            # 单轮，只输出答复
uv run doudou tools list [--args]          # 列出工具
uv run doudou tools show now               # 查看单个工具
uv run doudou skills list                  # 列出已发现 Skill
uv run doudou config show [--yaml]         # 打印生效配置（脱敏）
uv run doudou config check                 # 校验配置/凭据/工具/Skill
uv run doudou config path                  # 打印各路径的实际取值
uv run doudou version
make lint / test / fmt                     # 见 Makefile
```

## 6. 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 分层架构、模块职责、数据流 || [docs/configuration.md](docs/configuration.md) | 配置项、优先级、环境变量清单 |
| [docs/channels.md](docs/channels.md) | 飞书通道接入与时序 |
| [docs/mcp.md](docs/mcp.md) | MCP server 配置与工具装配 |
| [docs/skills.md](docs/skills.md) | Skill 目录规范与编写指引 |
| [docs/tools.md](docs/tools.md) | 自定义工具开发规范 |
| [docs/security.md](docs/security.md) | 鉴权、密钥、权限与审计 |
| [docs/roadmap.md](docs/roadmap.md) | 里程碑与待办 |

## 7. 许可证

Proprietary（内部项目）。
