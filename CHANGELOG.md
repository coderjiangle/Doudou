# 变更记录

按日期记录功能变更，版本号见 `src/doudou/version.py`。

## 2026-10-27

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
