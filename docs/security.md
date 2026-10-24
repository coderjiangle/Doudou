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

### 4.1 硬边界：跑不出工作区

写类工具（`workspace_write` / `workspace_edit` / `workspace_delete`）与只读工具共用
同一道边界：路径 `resolve()` 之后必须落在 `filesystem.root_dir` 里。只检查字符串里有没有
`..` 是挡不住软链接与绝对路径的。

### 4.2 细粒度规则：`agent.permissions`

在硬边界**之上**再限制具体路径。规则按声明顺序**首匹配生效**：

```yaml
agent:
  permissions:
    # 显式拒绝写在最前
    - operations: [read, write]
      paths: ["/**/.env", "/**/*.key", "/**/*.pem", "/**/secrets/**"]
      mode: deny
    # 想「默认拒绝」就把这条放在最后（注意：放最后才有意义）
    # - operations: [write]
    #   paths: ["/**"]
    #   mode: deny
```

要点：

- 路径形态是**相对工作区、以 `/` 开头**的 POSIX 路径；`/**` 覆盖子目录，
  `/*.log` 只覆盖根目录。
- **无匹配即放行** —— 硬边界已经保证了最基本的收敛，权限规则是额外收紧。
  想只读就显式写一条 `paths: ["/**"], mode: deny`。
- 两个 glob 边界是专门处理的，朴素翻译会漏：`/**/.env` 必须能匹配根目录的 `/.env`；
  `/secrets/**` 必须能匹配 `secrets` 目录本身。
- `PermissionPolicy.require_target()` 把「在工作区内」作为规则 #0 再判一次（纵深防御）：
  规则匹配本身区分不了区内外，两者都形如 `/a/b`。
- 拒绝时抛 `PermissionDenied`，消息里带**命中规则的原文**：模型看到原因才可能换个做法，
  人看到才知道该改哪条配置。
- 子 Agent 目前继承同一份规则（它用的是同一个 `root_dir`）。要点是**别指望「继承主 Agent
  的确认策略」**：子 Agent 的工具集不同，确认策略必须按它自己的工具清单重算。

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
| 不可逆 / 对外 | 删文件、执行命令、发消息、提交代码 | 需要确认 + 权限规则兜底 |

工具用 `side_effects=True` 声明自己需要确认（见 `docs/tools.md` §2.1），
不必在 `agent.interrupt_on` 里逐个罗列。

### 6.1 确认动作可能来自哪里

「问出去」和「谁来答」被分开：Agent 只依赖 `agent/approvals.py` 的 `ApprovalBroker`，
各通道负责把决策送回来。理由是**它们本质上都是「等一个外部事件」**：

| 入口 | 行为 |
| --- | --- |
| `doudou chat` / `ask` | 终端询问「是否执行工具 xxx？」（就地回答） |
| `--no-confirm` | **一律同意**，并打 `cli.tool_auto_approved` 日志留痕 |
| `--quiet` | 无回调 → **拒绝** |
| 飞书 | 推一张带「同意 / 拒绝」按钮的卡片；点击 → broker → 卡片换成结果态 |
| HTTP API | `GET /api/v1/approvals` 看有没有待确认的，`POST /api/v1/approvals/{id}` 落决策 |

### 6.2 三条必须说清的边界

1. **超时按拒绝。** 默认 `agent.approval_timeout_seconds`（300s）内没人处理，
   就按「拒绝」收场。悬着的确认等于没有确认，默认必须偏向安全的一侧 ——
   而那次运行**不会**被挂在那里等。
2. **「跨重启不丢」指的是决策与审计，不是运行。** 落库的是
   `approvals` 表里的记录：同一个 `tool_call_id` 不会问第二次、重启后
   `/api/v1/approvals` 仍能查到「当时问了什么、谁同意了什么」。
   而**等待决策的协程随进程消失**，被中断的那次运行不会自己接着跑；
   重启后重试同一操作会重新问一次。
3. **`pending` 与 `expired` 都不算决策。** 只有 `approved` / `rejected` 会被复用。
   把「还没问完」当成答案，会让重试静默跳过确认 —— 那正是确认机制要防的事。

> **M3 曾经的状态**：副作用工具在 API 与 `--quiet` 下只能被拒绝。
> M8 把这条换成上面三条：确认可以来自飞书按钮或 HTTP，超时仍然落在「拒绝」这一侧。

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
- `shell_exec` **默认禁用**（`tools.shell.enabled: false`）。它是唯一没有约束机制的出口：
  路径权限规则对它无效（`rm -rf /` 不经过我们的路径解析），工作目录只是「起点的默认位置」。
  打开时能给的保护仅限：执行超时、输出上限、环境变量白名单（不把 `*_API_KEY` 透传给
  子进程）、以及人工确认。**需要真正隔离请用容器 / 沙箱，不要指望这个工具。**
- 子 Agent 拿不到 `task`（`FORBIDDEN_TOOLS`），递归深度因此有界；
  它的确认策略按**自己的**工具清单重算，不会继承主 Agent 而静默放宽。
- `agent.recursion_limit` 防死循环；`mcp.call_timeout` 与模型 `timeout` 防挂死。
- 依赖安全：CI 中可加 `pip-audit`；容器镜像定期重建。
