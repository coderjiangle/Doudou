# 架构设计

## 1. 分层

```
┌─────────────────────────────── 入口层 ───────────────────────────────────┐
│  [doudou serve 服务端进程]           [本地客户端进程（默认走 HTTP/SSE）]  │
│  channels.feishu                     cli/{main,remote} (RemoteAgentRunner)│
│  api/{app,routes} (/ui, /stats) ◀──▶ channels/{cli_channel,voice}        │
└──────────────┬───────────────────────────┬──────────────────┬────────────┘
               │ InboundMsg / RunRequest   │ (仅 --local /    │
               │                           │  doudou ask 虚线 │
               ▼                           │  直连 RunnerLike)│
┌──────────────────────── gateway & scheduler ─────────────────────────────┐
│  scheduler.runtime（定时轮询 → router.run_now → 通道推送）               │
│  session（会话↔thread 映射）  bus（并发队列）                            │
│  router（分发）  runner（RunnerLike / 执行 + 事件回流 + 屏蔽取消落库）    │
└───────────────────────────────┬──────────────────────────────────────────┘
                                ▼ agent 调用
┌────────────────────────────── agent ─────────────────────────────────────┐
│  factory（AgentBundle 组装）  react（ReAct 循环）  events                │
│  models  messages  context  memory  subagents  permissions approvals     │
└───────┬──────────────────────────────────────────────┬───────────────────┘
        │ tools=                                       │ 依赖
        ▼                                              ▼
┌─────────────────────┐                 ┌──────────────────────────────────┐
│ tools.registry      │                 │ storage   observability          │
│  ├─ tools.builtin   │                 │ security  utils                  │
│  └─ mcp.client      │                 │ config                           │
│ skills.registry     │                 └──────────────────────────────────┘
└─────────────────────┘
```

## 2. 模块职责

### 2.0 ReAct 引擎

`agent/react.py` 是循环本体，只有两个动作：**推理**与**执行**。

```
run_start
  └─ step_start(step)
       ├─ 推理：model.astream(history) → text_delta* + 累积 tool_call_chunks
       ├─ 无工具调用 → step_end → run_end(text)                  ← 收敛
       └─ 有工具调用 → tool_call* → approval* → 并发执行 → tool_result* → step_end → 下一轮 step
```

关键设计：

- **事件化**：输出统一为 `RunEvent`（`agent/events.py`），供 CLI、SSE、飞书卡片和语音播报消费；`step_end` 与 `run_end` 额外携带每步的 LLM 推理耗时、TTFT 首包延迟、工具墙钟耗时（按并发区间合并）与确认等待耗时。
- **工具调用按 index 手工累积**：只依赖 `tool_call_chunks` 协议，对 OpenAI / Anthropic / 兼容网关都成立，不假定 provider 的返回形态。
- **单步失败不中断**：工具异常、工具不存在、参数 JSON 坏了，都转成错误 `ToolMessage` 回填给模型，让它换一种做法。
- **人在回路**：`interrupt_on` 登记的工具在执行前调用 `approval_hook`；没有回调时**拒绝执行**而不是静默放行。
- 所有工具结果回填后进入下一轮，直到无工具调用或达到 `agent.max_steps`。

### 2.1 入口层（Client / Serve 分离）

`doudou chat` / `doudou threads` / `doudou voice` 与 `doudou serve` 之间通过 `RunnerLike` 协议解耦：默认以客户端进程运行（连向已在线的 `doudou serve`，或自动拉起随用随关的临时 `serve` 子进程，从而共享同一份持久化历史、MCP、定时任务与 `/stats` 统计）；仅传入 `--local` 或运行 `doudou ask` 时才走单进程本地直连。

| 模块 | 职责 |
| --- | --- |
| `cli.remote` | `RemoteAgentRunner`（实现 `RunnerLike`，经 HTTP/SSE 转发对话、审批与历史查询）与 `EphemeralServeProcess`（探测复用或托管临时 `doudou serve`）。 |
| `channels.base` | 定义 `Channel` 协议：`start()` / `stop()` / 把外部消息归一化成 `InboundMessage` / `send(OutboundMessage)`。 |
| `channels.registry` | 按配置实例化并启停通道。 |
| `channels.feishu` | 飞书长连接接入，去重与卡片渲染。 |
| `channels.cli_channel` | 终端交互，消费 `RunnerLike`；默认走 `RemoteAgentRunner` 连向 `serve`，`--local` / `ask` 走本地 `AgentRunner`。 |
| `channels.voice` | `doudou voice` 启动常驻监听。本地唤醒断句 + DeepSeek ASR 转写后通过 `RunnerLike` 提交文本（默认走 `RemoteAgentRunner` → `serve`，`--local` 走本地 `AgentRunner`），回复文本增量经 DeepSeek WebSocket TTS 流式播报，支持打断。 |
| `api.app` | FastAPI 应用工厂：路由、中间件、异常处理、CORS，以及内置聊天页（`/`、`/ui`）与统计页（`/stats`）。 |
| `api.lifespan` | 进程级单例（agent / 数据库 / MCP / 定时调度 / 审批中介）的创建与释放，中间件与日志在此初始化。 |
| `api.deps` | 依赖注入：运行期状态、runner、Bearer 鉴权。 |
| `api.errors` | 领域异常 → 统一错误响应的翻译层。 |
| `api.routes.chat` | `POST /chat` 与 `POST /chat/stream`（SSE）；支持 `approval_required` 推流，并在断连取消时用 `CancelScope(shield=True)` 保护收尾写库。 |
| `api.routes.threads` / `approvals` / `traces` / `tools` / `health` | 会话与历史、客户端确认回传、轨迹与 `/stats` 统计、工具清单、健康检查。 |

### 2.2 编排层 `gateway`

| 模块 | 职责 |
| --- | --- |
| `history` | `HistoryStore` 协议 + 内存实现。**AgentRunner 只依赖这个协议**，因此历史放内存还是落库对上层透明。 |
| `session` | thread_id 的生成与规范化（`<channel>-<conversation>-<随机后缀>`）。 |
| `bus` | 进程内异步消息总线，按会话串行、跨会话并发。 |
| `router` | 把 `InboundMessage` 路由到对应 thread，组装 agent 输入。 |
| `runner` | 定义 `RunnerLike` 协议与 `AgentRunner`：调用 agent、消费事件流、做「同一 thread 串行」并发保护，并在取消/断连时通过屏蔽收尾确保历史与审计完整落库。 |
| `schema` | 层间共享的 `RunRequest` 定义。 |

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
| `memory` | 助手记忆文件（默认 `~/.doudou/config/AGENTS.md`）的加载与回写。 |
| `context` | 上下文工程（预算裁剪 / 摘要压缩 / 大结果卸载）。 |
| `subagents` | 从 `config.yaml` 的 `agent.subagents` 生成子 Agent 注册表与工具筛选。 |
| `permissions` | 文件系统读写权限规则（首匹配生效 + containment 兜底）。 |
| `approvals` | 人工确认中介（待决登记 / 超时按拒绝 / 决策复用）。 |

### 2.3.1 可观测性

审计、成本、追踪挂在**同一份事件流**上，runner 只认识一个很薄的协议：

| 模块 | 职责 |
| --- | --- |
| `observability.observers` | `RunObserver` 协议 + `RunContext` / `RunSummary`；回调失败只告警（观察者是旁路） |
| `observability.audit` | 审计记录与 `AuditObserver`；参数**构造即打码**（`__post_init__`） |
| `observability.cost` | 价格表 + 用量累计 + `UsageObserver`（超预算只告警） |
| `observability.traces` | 轨迹记录与 `TraceObserver`（运行结束整批聚成步骤树并落库） |
| `observability.buffering` | 审计 / 用量 / 轨迹的**写后缓冲**（SQLite 单写者下的必要优化） |
| `observability.setup` | `build_observability()`：按配置装配，零配置零开销 |
| `storage.audit` / `storage.usage` / `storage.traces` | 落库实现（`audit_events` / `usage_daily` / `run_traces` 三张表） |

设计取舍见 `docs/observability.md`，容量数据见 `docs/performance.md`。

### 2.3.2 定时任务

| 模块 | 职责 |
| --- | --- |
| `scheduler.types` | 领域对象 `ScheduledTask`（**不绑定会话**，只记投递目标） |
| `scheduler.triggers` | 时间表达 → `next_run_at`（`at` / `every` / `cron`，cron 借 apscheduler 的 trigger 算） |
| `scheduler.targets` | 投递目标：创建时定目标、触发时解析飞书私聊、记「用户 → 私聊会话」绑定 |
| `scheduler.runtime` | 轮询到期任务 → `MessageRouter.run_now` 跑一轮 → 回写状态 |
| `scheduler.store` | 存储协议 + 内存实现（SQL 实现在 `storage.scheduled`） |
| `storage.scheduled` | 落库实现（`scheduled_tasks` / `channel_targets` 两张表） |

**任务不绑定任何会话**：触发时在投递目标会话里解析出 thread，消息并入该会话上下文。
没有可推送目标时跳过整次触发（不跑模型）。见 `docs/scheduler.md`。

> **没有 `middleware` / `backends` / `checkpointer` 这三个模块。**
> 规划、子 Agent、虚拟文件系统、上下文压缩分别由 `react` / `context` / `subagents` /
> `tools.builtin.files` 自己实现。

### 2.4 能力供给

- **`tools`**：`registry` 汇总三类工具 —— 内置工具、MCP 工具、外部插件工具，并做重名检测与分组。所有工具进入 agent 前都是 LangChain `BaseTool`。
- **`mcp`**：`config` 解析 server 声明 → `client` 用 `MultiServerMCPClient` 建立会话 → `loader` 拉取并适配工具（含 include/exclude 过滤与名称前缀）→ `lifecycle` 随应用启停与健康检查。
- **`skills`**：`loader` 扫描 `skills/*/SKILL.md` 校验 frontmatter，`registry` 建索引；
  系统提示词只注入索引，正文由 `read_skill` 工具按需读取（自研的渐进式加载）。

### 2.5 支撑

| 模块 | 职责 |
| --- | --- |
| `config` | `env`（环境变量→配置路径）+ `schema`（类型模型）+ `loader`（合并优先级）+ `settings`（凭据视图）。全局唯一配置入口。 |
| `storage.database` | 引擎、会话工厂、建表；SQLite 外键开关；自动建父目录。 |
| `storage.models` | `threads` / `messages` / `session_bindings` 三张表。 |
| `storage.repositories` | 单表读写与聚合，不持有会话生命周期。 |
| `storage.history` | `HistoryStore` 的 SQL 实现 + LangChain 消息 ↔ ORM 行的编解码。 |
| `observability` | structlog 结构化日志（含噪音压制与脱敏）、运行指标。 |
| `security.secrets` / `security.auth` | 脱敏、Bearer 令牌校验。 |
| `utils.tokens` | token 计数（tiktoken `cl100k_base`，离线回退字符启发式）。 |
| `agent.context` | 上下文工程：预算裁剪、摘要压缩、大结果卸载。 |
| `agent.memory` | 长期记忆：`memory_files` 的加载与回写（回写目标只有第一个文件）。 |
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
用户发消息（或点击确认卡片按钮）
  → 飞书开放平台经 WebSocket 长连接主动推送给 doudou serve
  → channels.feishu.channel（daemon 线程内的 FeishuWebSocketRunner / lark_oapi.ws.Client）
  → EventDedup 按 event_id 幂等去重 + parse_message_event 过滤与归一化
  → asyncio.run_coroutine_threadsafe 转交回主事件循环（不阻塞 WS 线程心跳）
  → 若含图片则下载并组装 InboundMessage
  → gateway.router（TargetRecorder 记录私聊投递目标 + MessageBus 按会话串行入队）
  → gateway.runner（AgentRunner → agent.astream_events）
  → 事件增量 → channels.feishu.cards 节流整卡 patch（失败兜底纯文本）
  → 若触发人工确认：推送按钮卡片 → 用户点击触发 card.action.trigger → ApprovalBroker
  → 落库（messages / audit_events / run_traces）
```

> 飞书通道当前仅实现 **WebSocket 长连接**，无需公网地址与 HTTP 回调端点，因此不走
> 回调验签与 AES 解密流程。WS 线程回调只在内存里做去重、解析与线程间投递，耗时极短且吞掉异常，
> 避免卡住心跳或触发飞书重复投递。

### 3.2 HTTP 调用

```
POST /api/v1/chat            → 非流式，返回完整结果 + thread_id
POST /api/v1/chat/stream     → SSE: start → (delta | tool_call | approval_required | tool_result)* → done
POST /api/v1/approvals/{id}  → 客户端提交人工确认决策
GET  /api/v1/threads         → 会话列表
GET  /api/v1/threads/{id}    → 历史消息
DELETE /api/v1/threads/{id}  → 删除会话
GET  /api/v1/traces / /stats → 运行轨迹（含 LLM/TTFT/工具耗时拆分）与统计汇总
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
- 多副本时 thread 锁需要换成分布式锁。

## 5. 扩展点

| 想加什么 | 改哪里 |
| --- | --- |
| 新工具 | `src/doudou/tools/builtin/` 新增模块，`registry` 自动登记 |
| 新 MCP server | `~/.doudou/config/config.yaml` 的 `mcp.servers` 加一条，无需改代码 |
| 新 Skill | 根目录 `skills/<name>/SKILL.md` |
| 新通道（如钉钉/微信） | `channels/` 新增子包并实现 `Channel` 协议，注册进 `channels.registry` |
| 新模型厂商 | `doudou.agent.models` 注册 provider，或直接装 `langchain-<provider>` 并用 `provider:model` 写法 |
| 换持久化 | 改 `DOUDOU_STORAGE_DATABASE_URL`（如切换 SQLite / Postgres）与 `storage.history` |
