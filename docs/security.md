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

M3 的只读文件工具（`workspace_read` / `workspace_list`）已经把根目录钉在工作区，
路径会 `resolve()` 后校验，因此软链接也逃不出去。**路径级权限规则**（M8）在此基础上
提供更细的读写矩阵。

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

## 5. 工具层：默认拒绝

工具是「模型能做的动作」的边界，因此默认值一律偏向拒绝：

| 面 | 策略 |
| --- | --- |
| 出站 HTTP | `tools.http.allowed_domains` 为空 → 工具**不注册**；允许列表 + 内网 IP 拒绝 + 仅 GET/HEAD + 大小与超时上限 |
| 联网检索 | 缺少 `TAVILY_API_KEY` → 工具不注册 |
| 文件读取 | 根目录固定为工作区，路径 `resolve()` 后必须在根目录内（软链接也拦）；超大文件读之前就拒 |
| 笔记写入 | 声明 `side_effects=True` → 默认需要人工确认；文件名安全化，`..` 与绝对路径无法穿越 |
| 副作用工具 | 无确认回调时**拒绝执行**，而不是静默放行 |

**为什么 `http_request` 要额外校验解析出的 IP**：允许列表只约束域名，
而 `docs.internal.example.com` 完全可以解析到 `10.0.0.1` —— 那是经典的 SSRF 绕过。
因此域名通过后还要 `getaddrinfo` 一次，任何内网/回环/保留地址一律拒绝。

## 6. 人工确认（人在回路）

| 档位 | 例子 | 默认 |
| --- | --- | --- |
| 只读 | 检索、抓取、读文件、查时间 | 直接执行 |
| 可逆写 | 写笔记、写工作区文件 | 需要确认 |
| 不可逆 / 对外 | 删文件、执行命令、发消息、提交代码 | 需要确认（M8 补权限规则兜底） |

工具用 `side_effects=True` 声明自己需要确认（见 `docs/tools.md` §2.1），
不必在 `agent.interrupt_on` 里逐个罗列。

各入口的确认语义：

| 入口 | 行为 |
| --- | --- |
| `doudou chat` / `ask` | 终端询问「是否执行工具 xxx？」 |
| `--no-confirm` | **一律同意**，并打 `cli.tool_auto_approved` 日志留痕 |
| `--quiet` | 无回调 → **拒绝** |
| HTTP API | 无回调 → **拒绝**（M8 补持久化确认与飞书卡片按钮） |

> **M3 现状**：副作用工具在 API 与 `--quiet` 下无法执行。这是刻意的安全默认 ——
> 没有征求同意的通道，就不该替用户写下东西。要放开请显式配置
> `agent.interrupt_on: {note_save: false}`，或整体关掉
> `agent.require_approval_for_side_effects`。
> M8 会把它换成「持久化确认 + 卡片按钮」，并由 `agent.permissions` 提供路径级兜底。

## 7. 密钥管理

- 单一入口：`security/secrets.py` 读取，其他模块禁止直接 `os.environ`。
- 日志与 `doudou config show` 输出自动脱敏（匹配 `*_KEY`、`*_SECRET`、`*_TOKEN`、`*_PASSWORD`）。
- 生产建议用容器 Secret / 云 KMS 注入环境变量，不要落盘明文。
- 轮换：`FEISHU_APP_SECRET`、`DOUDOU_API_TOKEN` 支持热更新（重启即生效，无需改代码）。

## 8. 审计

`storage` 中保留审计记录：

| 记录项 | 内容 |
| --- | --- |
| 谁 | 通道 + user_id |
| 何时 | 时间戳 |
| 做了什么 | 工具名 + 参数摘要（脱敏） |
| 结果 | 成功/失败 + 耗时 |
| 是否确认 | 是否经人工批准 |

审计日志不可被 Agent 修改，也不放入模型上下文。

## 9. 运行时加固

- 容器以非 root 用户运行（见 `Dockerfile`）。
- `shell_exec` 默认禁用；启用时限定工作目录并设置超时。
- `agent.recursion_limit` 防死循环；`mcp.call_timeout` 与模型 `timeout` 防挂死。
- 依赖安全：CI 中可加 `pip-audit`；容器镜像定期重建。
