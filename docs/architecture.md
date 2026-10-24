# 架构设计

## 1. 分层

```
┌───────────────────────────── 入口层 ─────────────────────────────┐
│  channels/{feishu,webhook,cli}     api/{routes}     cli/         │
└───────────────┬────────────────────────┬────────────────────────┘
                │  InboundMessage        │  RunRequest
                ▼                        ▼
┌─────────────────────────── gateway ──────────────────────────────┐
│  session（会话↔thread 映射）  bus（并发队列）                      │
│  router（分发）  runner（执行 + 事件回流）                        │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼ agent 调用
┌──────────────────────────── agent ───────────────────────────────┐
│  factory（create_deep_agent 组装）                                │
│  models  middleware  backends  memory  subagents  permissions     │
│  checkpointer                                                     │
└───────┬──────────────────────────────────────────┬───────────────┘
        │ tools=                                   │ 依赖
        ▼                                          ▼
┌────────────────────┐              ┌──────────────────────────────┐
│ tools.registry     │              │ storage   observability      │
│  ├─ tools.builtin  │              │ security  utils              │
│  └─ mcp.client     │              └──────────────────────────────┘
│ skills.registry    │
└────────────────────┘
```

## 2. 模块职责

### 2.0 ReAct 引擎（M1 已实现）

`agent/react.py` 是循环本体，只有两个动作：**推理**与**执行**。

```
run_start
  └─ step_start(step)
       ├─ 推理：model.astream(history) → text_delta*  + 累积 tool_call_chunks
       ├─ 无工具调用 → run_end(text)           ← 收敛
       └─ 有工具调用 → tool_call* → 并发执行 → tool_result* → 下一轮 step
```

关键设计：

- **事件化**：输出统一为 `RunEvent`（`agent/events.py`），CLI / SSE / 飞书卡片各自决定怎么渲染。
- **工具调用按 index 手工累积**：只依赖 `tool_call_chunks` 协议，对 OpenAI / Anthropic / 兼容网关都成立，不假定 provider 的返回形态。
- **单步失败不中断**：工具异常、工具不存在、参数 JSON 坏了，都转成错误 `ToolMessage` 回填给模型，让它换一种做法。
- **人在回路**：`interrupt_on` 登记的工具在执行前调用 `approval_hook`；没有回调时**拒绝执行**而不是静默放行。
- 所有工具结果回填后进入下一轮，直到无工具调用或达到 `agent.max_steps`。

### 2.1 入口层

| 模块 | 职责 |
| --- | --- |
| `channels.base` | 定义 `Channel` 协议：`start()` / `stop()` / 把外部消息归一化成 `InboundMessage` / `send(OutboundMessage)`。 |
| `channels.registry` | 按配置实例化并启停通道。（M4） |
| `channels.feishu` | 飞书长连接或事件回调接入，验签解密、去重、卡片渲染。（M4） |
| `channels.webhook` | 通用入站 webhook，便于对接其他 IM / 自动化平台。（M4） |
| `channels.cli_channel` | 终端交互，本地开发不依赖任何外部平台。✅ |
| `api.app` | FastAPI 应用工厂：路由、中间件、异常处理、CORS。✅ |
| `api.lifespan` | 进程级单例（agent / 数据库）的创建与释放，中间件与日志在此初始化。✅ |
| `api.deps` | 依赖注入：运行期状态、runner、Bearer 鉴权。✅ |
| `api.errors` | 领域异常 → 统一错误响应的翻译层。✅ |
| `api.routes.chat` | `POST /chat` 与 `POST /chat/stream`（SSE）；`map_event()` 是纯函数，可单测。✅ |
| `api.routes.threads` / `tools` / `health` | 会话与历史、工具清单、健康与就绪检查。✅ |

### 2.2 编排层 `gateway`

| 模块 | 职责 |
| --- | --- |
| `history` | `HistoryStore` 协议 + 内存实现。**AgentRunner 只依赖这个协议**，因此历史放内存还是落库对上层透明。✅ |
| `session` | thread_id 的生成与规范化（`<channel>-<conversation>-<随机后缀>`）。✅ |
| `bus` | 进程内异步消息总线，按会话串行、跨会话并发。（M4） |
| `router` | 把 `InboundMessage` 路由到对应 thread，组装 agent 输入。（M4） |
| `runner` | 调用 agent、消费事件流、把结果落库，并做「同一 thread 串行」的并发保护。✅ |
| `schema` | 层间共享的 `RunRequest` 定义。✅ |

**关键点**：`gateway` 不 import 任何具体通道实现，只依赖 `channels.base` 协议；
也不认识 SQLAlchemy —— 落库由 `storage.history.SqlHistoryStore` 实现 `HistoryStore` 协议完成。

### 2.3 Agent 层 `agent`

| 模块 | 职责 |
| --- | --- |
| `factory` | 唯一的 Agent 装配点：模型 + 工具 + 提示词 → `ReactAgent`。 |
| `events` | `RunEvent` 定义。放在 agent 层是因为它是 Agent 的「产品」，被 gateway / channels / api 消费。 |
| `react` | ReAct 循环本体：推理 → 工具调用 → 观察 → 收敛。 |
| `models` | 通过 `init_chat_model` 解析 `provider:model`，配置超时/重试/回退；含 `scripted:*` 离线模型。 |
| `testing` | 确定性脚本模型，供单元测试、CI 与 `--model scripted:demo` 离线自检共用。 |
| `prompts` | 系统提示词加载与模板变量渲染。 |
| `middleware` | M9：接入 deepagents 中间件（todo / 摘要 / 提示词缓存）。 |
| `backends` | M9：虚拟文件系统后端（state / store / filesystem / composite）。 |
| `memory` | M7：`AGENTS.md` 等记忆文件的加载与回写。 |
| `subagents` | M8：从 `agents.yaml` 生成子 Agent 定义。 |
| `permissions` | M8：文件系统读写权限规则。 |
| `checkpointer` | M9：LangGraph checkpointer / store 选型（仅在切换到 deepagents 引擎时用）。 |

### 2.4 能力供给

- **`tools`**：`registry` 汇总三类工具 —— 内置工具、MCP 工具、外部插件工具，并做重名检测与分组。所有工具进入 agent 前都是 LangChain `BaseTool`。
- **`mcp`**：`config` 解析 server 声明 → `client` 用 `MultiServerMCPClient` 建立会话 → `loader` 拉取并适配工具（含 include/exclude 过滤与名称前缀）→ `lifecycle` 随应用启停与健康检查。
- **`skills`**：`loader` 扫描 `skills/*/SKILL.md` 校验 frontmatter，`registry` 建索引，交给 `SkillsMiddleware` 做渐进式加载。

### 2.5 支撑

| 模块 | 职责 |
| --- | --- |
| `config` | `settings`（env）+ `schema`（类型模型）+ `loader`（合并优先级）。全局唯一配置入口。✅ |
| `storage.database` | 引擎、会话工厂、建表；SQLite 外键开关；自动建父目录。✅ |
| `storage.models` | `threads` / `messages` / `session_bindings` 三张表。✅ |
| `storage.repositories` | 单表读写与聚合，不持有会话生命周期。✅ |
| `storage.history` | `HistoryStore` 的 SQL 实现 + LangChain 消息 ↔ ORM 行的编解码。✅ |
| `observability` | structlog 结构化日志（含噪音压制与脱敏）、运行指标。✅ |
| `security.secrets` / `security.auth` | 脱敏、Bearer 令牌校验。✅ |
| `utils.tokens` | token 计数（tiktoken `cl100k_base`，离线回退字符启发式）。✅ |
| `agent.context` | 上下文工程：预算裁剪、摘要压缩、大结果卸载。✅ |
| `agent.memory` | 长期记忆：`memory_files` 的加载与回写。✅ |
| `storage/migrations` | Alembic 迁移。（暂缓，见该目录 README） |

## 2.6 上下文工程

三个机制对三种「上下文被顶坏」的方式，都在 `agent/context.py`：

| 问题 | 机制 | 不做会怎样 |
| --- | --- | --- |
| 历史无限增长 | 按 token 预算**裁剪** | 请求被 provider 拒绝 |
| 裁剪等于丢信息 | **摘要压缩**（原文不删） | 助手「忘了刚才说的事」 |
| 单条结果过大 | **卸载到文件** | 一次 5 万字返回挤掉后续请求 |

压缩是**有损**的，出错表现是「悄悄忘事」而不是报错，因此设计上有四条约束：

1. **原始历史不删** —— 压缩只影响「这次送给模型的窗口」，库里保留完整消息，
   `GET /threads/{id}` 能回看；`GET /threads/{id}/context` 同时给出两边的数字；
2. **摘要可增量复用** —— 逐轮把「上一版摘要 + 新纳入的旧消息」再压一次，
   而不是从零重压（既慢又贵，且会让摘要随轮次漂移）；
3. **卸载目录落在工作区内** —— 模型要靠 `workspace_read` 读回来；
   配到工作区外会让「卸载」变成「丢弃」，因此配置错位置会告警；
4. **所有数字都上报** —— `runner.run_finished` 带 `ctx_tokens` / `ctx_compressed` /
   `ctx_summarized` / `ctx_dropped`。

`AgentRunner` 每轮：加载完整历史 → 需要时压缩 → 按预算取最近一段 → 执行。
注意 `storage.max_messages_per_thread` 管的是**库的增长**，
与「送进模型的窗口」（`context.max_input_tokens`）是两件事。

## 3. 两条主链路

### 3.1 飞书消息

```
用户发消息
  → 飞书开放平台事件推送（HTTP 回调）
  → api.routes.webhooks → channels.feishu.events（验签、解密、按 event_id 去重）
  → 归一化 InboundMessage
  → gateway.router：查 session 得 thread_id（无则创建）
  → gateway.runner：agent.astream_events(...)
  → 事件增量 → channels.feishu.cards 更新互动卡片
  → 落库（messages 表）+ 记指标
  → 3 秒内返回 200 给飞书（避免重推）
```

> 飞书要求回调在 **3 秒内**响应。因此 webhook 路由只做「验签 + 入队」，
> 真正执行放在后台任务，回复通过卡片更新或主动发消息完成。

### 3.2 HTTP 调用

```
POST /api/v1/chat            → 非流式，返回完整结果 + thread_id
POST /api/v1/chat/stream     → SSE: start → (delta | tool_call | tool_result)* → done
GET  /api/v1/threads         → 会话列表
GET  /api/v1/threads/{id}    → 历史消息
DELETE /api/v1/threads/{id}  → 删除会话
GET  /api/v1/tools           → 工具清单
GET  /api/v1/health[/ready]  → 健康 / 就绪（免鉴权）
```

会话解析规则（`api/routes/chat.py::_to_request`）：

| 请求带什么 | 行为 |
| --- | --- |
| `thread_id` | 直接用该会话（客户端自己记账） |
| `conversation_id` | 按渠道会话复用/分配，服务端持久绑定 |
| 都不带 | 本次请求自成一个新会话，id 在响应里返回 |

## 4. 并发与状态

- 同一 `thread_id` 上串行执行（`AgentRunner` 持进程内锁），避免历史互相踩踏；不同会话完全并发。
- 进程内状态只剩「thread 锁」与指标计数器；**历史与渠道绑定都已落库**，
  因此重启进程后同一 thread 仍可继续对话。
- 多副本时 thread 锁需要换成分布式锁（见 docs/roadmap.md M10）。

## 5. 扩展点

| 想加什么 | 改哪里 |
| --- | --- |
| 新工具 | `src/doudou/tools/builtin/` 新增模块，`registry` 自动登记 |
| 新 MCP server | `config/mcp.servers.yaml` 加一条，无需改代码 |
| 新 Skill | 根目录 `skills/<name>/SKILL.md` |
| 新通道（如钉钉/微信） | `channels/` 新增子包并实现 `Channel` 协议，注册进 `channels.registry` |
| 新模型厂商 | `doudou.agent.models` 注册 provider，或直接装 `langchain-<provider>` 并用 `provider:model` 写法 |
| 换持久化 | 改 `DOUDOU_DATABASE_URL` + 对应 checkpointer/store 选型 |
