# 安全设计

## 1. 威胁面

| 面 | 风险 | 对策 |
| --- | --- | --- |
| HTTP API | 未授权调用、越权读他人会话 | `DOUDOU_API_TOKEN` 鉴权 + 令牌常量时间比较 |
| 飞书回调 | 伪造事件、重放 | 签名校验 + `event_id` 幂等去重 + 时间窗校验 |
| MCP server | 恶意 server 返回注入内容、危险工具 | 白名单 server、`include_tools` 收敛、`exclude_tools` 屏蔽破坏性工具 |
| 工具执行 | 越权读写文件、执行任意命令 | 文件系统权限规则 + `interrupt_on` 人工确认 + 沙箱 |
| 模型输出 | 提示词注入导致越权动作 | 工具层做硬校验，不信任模型输入 |
| 密钥 | 泄漏进日志、进仓库 | 只走 `.env`、日志脱敏、`detect-private-key` pre-commit |

## 2. API 鉴权

- `DOUDOU_API_TOKEN` 非空时，`/api/v1/**`（除 `/health`）要求 `Authorization: Bearer <token>`。
- 比较使用 `secrets.compare_digest`，避免时序侧信道。
- prod 环境建议在网关层再叠一层鉴权（mTLS / OAuth2），并限制 `cors_origins` 为明确域名。

## 3. 飞书回调安全

```
1) 存在 Encrypt Key  → 先 AES-CBC 解密 body
2) 校验签名/时间戳   → 拒绝超时与签名不符的请求
3) 校验 verification_token（配置期 challenge 除外）
4) event_id 去重（TTL = dedup_ttl_seconds）
5) 通过后才入队处理
```

- **prod 必须配置 `FEISHU_ENCRYPT_KEY`**，明文模式不适合生产。
- 回调地址必须是 HTTPS。
- 白名单：`allowed_chats` / `allowed_users` 非空即启用。

## 4. 文件系统与权限

deepagents 的文件系统权限按声明顺序**首匹配生效**，因此：

```yaml
permissions:
  # 显式拒绝写在最前
  - { operations: [read, write], paths: ["/**/.env", "/**/*.key", "/**/*.pem", "/**/secrets/**"], mode: deny }
  # 工作区放行
  - { operations: [read, write], paths: ["/workspace/**"], mode: allow }
  # 其余只读
  - { operations: [write], paths: ["/**"], mode: deny }
```

要点：

- 默认无匹配即**放行**，所以「只读兜底」必须显式写 deny。
- 子 Agent 需要更窄的权限时单独配置，不要指望继承。
- 沙箱后端的 `execute` **不受**权限规则约束，启用即等于放开 shell。

## 5. 人工确认（人在回路）

| 工具 | 是否确认 |
| --- | --- |
| 只读检索、读文件 | 否 |
| `write_file` / `edit_file`（限 `/workspace/`） | 建议是 |
| `delete`、`shell_exec`、对外发消息、提交代码 | **必须** |

配置方式：在 `config.yaml` 的 `agent.interrupt_on` 里登记工具名（`"*"` 表示全部）。

> **M1 现状**：确认回调已打通（CLI 会弹 `是否执行工具 xxx？`），但底层是终端询问而非
> LangGraph interrupt；M8 会把它换成「持久化确认 + 飞书卡片按钮」，
> 并由 `agent.permissions` 提供路径级兜底。
> **未提供回调时工具会被拒绝执行**，不会静默放行。

## 6. 密钥管理

- 单一入口：`security/secrets.py` 读取，其他模块禁止直接 `os.environ`。
- 日志与 `doudou config show` 输出自动脱敏（匹配 `*_KEY`、`*_SECRET`、`*_TOKEN`、`*_PASSWORD`）。
- 生产建议用容器 Secret / 云 KMS 注入环境变量，不要落盘明文。
- 轮换：`FEISHU_APP_SECRET`、`DOUDOU_API_TOKEN` 支持热更新（重启即生效，无需改代码）。

## 7. 审计

`storage` 中保留审计记录：

| 记录项 | 内容 |
| --- | --- |
| 谁 | 通道 + user_id |
| 何时 | 时间戳 |
| 做了什么 | 工具名 + 参数摘要（脱敏） |
| 结果 | 成功/失败 + 耗时 |
| 是否确认 | 是否经人工批准 |

审计日志不可被 Agent 修改，也不放入模型上下文。

## 8. 运行时加固

- 容器以非 root 用户运行（见 `Dockerfile`）。
- `shell_exec` 默认禁用；启用时限定工作目录并设置超时。
- `agent.recursion_limit` 防死循环；`mcp.call_timeout` 与模型 `timeout` 防挂死。
- 依赖安全：CI 中可加 `pip-audit`；容器镜像定期重建。
