# 变更记录

按日期记录功能变更，版本号见 `src/doudou/version.py`。

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
