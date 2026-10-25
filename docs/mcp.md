# MCP 接入

支持 `stdio` / `http` / `sse` 三种 transport，
启动时拉取工具、按白/黑名单收敛工具面、单个 server 失败可降级。

## 1. 装配链路

```
config/mcp.servers.yaml
   │  mcp.config  ── ${ENV} 替换 + 严格校验（不碰网络）
   ▼
mcp.loader  ── 声明 → 适配器的连接字典；工具命名空间 server__tool
   ▼
mcp.client  ── 包一层 MultiServerMCPClient，**逐个 server** 拉取并记录状态
   ▼
tools.registry ── 与内置工具合并、重名检测、确认策略计算
   ▼
agent.factory ── build_agent(extra_tools=mcp_tools)
```

MCP 工具和内置工具在 Agent 眼里没有区别，都是普通 LangChain 工具。

## 2. 清单格式

```yaml
version: 1
defaults:            # 可被各 server 继承；显式写在 server 上的值优先
  enabled: true
  timeout: 60
  retries: 1
  read_only: false

servers:
  - name: filesystem            # 唯一名，会成为工具前缀（只允许字母数字下划线连字符）
    transport: stdio            # stdio | http | sse
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "./data/workspace"]
    env:
      NODE_NO_WARNINGS: "1"
    include_tools: []           # 白名单：非空时只暴露这些
    exclude_tools: [delete_path] # 黑名单：屏蔽危险工具
    read_only: false            # 见 §4
    description: 本地文件读写

  - name: github
    transport: http
    url: https://api.githubcopilot.com/mcp/
    headers:
      Authorization: "Bearer ${GITHUB_TOKEN}"
    include_tools: [search_repositories, get_file_contents]
```

字段含义见 `config/mcp.servers.example.yaml`。

## 3. transport 选择

| transport | 何时用 | 关键字段 |
| --- | --- | --- |
| `stdio` | 本地进程型 server（自己写的 Python server、`npx`/`uvx` 启动的） | `command` + `args` + `env` |
| `http` | 远程 Streamable HTTP 服务 | `url` + `headers` |
| `sse` | 只提供 SSE 的旧服务端 | `url` + `headers` |

## 4. ⚠️ 一个必须知道的代价：每次调用都重新建 session

`langchain-mcp-adapters` 的 `get_tools()` 返回的工具是**无状态**的，
**每次工具调用都会新建一个 MCP session**（这是其文档里明确写的）。

对 `stdio` 而言，这等于**每次调用都重新拉起一个子进程**：

- 自己写的 Python server：百毫秒级，可以接受；
- `npx` / `uvx` 那类要先解析包再启动 node/python 的：单次调用会到秒级；
- 需要频繁调用的能力，优先选 `http`（远端长驻进程，没有启动成本）。

持久 session 是后续优化项：它需要管理会话生命周期、重连与并发安全，
收益与复杂度都明显更高，暂不引入。

## 5. 自动的确认策略（安全默认）

**第三方 server 的工具默认按「有副作用」处理** —— 每次调用都要人工确认。

理由：一个 MCP server 里既可能有 `search` 也可能有 `delete`，而我们无法自动判断。
猜错的方向必须是「多问一次」，而不是「默默删了」。

确认它确实只读之后，在这个 server 上写 `read_only: true` 即可免除逐次确认。
（注意：在飞书通道里目前还不能确认，见 `docs/security.md`。）

## 6. 工具面收敛（重要）

MCP server 常常暴露几十个工具，全部交给模型会导致提示词膨胀、token 成本上升、
模型选错工具的概率上升。因此：

1. `include_tools` 白名单优先，明确只给需要的；
2. 危险工具（删除、DDL、支付类）进 `exclude_tools`；
3. `mcp.tool_name_prefix: true`（默认）时工具名形如 `github__search_repositories`，
   避免多 server 撞名；
4. 两个列表都写了同一个工具会**直接报错** —— 白名单优先，这样写只会造成困惑。

从 MCP 拿到的结果会被**拍平成纯文本**（见 `utils/text.blanks_to_text`）。
适配器默认交出来的是 `[{"type": "text", "text": "...", "id": "lc_..."}]`，
这层包装既白花 token，其中的内部 id 还会诱导模型把它当成有效引用。

## 7. 生命周期与容错

| 行为 | 配置 | 说明 |
| --- | --- | --- |
| 总开关 | `mcp.enabled` | false 时完全不拉取 |
| 单 server 失败 | `mcp.fail_fast` | false（默认）只告警并降级，true 则启动失败 |
| 拉取重试 | `defaults.retries` / `servers[].retries` | 冷启动首次握手失败很常见 |
| 调用超时 | `defaults.timeout` | 同时用作工具的超时（经注解传给 Agent） |
| 检视 | `GET /api/v1/mcp` | 每个 server 的状态、工具、被过滤项、耗时 |
| 刷新 | `POST /api/v1/mcp/refresh` | 重新拉取，**无需重启** |

**清单本身不合法时按「没有 MCP」处理并记 error 日志** ——
一个 YAML 拼写错误不该让整个服务起不来，但必须显式告警，
否则用户会以为 MCP 生效了。

> **刷新的边界**：`POST /mcp/refresh` 只更新检视结果，
> **Agent 的工具列表在启动时就固定了**。要真正生效需重启服务。
> 这点在响应里如实说明，避免用户以为刷新完就能用上新工具。

## 8. 排查

```powershell
uv run doudou mcp list             # 每个 server 的状态、工具数、被过滤项
uv run doudou mcp check github     # 单独探测一个 server，并打印服务端信息
uv run doudou tools list           # Agent 实际装载的工具（含 MCP）
uv run doudou tools list --no-mcp  # 只看内置，不连 MCP
```

常见问题：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `stdio` server 无工具 | 命令不存在 / 缺依赖 | 手动跑一次 `command args` 验证 |
| 启动时 `MCP 清单引用了未设置的环境变量` | `.env` 里缺变量 | 补上，或从清单里删掉该项 |
| 401 / 403 | token 未注入或已失效 | 检查 `${...}` 占位与 `.env` 变量名是否一致 |
| 工具名重复 | 多个 server 同名工具 | 打开 `tool_name_prefix` 或改用白名单 |
| 每次调用都要确认 | server 未声明只读 | 确认后加 `read_only: true` |
| 单次调用很慢 | 无状态 session 反复拉起子进程 | 换 `http` transport（见 §4） |
| 启动变慢 | 每个 stdio server 都要拉起进程 | 只启用需要的 server |
