# 变更记录

按日期记录功能变更，版本号见 `src/doudou/version.py`。

## 2026-10-30

### 统计页：运行列表改为游标分页（单页大小可选）

- `GET /api/v1/traces` 增加**游标（keyset）分页**：按 `(started_at, run_id)` 取更早的记录，
  返回 `next_cursor`；`limit` 为单页大小（1–500）。翻页期间新产生的运行不会造成漏项或重复
  （`offset` 分页做不到）。游标格式非法返回 400。
- 统计页运行列表支持单页大小（20/50/100/200）与「加载更多」，并在筛选/刷新时重置分页。
- 统计页左上角换成与聊天页一致的品牌区（头像 + 名称），并补上 favicon。

### 定时任务

- 新增**定时任务**（`doudou.scheduler`）：在聊天里用 `schedule_create` / `schedule_list` /
  `schedule_cancel` 管理；到点由 `serve` 进程过一遍 LLM，把结果推到目标会话。
  - **任务不绑定会话**：只记投递目标（`channel` + `conversation_id`）。飞书群 → 该群，
    飞书私聊 → 该私聊，CLI / Web → 飞书私聊（触发时按 `channel_targets` 绑定解析）。
  - 触发时**没有可推送目标就跳过整次**（`last_status: skipped`，不跑模型、不推送）。
  - 触发的运行落在**目标会话的同一 thread** 上，消息并入上下文；触发消息以
    `[定时任务] <name>：<prompt>` 写入历史。
  - 时间表达支持 `at` / `every` / `cron`；cron 借 `apscheduler` 的 `CronTrigger` 算时间
    （不用它的调度器，避免 DB 与内存双份状态）。停机错过的触发点默认不补跑。
  - 新增表 `scheduled_tasks` / `channel_targets`，只读接口 `GET /api/v1/scheduled`；
    `MessageRouter` 增加 `on_inbound` 钩子用于记录「用户 → 私聊会话」绑定。
  - 新增配置段 `scheduler` 与文档 `docs/scheduler.md`。

### 删除会话改为彻底删除

- `DELETE /api/v1/threads/{id}` 现在会**连同该会话的审计 / 用量 / 轨迹 / 确认记录与
  渠道绑定一起删掉**（此前只删 `messages` 与 `threads`，观测数据会作为孤儿行留下）。
  响应新增 `purged` 字段回显各表删除行数；攒批写的缓冲区也会被清空，避免回写复活。
- 历史存储的 `clear()` 顺带清理渠道绑定，修掉「渠道会话仍指向已删 thread」的悬空绑定；
  内存实现的会话映射同样清理。

### 可观测性：轨迹落库 + 自建统计页（移除 Langfuse）

- **移除 Langfuse 追踪**：删掉 `observability/tracing.py`、`observability.tracing` 配置段、
  `[tracing]` 可选依赖与相关密钥/文档/测试。第三方上报不再是可选项。
- **新增运行轨迹落库**（`observability.traces`，默认开）：一次运行一行写 `run_traces` 表，
  步骤明细（每步推理文本 + 工具调用）以 JSON 存 `steps_detail`；带 `max_rows` 上限与
  `max_text_chars` 截断。复用审计/用量那套「SQL + 内存 + 攒批」三件套。
- **新增接口**：`GET /traces`、`GET /traces/{run_id}`、`GET /stats`
  （运行量/成功率/耗时、工具分布、确认比例、token 与费用趋势；均支持 `?thread_id=` 收敛到单会话）。
- **新增内置统计页** `GET /stats`（单文件、无构建、图表用内联 CSS，不依赖外部 CDN），
  聊天页右上角加「统计」入口；顶部可按「全部会话 / 具体会话」切换，点开单次运行可逐步回放。

## 2026-10-27

### 配置与部署修复

- 接入 `model.fallbacks`：主 Agent、摘要及子 Agent 按顺序尝试备用模型，已输出文本的
  流式请求不切换模型，避免混合回答。
- 子 Agent 的独立 `model` 与专属 `skills` 配置生效；技能读取继续遵守白名单与全局禁用项。
  `tools: []` 现在禁用全部工具，省略 `tools` 才继承，修正规划 Agent 意外拿到工具的问题。
- 生产启动统一单 worker，配置拒绝其它 worker 数；修正 K8s 的旧环境变量名。
- 镜像包含 PostgreSQL 驱动；Compose 允许 `.env` 的数据库地址覆盖默认 SQLite 地址。

### 变更

- **`context.max_input_tokens` 调到 `1000000`**（与 himodels 上 `deepseek-flash-official`
  的窗口对齐）：压缩阈值随之变成 70 万 token，实际几乎不再触发。代价是每轮输入 token（因而
  耗时与费用）随会话线性增长；另外注意**超窗时上游返回的是一句固定婉拒文案而不是报错**，
  所以预算必须与模型真实窗口对齐，否则表现为「助手忽然不肯回答」。
- **MCP server 清单并入 `config/config.yaml`**（`mcp.servers`），删掉独立的
  `config/mcp.servers.example.yaml`，`.env` 的 `DOUDOU_MCP_CONFIG_FILE` 与
  `Settings.mcp_config_path` 一并移除。清单结构现在由 `config/schema.py` 的
  `McpServerSpec` / `McpDefaults` 定义，**写错字段或 transport 与必填字段对不上会在启动前报错**
  （原先写在独立文件里，拼错只会「告警 + 静默不加载 MCP」）。
  `${ENV}` 占位仍在 `mcp/config.py` 里替换，变量缺失时依旧降级为「无 MCP」并告警。
  > 迁移：把原来 `mcp.servers.yaml` 里的 `servers:` 列表原样搬到 `config.yaml` 的
  > `mcp.servers` 下（`defaults:` 搬到 `mcp.defaults`），删掉 `version: 1` 那行即可。
- **助手记忆与项目约定拆成两个文件**：`remember` 的回写目标是新增的 `config/AGENTS.md`
  （含 `## 助手记录` 小节）；仓库根的 `AGENTS.md` 是给编码工具（Claude Code / OpenCode 等）
  的项目约定，**不再**注入助手提示词 —— 顺带省下每轮约 2 千字符的提示词开销。
  原先两者共用一个文件：运行期攒出来的个人偏好会混进项目规范，注入截断时还互相挤占额度。
  注意 `memory_files` 的路径按**进程工作目录**解析（与 `system_prompt_file`/`skills`
  的 config 目录相对不同），且只有**第一个**文件可回写。
- 移除 `.github/workflows/ci.yml` 与 `.vscode/*`：远端只有 Gitee，GitHub Actions 从来不会
  触发；lint / test 门禁继续走本地的 `make lint` / `make test`（见 `AGENTS.md` 的提交规范）。
- **移除 `context.summarize_after_messages`**（含配置项、环境变量与代码分支）：压缩不再按
  消息条数触发，**只看 token 预算**（`max_input_tokens × summarize_threshold_ratio`）。
  旧的按条数规则与预算无关，会让「内容很小、条数很多」的会话每轮都多出一次摘要模型往返
  （一次完整模型调用，且在关键路径上、对用户完全静默）。仍在配置里写它的需要删掉，
  否则会因为未知字段直接启动失败。
- **移除未使用的 `observability.langsmith` 与 `observability.otel`**：schema 字段
  （`LangSmithConfig` / `OtelConfig`）、`Secrets.langsmith_api_key`、`config*.yaml` 与
  `.env.example` 里的配置项、`deploy/k8s/configmap.yaml` 的环境变量、以及 `--extra otel`
  依赖组（连带 `uv.lock` 里只为它存在的 grpcio / exporter 包）全部删除。
  两者从来没有被任何代码读取过，留着只会让人以为「配了就能用」。
  **破坏性变更**：配置文件里仍写着 `langsmith:` / `otel:` 会因未知字段直接启动失败，删掉这两段即可。
  （追踪继续用 Langfuse：`uv sync --extra tracing` + `observability.tracing`。）

### 修复

- **Langfuse SDK 降到 2.x**（`langfuse>=2.60,<3`，原先 `>=3,<4`）：v3 SDK 走 OTEL 上报
  （`POST /api/public/otel/v1/traces`），只有 **v3 服务端**收；自建实例常见的是 2.95.x，
  那个端点不存在（404），表现为「key 配了、什么都没上传、也没有任何报错」——因为 SDK
  内部吞掉了上报错误。2.x SDK 走旧版 ingestion 接口，v2 / v3 服务端都能对接。
  `LangfuseTracer.emit` 随之从 v3 的 OTEL 风格（成对 `with`）改成 v2 的命令式
  （`trace()` → `span()` / `generation()` → `end()`）：
  - `release` / `environment` 改由 SDK 在构造客户端时注入（v2 的 `trace()` 不接受这两个参数）
  - trace 的 `output` 现在真的写上了最终答复（原先只在文档里写了）
  - 用量只能挂在 generation 上（v2 的 trace 没有 usage）→ 挂到**最后一个** generation，
    trace 级合计仍正确
  - 顺手修掉一个幽灵节点：`run_start` 的 `step=0` 以前会造出一个空的「step 0」span
- **日志时间戳改用 `runtime.timezone`**（原先写死 UTC）：UTC+8 的机器上日志看起来
  「比现实晚 8 小时」，排查时每次都要心算。现在输出带偏移量的 ISO
  （`2026-10-27T16:01:00.357372+08:00`），与 `now` 工具、墙钟一致；时区名写错只告警并
  回落到系统本地时区，不会让服务起不来。需要机器可读的统一时间时开 `runtime.log_json`。

## 2026-10-26

### 新增

- **英文版 README**（`README.en.md`）：中文版的结构 1:1 直译（29 个标题一一对应，表格 /
  代码块 / 图片 / 文档链接全保留），两版顶部互加语言切换入口。示例提问改成英文，
  `scripted:demo` 的关键词表含 `time` / `date` / `clock`，所以离线链路照样跑通。

### 变更

- **示例技能换成 `weather`**：删掉 `example-echo` 与 `research`，新增 `weather`
  （wttr.in 为主、Open-Meteo 兜底，两个服务都不需要密钥）；`skills/README.md` 的目录树
  同步更新，仓库自带技能的断言也跟着改（否则 `test_repo_skills_are_valid` /
  `test_repo_skills_are_indexed` 必然失败）。
- `config/config.yaml`：去掉火山方舟 Coding Plan 的过时注释，`model.base_url` 与
  `model.api_key` 留空待填，`tools.web_search.api_key` 补上占位空字段，飞书通道默认改回关闭。

### 文档

- CHANGELOG 改为**按日期归档**：把原来的「未发布」段落整理成 `## 2026-10-25`，
  与 10-24 / 10-23 两段保持一致；合并重叠条目，并补上当天漏记的一条修复
  （`tools.web_search.provider` 大小写不敏感）。

## 2026-10-25

### 新增

- **内置聊天页**（`GET /`，别名 `/ui`）：流式对话、会话列表与历史回放、模型与工具数展示、
  `?thread=` 深链到某个会话；回复按 **Markdown** 渲染，工具调用折叠成紧凑 chips；
  需要确认的工具弹出同意/拒绝卡片。单文件、无构建步骤、不依赖外部 CDN，
  支持 `DOUDOU_API_TOKEN` 与自定义服务地址。
- 只读工具 `my`（`meta` 分组）：按段落或点号路径查询当前配置（模型、步数、工具清单、
  服务地址、密钥是否已配置），密钥只报存在性、其余敏感值打码，不能修改任何配置。
- `shell_exec` 支持 Linux 沙箱：`tools.shell.sandbox: bwrap` 用 bubblewrap 把命令关进
  「只有工作区可写」的文件系统视图（父目录 tmpfs 遮罩、系统路径只读、网络不隔离）；
  `bwrap` 不可用或工作区在根目录时，`shell` 分组整组不注册并说明原因。
- `web_search` 支持 **DeepSeek 自研搜索**：`tools.web_search.provider: deepseek`，
  使用 `DOUDOU_TOOLS_WEB_SEARCH_API_KEY` 调 `POST /v1/tools/search`；结果优先取官方的
  `summary`，可选 `need_content` 返回正文。

### 变更

- **环境变量命名统一**：所有环境变量都是 `DOUDOU_` + `config.yaml` 路径（单下划线），
  例如 `DOUDOU_MODEL_PRIMARY`、`DOUDOU_MODEL_MAX_TOKENS`、`DOUDOU_CHANNELS_FEISHU_APP_SECRET`。
  旧的双下划线写法与无前缀厂商变量（`OPENAI_API_KEY` 等）不再读取；凭据也改成配置字段
  （`model.api_key`、`tools.web_search.api_key`、`channels.feishu.*`），仍约定只放 `.env`。
- 许可证改为 **MIT**（新增 `LICENSE`，`pyproject.toml` 去掉 `Private :: Do Not Upload`）。
- 跨平台适配：CLI 在 Windows 上不再因 GBK 控制台编码崩掉（终端切 UTF-8，管道只替换
  不可编码字符）；补齐 `scripts/*.sh`，文档命令同时给出 PowerShell 与 bash 版本；
  CI 增加 Windows 单元测试 job。
- 移除 `note_save` / `note_list` / `note_read` 三个工具（改用工作区文件工具写 Markdown）
  与 `days_between`（`clock` 分组只剩 `now`）；`tools.notes` 配置字段保留但不再生效，
  只为兼容旧配置文件。

### 修复

- 飞书：`bot/v3/info` 的响应体只在 `raw.content` 里，字段是 `bot.open_id`；原来的解析路径
  读 `response.data`（恒为 `None`），导致「自动获取 `bot_open_id`」从未生效，而且没有任何日志。
  现在解析原始 JSON，成功但取不到 `open_id` 时打 warning。
- 飞书：回复卡片不再回显用户原话；需要上下文的确认卡片仍保留提问内容。
- 飞书：卡片长度按飞书的真实限制处理 —— 硬上限 30 KB（错误码 `230025`），字符上限由 3000
  提到 8000，并以整卡序列化后的字节数（28 KB）为准收缩；截断提示只保留一处。
- `tools.web_search.provider` 大小写不敏感。

### 文档

- 修正飞书权限清单：私聊要 `im:message.p2p_msg:readonly`，群里 @ 要
  `im:message.group_at_msg:readonly`。只开 `im:message` 会「长连接正常但收不到消息」；
  新增对应排查顺序，并说明控制台的「验证长连接」只验证通道、不验证推送过滤。
- README：补豆豆的形象与取名由来、架构图换 PNG（附 SVG 源文件）、飞书对话与联网搜索效果图；
  用 CHANGELOG 取代里程碑路线图。

## 2026-10-24

### 新增

- 上下文工程：按 token 预算裁剪（不切断工具调用组）、早期历史摘要压缩、大工具结果落盘卸载、
  长期记忆加载与回写。
- 子 Agent：从 `config/agents.yaml` 派生，独立提示词、工具白名单与步数上限；`task` 工具只回传结论。
- 权限：工作区路径规则（首匹配生效）、写文件类工具、`shell_exec`（默认禁用）。
- 人工确认：`ApprovalBroker` 负责登记、超时按拒绝、决策复用与落库；飞书卡片按钮回传决策。
- 可观测性：审计日志（参数打码）、按天 × 会话的成本核算与日预算告警、可选 Langfuse 追踪。
- 安全：凭据扫描脚本接入预提交与 CI；补充容量评估与压测脚本。

## 2026-10-23

### 新增

- 核心：自研 ReAct 循环、工具注册表、会话映射、CLI 通道与终端侧人工确认。
- HTTP API：`/chat`、SSE 流式、threads 管理、Bearer 鉴权、结构化访问日志。
- 持久化：SQLite / PostgreSQL 存储消息、线程与渠道绑定，重启后可续同一会话。
- 内置工具：`web_search`、`http_request`（域名白名单 + 内网拒绝 + 仅 GET/HEAD）、
  只读工作区文件；`side_effects` 声明触发自动确认。
- 飞书通道：WebSocket 长连接、流式卡片、群聊 @ 判定、事件幂等去重。
- MCP：stdio / http / sse 接入，工具命名空间与单个 server 失败降级。
- Skill 渐进式加载：提示词只注入索引，正文按需通过 `read_skill` 读取，支持热重载。
