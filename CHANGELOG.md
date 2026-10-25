# 变更记录

按日期记录功能变更，版本号见 `src/doudou/version.py`。

## 未发布

### 变更

- 移除 `note_save` / `note_list` / `note_read` 三个工具。需要记东西时直接用工作区文件工具
  写 Markdown，模型能看到、人也能改。`tools.notes` 配置字段保留但不再生效，只为兼容旧配置文件。
- 移除 `days_between` 工具（`clock` 分组只剩 `now`）。日期差属于低频操作，
  需要时让模型算或写个脚本即可；同时从 `config/agents.yaml` 的子 Agent 白名单里去掉。

### 修复

- 飞书：`bot/v3/info` 的响应体只在 `raw.content` 里，字段是 `bot.open_id`；原来的解析路径
  读 `response.data`（恒为 `None`），导致「自动获取 `bot_open_id`」从未生效，而且没有任何日志。
  现在解析原始 JSON，成功但取不到 `open_id` 时打 warning。
- 飞书：回复卡片不再回显用户原话（卡片就紧跟在那条消息下面，回显看起来像被重复推送）。
  需要上下文的确认卡片仍然保留提问内容。
- 飞书：卡片长度按飞书的真实限制处理 —— 硬上限 30 KB（错误码 `230025`），字符上限由 3000
  提到 8000，并以整卡序列化后的字节数（28 KB）为准收缩；截断提示只保留一处。

### 文档

- 修正飞书权限清单：私聊要 `im:message.p2p_msg:readonly`，群里 @ 要
  `im:message.group_at_msg:readonly`。只开 `im:message` 会「长连接正常但收不到消息」。
- 新增「收不到消息」的排查顺序，并说明控制台的「验证长连接」只验证通道、不验证推送过滤。

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
