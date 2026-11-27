# 安全设计

## 1. 威胁面

| 面 | 风险 | 对策 |
| --- | --- | --- |
| HTTP API | 未授权调用、越权读他人会话 | `DOUDOU_API_TOKEN` 鉴权 + 令牌常量时间比较 |
| 飞书通道 | 伪造建连、重复事件、越权访问 | App ID / App Secret 主动长连接鉴权 + `event_id` 幂等去重 + `allowed_chats` / `allowed_users` 白名单 |
| MCP server | 恶意 server 返回注入内容、危险工具 | 白名单 server、`include_tools` 收敛、`exclude_tools` 屏蔽破坏性工具 |
| 工具执行 | 越权读写文件、执行任意命令 | 文件系统权限规则 + `interrupt_on` 人工确认 + 沙箱 |
| 模型输出 | 提示词注入导致越权动作 | 工具层做硬校验，不信任模型输入 |
| 密钥 | 泄漏进日志、进仓库 | 只走 `.env`、日志脱敏、`detect-private-key` pre-commit |

## 2. API 鉴权

- `DOUDOU_API_TOKEN` 非空时，`/api/v1/**`（除 `/health`）要求 `Authorization: Bearer <token>`。
- 比较使用 `secrets.compare_digest`，避免时序侧信道。
- prod 环境建议在网关层再叠一层鉴权（mTLS / OAuth2），并限制 `cors_origins` 为明确域名。

## 3. 飞书通道安全

当前飞书接入**只实现 WebSocket 长连接**（`FeishuWebSocketRunner` / `lark_oapi.ws.Client`），
**不暴露公网 HTTP 回调端点**：

1. **建连鉴权**：由本进程持 `DOUDOU_CHANNELS_FEISHU_APP_ID` 与 `DOUDOU_CHANNELS_FEISHU_APP_SECRET`
   主动向飞书开放平台建立长连接；不开放 HTTP 事件回调入口，因此没有回调验签、`Verification Token`
   与 `Encrypt Key` AES-CBC 解密环节。
2. **入站过滤与防环**：丢弃机器人自身回流消息（`sender_type == "app"`）；`allowed_chats` /
   `allowed_users` 非空即启用白名单校验；群聊默认要求显式 `@`（`group_require_at: true`）。
3. **幂等去重**：`EventDedup` 按 `event_id` + `dedup_ttl_seconds`（默认 600s，容量上限 4096）去重，
   防止重连或网络抖动时同一条消息被重复执行。
4. **确认按钮粒度**：人工确认卡片只绑定单次调用的 `approval_id`，由 `ApprovalBroker`
   校验状态与超时（超时按拒绝），不会把一次确认放宽为整类工具豁免。

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
| 联网检索 | 缺少 `DOUDOU_TOOLS_WEB_SEARCH_API_KEY` → 工具不注册 |
| 文件读取 | 根目录固定为工作区，路径 `resolve()` 后必须在根目录内（软链接也拦）；超大文件读之前就拒 |
| 文件写入 | 声明 `side_effects=True` → 默认需要人工确认；路径同时受工作区边界与权限规则约束 |
| 副作用工具 | 无确认回调时**拒绝执行**，而不是静默放行 |

**为什么 `http_request` 要额外校验解析出的 IP**：允许列表只约束域名，
而 `docs.internal.example.com` 完全可以解析到 `10.0.0.1` —— 那是经典的 SSRF 绕过。
因此域名通过后还要 `getaddrinfo` 一次，任何内网/回环/保留地址一律拒绝。

## 6. 人工确认（人在回路）

| 档位 | 例子 | 默认 |
| --- | --- | --- |
| 只读 | 检索、抓取、读文件、查时间 | 直接执行 |
| 可逆写 | 写工作区文件、改文件内容 | 需要确认 |
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

> 早期版本里，副作用工具在 API 与 `--quiet` 下只能被拒绝。现在确认可以来自飞书按钮或
> HTTP，超时仍然落在「拒绝」这一侧。

## 7. 密钥管理

- 单一入口：`security/secrets.py` 读取，其他模块禁止直接 `os.environ`。
- 日志与 `doudou config show` 输出自动脱敏（匹配 `*_KEY`、`*_SECRET`、`*_TOKEN`、`*_PASSWORD`）。
- 生产建议用容器 Secret / 云 KMS 注入环境变量，不要落盘明文。

### 7.1 轮换流程

密钥轮换是**一次性动作**，成本远低于事后清理历史。触发时机：
怀疑泄漏（提交进了 git、贴进了聊天记录）、人员变动、定期到期。

| 密钥 | 怎么轮换 | 生效方式 |
| --- | --- | --- |
| 模型网关（`DOUDOU_MODEL_API_KEY`） | 在网关后台新建 key → 改环境变量 → **确认能跑通**再吊销旧 key | 重启进程 |
| `DOUDOU_CHANNELS_FEISHU_APP_SECRET` | 飞书开放平台重置 → 改环境变量 | 重启进程（长连接需重连） |
| `DOUDOU_API_TOKEN` | 直接换一个新随机串，同时通知调用方 | 重启进程 |
| `DOUDOU_TOOLS_WEB_SEARCH_API_KEY` | 对应平台后台重建（取决于 `web_search.provider`） | 重启进程 |

顺序很重要：**先让新 key 生效并验证，再吊销旧的**。反过来会出现一段
「配置是新的、凭据是废的」的窗口，表现是「服务在跑但每个请求都 401」。

验证方式：`uv run doudou config check`（校验配置与凭据是否存在）+
一次真实调用（`uv run doudou ask "现在几点？"`）。

**吊销旧 key 之后**再检查有没有历史泄漏：`git log -p -- . | grep -i "sk-"` 之类。
本仓库的 `.gitignore` 已经覆盖 `.env` / `data/`，
但**已经把密钥提交进去的历史不会因此消失** —— 该轮换就得轮换，
不改写历史也能靠轮换把风险关掉。

## 8. 审计

审计落在 `audit_events` 表（见 `docs/observability.md` §2），`GET /api/v1/audit` 可查：

| 记录项 | 内容 |
| --- | --- |
| 谁 | 通道 + `user_id` + 会话（`thread_id` / `conversation_id`） |
| 何时 | `created_at`（请求时）/ `updated_at`（出结果时） |
| 做了什么 | 工具名 + 参数摘要（**先打码再截断**，`args_truncated` 说明截了多少） |
| 结果 | 成功/失败 + 耗时 + 结果开头一段（`result_chars` 是全长） |
| 是否确认 | `approved` 三态：`true` / `false`（被拒） / `null`（该工具不需要确认） |

三条设计约束：

- **Agent 改不了它**：没有写接口，只能由运行产生；
- **不进模型上下文**：审计是给人看的，塞进上下文只会污染对话；
- **密钥不可能进表**：`AuditEntry.__post_init__` 里强制打码，
  这样「审计含明文密钥」不是「记得调那个函数」的问题，而是构造就做不到。

保留期：审计由 `observability.audit.max_rows` 封顶（超出按时间清理）；
另外 `DELETE /api/v1/threads/{id}` 会**连同该会话的审计一起删掉** —— 审计绑定在会话
生命周期上，不是永久留痕。需要不可删的合规留痕时，请自行把 `GET /api/v1/audit`
导出到外部存储（见 `docs/observability.md` §6）。

## 9. 质量门禁、依赖与镜像

> 仓库未配置 GitHub Actions 等云端 CI；静态检查、离线测试与安全扫描均通过本地 `Makefile`
> （或 Windows 下的 `scripts/*.ps1`）执行。

| 检查项 | 本地命令 | 说明 |
| --- | --- | --- |
| 代码规范与类型 | `make lint`（`ruff` + `mypy`） | 提交前必须通过 |
| 凭据泄漏扫描 | `make secrets`（`scripts/check_secrets.py`） | 防止明文密钥进入版本库 |
| 离线测试套件 | `make test`（`tests/unit` + `tests/integration`） | 提交前必须通过 |
| 依赖漏洞 | `make audit`（`pip-audit`，只审**运行时**依赖） | 按需 / 发布前手工执行 |
| 镜像漏洞 | `make scan`（`trivy`，HIGH/CRITICAL 即失败） | 构建镜像后手工扫描 |
| 依赖更新 | `make lock`（`uv lock --upgrade`）+ 跑全量测试 | 更新锁定版本后回归 |

`make audit` 只审运行时依赖是有意的：dev 工具不进生产镜像，它们的告警没有处置路径，
只会训练人忽略告警。上游未修的漏洞要用 `--ignore-vuln` 显式登记**并写清原因**，
否则检查会慢慢变成装饰品。

## 10. 运行时加固

- 容器以非 root 用户运行（见 `Dockerfile`）。
- `shell_exec` **默认禁用**（`tools.shell.enabled: false`）。它是唯一没有约束机制的出口：
  路径权限规则对它无效（`rm -rf /` 不经过我们的路径解析），工作目录只是「起点的默认位置」。
  打开时能给的保护仅限：执行超时、输出上限、环境变量白名单（不把 `*_API_KEY` 透传给
  子进程）、以及人工确认。
- Linux 上可以再打开 `tools.shell.sandbox: bwrap`：命令被关进 bubblewrap 沙箱，只有工作区
  可写，`config/`、`.env`、数据库都不可见。它**只隔离文件系统、不隔离网络**，也不替代
  容器；容器内通常还需要额外的 namespace 权限。`bwrap` 不存在时 `shell` 分组整组不注册。
- 子 Agent 拿不到 `task`（`FORBIDDEN_TOOLS`），递归深度因此有界；
  它的确认策略按**自己的**工具清单重算，不会继承主 Agent 而静默放宽。
- `agent.max_steps` 防死循环；`mcp.call_timeout` 与模型 `timeout` 防挂死。
- 依赖与镜像安全：发布前运行 `make audit` 与 `make scan`；容器镜像定期重建。
