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

### M3 — 实用工具集（只读类）✅

- [x] `tools/decorators.py`：工具注解（`group` / `side_effects` / `timeout`），挂在 `BaseTool.metadata`
- [x] `tools/builtin/web_search.py`：Tavily REST（不引入额外依赖），**缺 key 则整组不注册**
- [x] `tools/builtin/http.py`：`http_request`，默认拒绝 + 域名白名单 + 内网 IP 拒绝 + 仅 GET/HEAD + 截断
- [x] `tools/builtin/notes.py`：`note_save` / `note_list` / `note_read`，文件名安全化
- [x] `tools/builtin/files.py`：`workspace_read` / `workspace_list`（只读，路径逃逸防护）
- [x] `utils/text.py`：`html_to_text`（stdlib `HTMLParser`，剥掉标签与 script）
- [x] `utils/async_utils.py`：`retry_async`（tenacity，指数退避，只重试可重试的异常）
- [x] `config`：`tools.enabled_groups` / `tools.disabled` / 各工具参数
- [x] **副作用自动拦截**：`side_effects=True` 的工具默认需要人工确认，不必逐个登记
- [x] `registry.describe_groups()`：说明每个分组为什么在/不在工具列表里
- [x] `--no-confirm` 语义修正为「一律同意并留痕」，`--quiet` 保持「无人在场则拒绝」
- [x] 118 个新增测试，出网路径全部用 `httpx.MockTransport` 离线跑
- [x] 验收：真实模型 + 真实网络下，5 个工具全部跑通；白名单外域名与无人在场的写操作都被正确拒绝

**验收命令**

```powershell
uv run doudou tools list
uv run doudou ask "工作区里有哪些文件？"
uv run doudou ask "帮我记一条笔记：明天交周报" --no-confirm
uv run doudou ask "抓取 https://example.com 看看写了什么"   # 需先配白名单
```

> **与规划的一处偏差**：规划里 `http_request` 与 `notes` 都算「只读类」，但写笔记确实
> 有副作用。处理方式不是把它挪到 M8，而是引入「副作用声明 + 自动拦截」机制 ——
> 这样 M8 加写文件 / 执行命令时不必再建一套确认逻辑。
>
> 另外新增了一条规划外的策略：**依赖不满足的工具不注册**（而不是注册一个永远失败的）。
> 这让「工具列表 = 现在真的能用」成为不变式。


**验收命令**

```powershell
uv run doudou tools list
uv run doudou ask "工作区里有哪些文件？"
uv run doudou ask "帮我记一条笔记：明天交周报" --no-confirm
uv run doudou ask "抓取 https://example.com 看看写了什么"   # 需先配白名单
```

> **与规划的一处偏差**：规划里 `http_request` 与 `notes` 都算「只读类」，但写笔记确实
> 有副作用。处理方式不是把它挪到 M8，而是引入「副作用声明 + 自动拦截」机制 ——
> 这样 M8 加写文件 / 执行命令时不必再建一套确认逻辑。
>
> 另外新增了一条规划外的策略：**依赖不满足的工具不注册**（而不是注册一个永远失败的）。
> 这让「工具列表 = 现在真的能用」成为不变式。

---

### M4 — 飞书通道（WebSocket 长连接）✅

- [x] `channels/feishu/events.py`：事件解析 + `event_id` 幂等去重（TTL + 容量上限）
- [x] `channels/feishu/schemas.py`：`text` / `post` 消息内容解析（保留链接）
- [x] `channels/feishu/client.py`：lark SDK 封装（发卡、发文本、整卡 patch、机器人身份）
- [x] `channels/feishu/cards.py`：思考卡片 / 答案卡片 / 错误卡片 + 工具痕迹
- [x] `channels/feishu/channel.py`：长连接托管、卡片流式更新、失败降级为文本
- [x] `gateway/bus.py`：按会话串行、跨会话并发、积压拒绝
- [x] `gateway/router.py`：只依赖 `InboundMessage` + `EventSink`，对渲染方式无感知
- [x] `channels/base.py`：新增 `EventSink` 协议；`CliChannel` 也实现它
- [x] `channels/registry.py`：按配置启停常驻通道，单个失败不影响服务
- [x] 群聊 @ 策略（`mentioned_type` / `bot_open_id` / `bot_name` 三级判定）、白名单
- [x] 会话隔离：**同一个群共享一个上下文**（`isolate_by_thread: false`）
- [x] 68 个新增测试，全部离线（假 lark 客户端 + 假长连接）
- [x] 验收：真实启动 + 无效凭据下，服务照常可用且长连接失败**可见**

**与规划的两处偏差**

1. **只实现了 websocket 模式，`crypto.py` 与 `api/routes/webhooks.py` 未写。**
   长连接是本进程主动连出去、由 app_id/app_secret 鉴权，事件经 SDK 解析后交给我们 ——
   没有公网端点，也就没有「必须验签解密」这一层。为不需要的路径写加解密代码是负资产，
   因此留到真正需要公网回调时再补。
2. **卡片流式用「整卡 patch」而不是 cardkit 增量更新。**
   后者流量更省但要维护元素 id 与流式会话状态；整卡 patch 的 API 面小、行为可预测，
   配合节流已经足够像流式。增量更新列为后续优化。

**一个必须记下的坑**

`lark_oapi.ws.client` 在模块导入时就抓了 `asyncio.get_event_loop()` 存成模块级变量。
若 lark 已在主线程导入过，那个 loop 就是主事件循环，`start()` 里的
`run_until_complete` 会抛 `This event loop is already running` ——
**长连接根本连不上，而异常被线程吞掉，服务表面完全正常**。
处理方式见 `channels/feishu/channel.py::_run`（在专属线程里接管该模块级 loop）。


---

### M5 — MCP ✅

- [x] `mcp/config.py`：清单解析 + `${ENV}` 替换 + 严格校验（不碰网络）。
      变量未设置**直接报错**，而不是把 ``Bearer ${TOKEN}`` 原样发出去换一个 401
- [x] `mcp/loader.py`：声明 → 适配器连接字典；工具命名空间 `server__tool`
- [x] `mcp/client.py`：**逐个 server** 拉取，失败只影响它自己；带重试
- [x] `mcp/lifecycle.py`：启停、刷新、状态快照（对上层暴露唯一门面）
- [x] `defaults` 段继承，**显式写在 server 上的值优先**（依据 `model_fields_set`）
- [x] `read_only` 声明：第三方 server 默认按「有副作用」处理，声明后才免逐次确认
- [x] `include_tools` / `exclude_tools` 工具面收敛；两者冲突直接报错
- [x] `GET /api/v1/mcp`、`POST /api/v1/mcp/refresh`、`doudou mcp list|check`
- [x] 失败降级：`fail_fast: false`（默认）时单个 server 挂掉只告警
- [x] `/health` 汇总 MCP 状态（几个 server、几个工具、谁掉线了）
- [x] 60 个新增测试，含**真实 stdio + 真实 http** 的协议往返
- [x] 验收：真实模型正确选择了 MCP 工具；关掉一个 server 服务仍可用

**验收命令**

```powershell
uv run doudou mcp list
uv run doudou ask "用你能用的 MCP 工具把这句话原样回显给我：hello"
```

> **顺带修掉一个会立刻被撞上的不一致**：`GET /tools` 与 `doudou tools list`
> 原本是「从配置推导」的，因此**不会显示 MCP 工具** —— 而 Agent 明明有它们。
> 现在两者都描述 **Agent 实际装载的工具**（`tool_infos(tools=...)`），
> 分组与来源从工具自身的注解读取。
>
> **另一个必须记下的代价**：适配器的 MCP 工具是无状态的，
> **每次调用都会新建 session**；对 stdio 就是每次重新拉起子进程。
> 这决定了「频繁调用的能力要用 http transport」。持久 session 列为后续优化。

---

### M6 — Skill 渐进式加载 ✅

- [x] `skills/registry.py`：索引 + 按名/描述检索 + 附属文件访问（含路径逃逸防护）
- [x] 系统提示词**只注入索引**（name + description），正文不进上下文预算
- [x] `agent.prompts.render_skill_index`：索引**有界**（`skill_index_limit`），
      超出时说明「另有 X 个未列出」并明确告知「无关的技能不要读」
- [x] 三个工具：`read_skill` / `list_skill_files` / `skill_read_file`；无技能时**不注册**
- [x] 提示词与工具**共用同一份索引**（否则「列出的」与「能读到的」会对不上）
- [x] `GET /api/v1/skills`、`GET /api/v1/skills/{name}`（正文按需）、`POST /api/v1/skills/reload`
- [x] `doudou skills list|show|check|reload`
- [x] 52 个新增测试，覆盖索引、检索、附属文件防护、提示词注入与热重载
- [x] 验收：真实模型读索引 → 调 `read_skill("research")` → **按其步骤**继续执行；
      无关提问（「现在几点了」）只调 `now`，**不读任何技能**

**验收命令**

```powershell
uv run doudou skills list
uv run doudou ask "帮我做一次结构化调研：对比 X 与 Y" --no-confirm
uv run doudou ask "现在几点了？" --no-confirm   # 不应调用 read_skill
```

> **这里的热重载是真的，MCP 的不是。** `POST /skills/reload` 会就地更新系统提示词：
> 技能索引只是提示词里的一段文本，而 `read_skill` 在调用时才查索引，
> 所以换掉提示词 + 刷新索引就生效了。MCP 的工具列表是构建 Agent 时固定的，必须重启。
>
> **又一次印证了「依赖不满足就不注册」**：技能数量为 0 时不注册那三个工具，
> 不给模型一个永远失败的 `read_skill`。

---

### M7 — 上下文工程 ✅

- [x] `utils/tokens.py`：token 计数（tiktoken `cl100k_base`），**离线时回退字符启发式**并只告警一次
- [x] `agent/context.py`：`ContextManager` / `HistorySummarizer` / `ResultOffloader` / `trim_to_budget`
- [x] 按 **token 预算**裁剪（`context.max_input_tokens`），且不切断工具调用组
- [x] 摘要压缩：超阈值时把早期历史压成摘要，**原文不删**；摘要可增量复用
- [x] 大结果卸载：超过 `offload_threshold_bytes` 落盘，上下文里只留预览 + 路径
- [x] `agent/memory.py`：加载 `memory_files` 到提示词、支持助手回写（`remember` 工具有副作用）
- [x] `remember` 写回后**立刻**刷新系统提示词（回调机制，与 Skill 热重载同源）
- [x] `GET /api/v1/threads/{id}/context`：占用 / 预算 / 压缩明细 / 原文条数
- [x] `runner.run_finished` 日志带 `ctx_tokens`、`ctx_compressed`、`ctx_summarized`、`ctx_dropped`
- [x] 60 个新增测试，含 50 轮长会话的验收用例
- [x] 验收：真实模型下 12 轮把 29 条 / 2483 token 压到 875 token，
      压缩后**仍能概括此前聊过什么**；记忆在新会话里被准确复述

**验收命令**

```powershell
uv run doudou serve
curl -X POST http://127.0.0.1:8000/api/v1/chat -d '{"message":"你好"}' -H "Content-Type: application/json"
curl http://127.0.0.1:8000/api/v1/threads/{thread_id}/context
```

> **这一版修复了一个从 M4 潜伏至今的循环导入。**
> `gateway.router` → `channels.base`（触发包 `__init__`）→ `channels.registry`
> → `channels.feishu.channel` → `gateway.router` 成环，
> 表现是「导入顺序不同就报 ImportError」。两处修：`channels/__init__` 只导出协议
> （不再拖进 lark），`feishu.channel` 改用 `TYPE_CHECKING` 引用 `MessageRouter`。
> 顺带的好处：`import doudou.channels.base` 不再需要加载 lark。
>
> **同时修掉一个 flaky 测试**：`test_ask_stream_and_non_stream_agree` 比较两次
> 「现在几点」的完整输出，跨秒就会不同。改为断言答复结构而非字面相等。

---

### M8 — 子 Agent + 权限 + 完整人在回路 ✅

**目标**：让助手能做「危险」的事，同时不失控。

**交付物**

- [x] `tools/builtin/shell.py`：`shell_exec`，**默认禁用**，限定工作目录 + 超时 + 环境变量白名单
- [x] 写文件类工具（`workspace_write` / `workspace_edit` / `workspace_delete`），落在受控工作区
- [x] `agent/permissions.py`：路径权限规则（**首匹配生效**、自研 glob、`require_target` 兜底 containment）
- [x] `agent/subagents.py`：从 `agents.yaml` 生成子 Agent（独立 prompt / 工具白名单 / 步数上限）
- [x] `task` 工具：主 Agent 委派子任务，子 Agent 只回传结论（上下文隔离）
- [x] `agent/approvals.py`：`ApprovalBroker` —— 待决登记 + 超时按拒绝 + 决策复用
- [x] 确认状态落库：`approvals` 表 + `SqlApprovalStore`；`GET/POST /api/v1/approvals`
- [x] 飞书确认卡片回传决策（`register_p2_card_action_trigger` → broker → 卡片换结果态）
- [x] 越权测试：读 `.env` / 写工作区外路径 / 子 Agent 绕开确认都被钉住
- [x] 98 个新增测试（权限 glob 边界、写类工具、shell、子 Agent、确认中介、卡片）
- [x] 验收：真实模型下「写 workdir 文件 → 弹确认 → 同意后落盘」跑通；
      `.env` 的读写被规则拒绝；`shell_exec` 默认不在工具列表里

**命令**

```powershell
uv run doudou tools list                     # 看 shell_exec 在不在（默认不在）
uv run doudou ask "把结果写到 report.md"      # 会先弹确认
curl http://127.0.0.1:8000/api/v1/approvals  # 无人在场时从这里落决策
```

> **设计取舍：权限规则「无匹配即放行」。**
> 工具层已经有一道硬边界（`resolve()` 之后必须落在工作区内），权限规则是**在它之上**
> 再加细粒度限制。因此没有规则时放行；想「默认拒绝」就加一条
> `paths: ["/**"], mode: deny` 放在最后。为了让这条兜底不被绕过，
> `PermissionPolicy.require_target()` 把 containment 也当作规则 #0 判一次 ——
> 规则匹配本身区分不了区内外（两者都形如 `/a/b`）。
>
> **`/**/.env` 与 `/secrets/**` 两个 glob 边界是专门处理的。**
> 朴素翻译会得到 `/.*/\.env`（要求两层斜杠）→ **恰好漏掉最想拦的根目录 `.env`**；
> `/secrets/**` 也只匹配子项、漏掉目录本身。因此 `**` 在「前面紧跟 `/`」时按
> 「零或多个层级」处理。安全相关的匹配逻辑是靠测边界保证的，不是靠「看起来对」。
>
> **子 Agent 不能拿到 `task`**（`FORBIDDEN_TOOLS`），否则递归深度不可控。
> 另外它的确认策略必须**按子 Agent 自己的工具清单重算**：直接抄主 Agent 的
> `interrupt_on` 会让子 Agent 的写操作静默免确认 —— 这是写测试时才发现的真实绕过。
>
> **「跨重启不丢」的边界要说清**：落库的是**决策与审计记录**（同一个 `tool_call_id`
> 不再问第二次、`/approvals` 重启后仍能查），**不是**「被中断的运行会自己续跑」——
> 等待决策的协程随进程消失。`pending` 与 `expired` 都不算决策：把「还没问完」当成
> 答案会让重试静默跳过确认。超时一律按拒绝处理 —— 悬着的确认等于没有确认。
>
> **写工具命名避开 `read_file` / `write_file`**：M9 接 deepagents 的
> `FilesystemMiddleware` 会提供同名工具，而 `tools.registry` 对重名是「留前者、
> 丢后者」，静默失效比报错更难查。
>
> **又修掉同类循环导入**：`tools.registry` → `tools.builtin.delegation` → `agent.react`
> → `tools.decorators` →（包 `__init__`）→ `tools.registry`。三处修：
> `agent/__init__` 与 `tools/__init__` 只导出无跨包依赖的东西（仓库里**没有**任何地方
> 从这两个包根导入），`delegation` 用 `TYPE_CHECKING` + 调用时再 import `ReactAgent`，
> 确认策略则通过回调注入而不是反向 import。

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
| 提示词注入导致越权动作 | M8 引入写文件 / 执行命令后 | 工具层硬校验（resolve + containment）+ 权限规则 + 人工确认；`shell_exec` 默认关闭 |
| 确认请求无人应答 | M8 的写类工具 | 超时按拒绝（`approval_timeout_seconds`），不会把会话挂死；`pending` 不被当成决策 |
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
