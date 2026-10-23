# 路线图

> **ID 即执行顺序。** 里程碑按「能否尽早交付用户可见价值」排序，不做纯技术分层。
> 每个里程碑都必须给出可执行的验收命令。
>
> 本轮对旧版路线图做了三处修正，见文末「规划修正记录」。

## 已完成

### M0 — 骨架 ✅

- [x] 目录结构、分层约定、依赖方向
- [x] `pyproject.toml` / 依赖与工具链（uv + ruff + mypy + pytest）
- [x] 配置模板（`config/`）、环境变量模板（`.env.example`）
- [x] 文档骨架、CI、Dockerfile、docker compose、pre-commit、VSCode 配置

### M1 — 打通最小闭环 ✅

- [x] `config`：settings / schema / loader（默认 < yaml < local < 环境变量）
- [x] `agent.react`：自研 ReAct 循环，流式 `RunEvent`
- [x] `agent.models`：`provider:model`、超时/重试、兼容 OpenAI 协议的网关
- [x] `agent.prompts` / `agent.factory`：提示词组装 + Agent 装配
- [x] `tools.registry` + 首批内置工具（`now`、`days_between`）
- [x] `gateway`：会话映射 + 多轮历史 + 同会话串行
- [x] `channels.cli_channel` + `cli`（`chat` / `ask` / `tools` / `skills` / `config`）
- [x] `observability.logging`（结构化日志 + 噪音压制）、`security.secrets`（脱敏）
- [x] 105 个离线测试，ruff / `mypy --strict` 全绿
- [x] 验收：真实模型完成「调用 `now` → 观察 → 收敛」，日志含脱敏后的出入参

**验收命令**

```powershell
uv run doudou config check
uv run doudou ask "现在几点了？" --model scripted:demo   # 离线自检
uv run doudou ask "现在几点了？"                         # 真实模型
```

---

## 未完成

### M2 — 服务化：HTTP + SSE + 持久化 + 鉴权 ✅

- [x] `api.app` + `api.lifespan`：agent 与数据库连接进程级单例、优雅启停
- [x] `api.errors` + `api.middleware`：统一错误模型 + request id + 结构化访问日志
- [x] `POST /api/v1/chat`、`POST /api/v1/chat/stream`（SSE）
- [x] `GET /api/v1/threads`、`GET /api/v1/threads/{id}`、`DELETE /api/v1/threads/{id}`
- [x] `GET /api/v1/tools`、`GET /api/v1/health`、`GET /api/v1/health/ready`、`GET /api/v1/version`
- [x] `security.auth`：Bearer Token（常量时间比较，`DOUDOU_API_TOKEN` 为空时不鉴权）
- [x] `storage`：`database.py` / `models.py` / `repositories/`（threads / messages / session_bindings）
- [x] `gateway.history`：`HistoryStore` 协议 + 内存与 SQL 两种实现，runner 不再认识 SQLAlchemy
- [x] 会话历史**与渠道绑定**都落库 → 重启后可续同一会话
- [x] `observability.metrics` 最小版：运行数、工具调用数、HTTP 耗时
- [x] `doudou serve` 真正拉起 uvicorn（不覆盖日志配置、关闭重复的 access log）
- [x] 47 个新增测试（存储层 + HTTP 端到端），含「重启后可续」的验收用例
- [x] 验收：真实模型 + 真实 uvicorn 下跑通 `/chat`、SSE、重启后读历史并继续对话

**验收命令**

```powershell
uv run doudou serve
curl http://127.0.0.1:8000/api/v1/health
curl -X POST http://127.0.0.1:8000/api/v1/chat -H "Content-Type: application/json" -d '{\"message\":\"现在几点了？\"}'
```

> **与旧规划的一处偏差**：原计划在 M2 引入 Alembic 迁移。
> 实际先用 `Base.metadata.create_all` 建表（schema 全新、没有线上数据要迁移），
> Alembic 推迟到「第一次真正需要改表结构」时再补 —— 见 `storage/migrations/README.md`。

**风险回顾**：SSE 与飞书卡片共用 `RunEvent`。本次已按「`map_event()` 是纯函数」
设计映射层（`api/routes/chat.py`），M4 的卡片渲染可以直接复用同一份事件语义。

---

### M3 — 实用工具集（只读类）

**目标**：现在的助手只会报时。没有检索能力就只是个聊天机器人。

**交付物**

- [ ] `tools/builtin/web_search.py`：Tavily（`TAVILY_API_KEY` 缺失时**自动不注册**）
- [ ] `tools/builtin/http.py`：`http_request`，域名允许列表 + 超时 + 响应截断
- [ ] `tools/builtin/notes.py`：本地笔记/待办（读、写、列）
- [ ] `tools/builtin/files.py`：读取工作区文件（只读）
- [ ] `tools/decorators.py`：工具元数据（分组、是否需确认、超时）
- [ ] `utils/async_utils.py`：重试与并发编排
- [ ] `config` 增加 `tools.disabled` / `tools.enabled_groups` 开关

**验收**：`doudou ask "帮我查一下 X 的最新进展"` → 调用 `web_search` → 给出带来源的回答。

**依赖**：M1（弱依赖 M2，命令行即可验收）

**风险**：`http_request` 是 SSRF 面，允许列表必须默认拒绝；不要做成「任意 URL」。

> **写操作与命令执行刻意不放在这里**：它们需要权限规则与确认机制配套，见 M8。

---

### M4 — 飞书通道

**目标**：在飞书里真的能对话。这是产品的核心诉求。

**交付物**

- [ ] `channels/feishu/crypto.py`：AES 解密 + 签名/时间戳校验（含单测，覆盖篡改与重放）
- [ ] `channels/feishu/events.py`：事件解析 + `event_id` 幂等去重（TTL）
- [ ] `channels/feishu/schemas.py` / `client.py`：lark-oapi 封装（token 刷新、发消息、更新卡片）
- [ ] `api/routes/webhooks.py`：**3 秒内返回 200**，执行放后台
- [ ] `gateway/bus.py` + `gateway/router.py`：按会话的异步队列与分发
- [ ] `channels/feishu/cards.py`：流式卡片（节流更新）、工具调用进度、确认按钮
- [ ] 群聊 `@` 策略、`allowed_chats` / `allowed_users` 白名单
- [ ] `mode: callback | websocket` 两种接入方式

**验收**：飞书私聊与群聊（@ 机器人）都能流式回复；重复推送同一事件只回答一次；
`kubectl`/进程重启后飞书侧不再重推（幂等）。

**依赖**：M2（HTTP 端点）；M3（否则回复没有信息量）

**风险**

- 回调需公网地址，本地开发要靠内网穿透或 `websocket` 模式；
- 卡片更新有频控，`card_update_interval_ms` 需要实测调整；
- 3 秒限制意味着「验签 + 入队即返回」，执行必须真正异步。

---

### M5 — MCP

**目标**：工具来源从「写死在代码里」变成「配置声明」，接入现成生态。

**交付物**

- [ ] `mcp/config.py`：清单解析 + `${ENV}` 占位替换 + 严格校验
- [ ] `mcp/client.py`：`MultiServerMCPClient` 封装，stdio / http / sse 三种 transport
- [ ] `mcp/loader.py`：工具拉取 + include/exclude 过滤 + 名称前缀
- [ ] `mcp/lifecycle.py`：随应用启停、健康检查、stdio 子进程回收
- [ ] `GET /api/v1/mcp`、`POST /api/v1/mcp/refresh`
- [ ] `doudou mcp list` / `doudou mcp check <server>`
- [ ] 失败降级：单个 server 连不上只告警（`mcp.fail_fast: false`）

**验收**：接入 1 个本地 stdio server + 1 个远程 http server，两者的工具都能被模型正确选择；
关掉其中一个，服务仍然可用。

**依赖**：M2（检视接口）；工具通道 M1 已预留 `build_tools(extra=...)`

**风险**：MCP server 的工具面常常过大，必须靠白名单收敛，否则模型选错工具的概率显著上升。

---

### M6 — Skill 渐进式加载

**目标**：让「怎么做」与「能做什么」解耦，用户自己加 `SKILL.md` 就能扩展能力。

**交付物**

- [ ] `skills/registry.py`：索引 + 按 `description` 的检索接口
- [ ] 启停时把 Skill 的 `name` + `description` 注入系统提示词（**只注入索引**）
- [ ] `read_skill` 工具：模型判断需要时再读全文（渐进式披露，正文不进上下文预算）
- [ ] `doudou skills show <name>` / `doudou skills check` / `POST /api/v1/skills/reload`
- [ ] Skill 附属文件（`reference.md` / `templates/`）的按需读取路径

**验收**：问「帮我做一次结构化调研」→ 模型读到 `research` 的 description → 调用 `read_skill("research")`
→ 按其中的步骤执行；问无关问题时**不加载**该 Skill（系统提示词长度不增长）。

**依赖**：M1（`skills/loader.py` 的发现与校验已完成）

**关键判断**：**这一项不必等 deepagents。** deepagents 的 `SkillsMiddleware` 做的是同一件事
（启动只读 frontmatter、按需读全文），用「索引注入 + `read_skill` 工具」自研即可，
成本远低于换引擎。见 M9 的说明。

---

### M7 — 上下文工程

**目标**：把 100 万 token 的上下文真正用起来，并支持跨会话的长期记忆。

**交付物**

- [ ] `utils` 增加 token 计数（`tiktoken` 已在依赖树中）
- [ ] `AgentRunner` 的历史裁剪从「按条数」改为「按 token 预算」，且不切断工具调用组
- [ ] 摘要压缩：超阈值时把早期历史压成摘要（`context.summarize_after_messages` 生效）
- [ ] 大结果卸载：超 `offload_threshold_bytes` 的工具结果落盘，上下文里只留摘要 + 路径
- [ ] `agent/memory.py`：加载 `AGENTS.md` 到系统提示词；支持助手回写记忆
- [ ] `GET /api/v1/threads/{id}/context`：查看当前上下文占用（排查用）

**验收**：连续 50 轮对话不炸上下文；一次返回 5 万字的结果不会把后续请求顶坏；
`AGENTS.md` 里写的偏好在新会话中生效。

**依赖**：M2（持久化）

**风险**：压缩是有损的。摘要策略一旦出错会表现为「助手忘了刚才说的事」，
必须有可观测的占用指标，且压缩历史要可回溯。

---

### M8 — 子 Agent + 权限 + 完整人在回路

**目标**：让助手能做「危险」的事，同时不失控。

**交付物**

- [ ] `tools/builtin/shell.py`：`shell_exec`，**默认禁用**，限定工作目录 + 超时
- [ ] 写文件类工具（`write_file` / `edit_file` / `delete`），落在受控工作区
- [ ] `agent/permissions.py`：路径权限规则（首匹配生效，兜底显式 deny）
- [ ] `agent/subagents.py`：从 `agents.yaml` 生成子 Agent（独立 tools / skills / 权限）
- [ ] `task` 工具：主 Agent 委派子任务，子 Agent 只回传结论（隔离上下文）
- [ ] 确认状态落库：跨重启不丢，支持超时自动拒绝
- [ ] 飞书确认卡片回传决策
- [ ] 越权测试：试图读 `.env` / 写工作区外路径必须被拒

**验收**：让助手「把结果写到工作区文件」→ 弹确认 → 同意后写入成功；
试图写工作区外路径 → 被权限规则拒绝；子 Agent 完成调研只回传一段结论。

**依赖**：M6（Skill）、M7（上下文，子 Agent 依赖隔离与摘要）、M1（`interrupt_on` 已就绪）

**风险**：这是安全边界的关键节点。提示词注入的真实危害从这一刻开始出现——
**工具层必须做硬校验，不能只依赖提示词约束**。

---

### M9 — 引擎可选化（deepagents）

**目标**：把 deepagents 的能力接成**可选项**，而不是重写地基。

**交付物**

- [ ] `agent/factory.py` 增加 `agent.engine: react | deep` 配置
- [ ] `agent/middleware.py`：`TodoListMiddleware`、`SummarizationMiddleware`、`AnthropicPromptCachingMiddleware`
- [ ] `agent/backends.py`：虚拟文件系统后端（state / store / filesystem / composite）
- [ ] `agent/checkpointer.py`：LangGraph checkpointer / store（Postgres 切换）
- [ ] deepagents 事件流 → `RunEvent` 的适配层（`gateway` 与通道**不改**）
- [ ] `doudou config check` 报告当前生效引擎

**验收**：`agent.engine: deep` 时 CLI 与飞书行为一致；同一套测试对两个引擎都通过。

**依赖**：M6、M7 已交付（否则无法判断哪些能力需要它）

**⚠️ 这一项可能被取消。** 判断标准：如果 M6 的 Skill 加载与 M7 的上下文压缩自研效果够用，
deepagents 剩下的独特价值只有「虚拟文件系统 + 子 Agent 的内置实现 + 任务规划 UI」，
那就不值得引入 `langgraph` 运行时与 `create_deep_agent` 的事件适配成本。
**在 M6/M7 完成后重新评估，而不是提前投入。**

**风险**：`deepagents` 仍是 0.7.x，API 可能变动；`astream_events` 的事件形态与我们的
`RunEvent` 需要映射，映射层是新的出错面。

---

### M10 — 生产化

**目标**：能长期稳定跑，出问题能定位。

**交付物**

- [ ] Postgres：`storage` + checkpointer / store 切换
- [ ] 多副本：会话映射与队列下沉（Redis 锁 + 队列），去掉进程内状态
- [ ] `observability/tracing.py`：LangSmith / OpenTelemetry
- [ ] 审计日志：谁、何时、调用了什么工具、参数摘要、结果、是否人工确认
- [ ] 成本控制：token 预算告警、按会话用量统计
- [ ] 安全：`pip-audit`、镜像扫描、密钥轮换流程
- [ ] 压测与容量评估

**验收**：两个副本并行处理不同会话不出错；一次异常请求能从审计日志完整还原。

---

## 关键路径

```
M2 服务化 ──▶ M4 飞书通道 ──▶ 「在飞书里可用」
   │                              ▲
   └──▶ M3 工具集 ───────────────┘
              │
              ├──▶ M5 MCP（纯增量，可并行）
              └──▶ M6 Skill ──▶ M7 上下文 ──▶ M8 子Agent/权限 ──▶ M9 引擎（可选）──▶ M10 生产化
```

**最短可用路径**：M2 → M3 → M4。三件事做完就是一个「能在飞书里用、会查资料」的助手。

其余里程碑都可以在这之后按需插入；M5（MCP）与主线无依赖，想早点扩工具来源可以先做。

---

## 待决问题

| # | 问题 | 备选 | 影响 |
| --- | --- | --- | --- |
| 1 | 飞书接入方式 | 事件回调（需公网地址）vs WebSocket 长连接（本地友好） | 影响 M4 的开发与部署方式 |
| 2 | 会话隔离粒度 | 群聊内共享一个上下文 vs 每人独立 | 影响 `SessionStore` 的键设计 |
| 3 | 是否引入 `shell_exec` / 写文件 | 引入（能力大、风险大）vs 只做只读 | 影响 M8 的范围与安全投入 |
| 4 | 引擎选择 | 自研（M6/M7 之后评估）vs 引入 deepagents | 影响 M9 是否要做 |
| 5 | 上下文压缩策略 | 纯截断 vs 摘要 vs 向量检索召回 | 影响 M7 的实现复杂度 |
| 6 | Web UI | 只做 API vs 提供独立前端 | 影响 M2 的接口形态（是否要 WebSocket） |

---

## 风险清单

| 风险 | 触发点 | 缓解 |
| --- | --- | --- |
| 提示词注入导致越权动作 | M8 引入写文件/执行命令后 | 工具层硬校验 + 权限规则兜底 deny + 人工确认 |
| 成本失控 | 100 万上下文 + 38 万输出上限 | M7 的 token 预算；`max_tokens` 只封顶不提满 |
| 飞书回调超时导致重复回答 | M4 | 3 秒内返回 200 + `event_id` 幂等去重 |
| 卡片更新频控被限流 | M4 | 节流合并更新，间隔可配 |
| 多副本下会话状态分裂 | M10 | 会话映射与队列下沉到外部存储 |
| `deepagents` API 变动 | M9 | 适配层隔离；M9 本身可取消 |
| 本地（Windows）与生产（Linux）行为差异 | 全程 | CI 跑 Linux；`tzdata`、路径、Makefile 已分别处理 |

---

## 规划修正记录

本轮根据 M1 的实现经验，对旧版路线图做了三处修正：

1. **M2 不再依赖「LangGraph checkpointer」。**
   旧版把「SQLite checkpointer + 会话可续」写在 M2。但 checkpointer 是 LangGraph 的概念，
   而 M1 的 ReAct 循环是用自己的 `AgentRunner` 维护历史的。
   因此 M2 改为**我们自己的 `storage` 落库**；LangGraph checkpointer 挪到 M9，只在真的换引擎时才需要。

2. **Skill 的渐进式加载不再挂在 deepagents 上（M1.5 → M6）。**
   旧版把它放在「引擎可选化」里，意味着要先换引擎才能用 Skill。
   实际上「注入 Skill 索引 + 提供 `read_skill` 工具」用自研循环几十行就能实现，
   于是升格为独立里程碑 M6，并从 M6 的验收标准上明确「不加载无关 Skill」。

3. **「引擎可选化」从 M1.5 降级到 M9，且标记为可能取消。**
   旧版紧随 M1，但它的价值（规划 / 子 Agent / 虚拟文件系统）都要等 Skill 与上下文工程
   就位后才能判断是否必要。提前引入只会让 M2–M8 都建在一个更重的运行时上。
   改为「M6/M7 完成后重新评估」。

另外新增两个旧版遗漏的里程碑：**M3 实用工具集**（旧版只在 M1 里提过一句，实际是助手可用性的前提）
与 **M7 上下文工程**（旧版只在 M3 里夹带一句「摘要压缩」，但它是长会话可用的必要条件）。

### 旧 ID → 新 ID 映射

| 旧 | 新 | 说明 |
| --- | --- | --- |
| M1.5 引擎可选化 | **M9** | 降级并标记可能取消 |
| M2 HTTP 服务 | **M2** | 内容修正：改用自研 storage |
| M3 Skill 与上下文管理 | **M6** + **M7** | 拆成 Skill 加载与上下文工程两项 |
| M4 MCP | **M5** | 内容不变 |
| M5 飞书通道 | **M4** | 提到 MCP 之前（它是核心诉求） |
| M6 子 Agent 与人在回路 | **M8** | 增加权限与写类工具 |
| M7 生产化 | **M10** | 内容不变 |
| — | **M3** 实用工具集 | 新增 |
