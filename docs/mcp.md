# MCP 接入

> **状态：设计稿（M5 落地）。** 本章描述目标设计与配置格式，
> 其中 `doudou mcp ...` 命令与 `GET /api/v1/mcp` 接口尚未实现；
> 当前可用的工具相关命令是 `doudou tools list` / `doudou tools show <name>`。

## 1. 装配链路

```
config/mcp.servers.yaml
   │  mcp.config 解析 + ${ENV} 替换 + schema 校验
   ▼
mcp.client  ── MultiServerMCPClient（langchain-mcp-adapters）
   │           stdio / http / sse 三种 transport
   ▼
mcp.loader  ── get_tools() + include/exclude 过滤 + 名称前缀
   │           适配成 LangChain BaseTool
   ▼
tools.registry ── 与内置工具合并、重名检测、分组
   │
   ▼
agent.factory ── create_deep_agent(tools=[...])
```

MCP 工具和内置工具在 Agent 眼里没有区别，都是普通 LangChain 工具。

## 2. 清单格式

```yaml
servers:
  - name: filesystem              # 唯一名，用作工具前缀
    enabled: true
    transport: stdio              # stdio | http | sse
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "./data/workspace"]
    env:
      NODE_NO_WARNINGS: "1"
    include_tools: []             # 空 = 全部
    exclude_tools: [delete_path]
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
| `stdio` | 本地进程型 server（npx / uvx 启动） | `command` + `args` + `env` |
| `http` | 远程 Streamable HTTP 服务 | `url` + `headers` |
| `sse` | 只提供 SSE 的旧服务端 | `url` + `headers` |

远程服务鉴权统一走 `headers`，值支持 `${ENV_VAR}` 占位，从环境变量注入。

## 4. 工具面收敛（重要）

MCP server 常常暴露几十个工具，全部交给模型会导致：

- 系统提示词膨胀、token 成本上升；
- 模型选错工具的概率上升。

因此：

1. `include_tools` 白名单优先，明确只给需要的；
2. 危险工具（删除、DDL、支付类）进 `exclude_tools`；
3. `tool_name_prefix: true` 时工具名形如 `github__search_repositories`，避免多 server 撞名。

## 5. 生命周期与容错

| 行为 | 配置 | 说明 |
| --- | --- | --- |
| 启动时拉取工具 | `mcp.enabled` | false 时完全跳过 |
| 单 server 失败 | `mcp.fail_fast` | false（默认）只告警并降级，true 则启动失败 |
| 调用超时 | `mcp.call_timeout` | 单次工具调用超时 |
| 健康检查 | `mcp.lifecycle` | `/health` 中报告各 server 状态 |
| 热重载 | `POST /api/v1/mcp/refresh` | 重新拉取工具列表，无需重启 |

`mcp/lifecycle.py` 与应用 lifespan 绑定，保证 stdio 子进程被正确回收，
避免容器里留下僵尸进程。

## 6. 排查

```powershell
uv run doudou mcp list            # 列出 server 与拉取到的工具
uv run doudou mcp list --tools    # 打印工具入参 schema
uv run doudou mcp check github    # 单独探测某个 server
```

常见问题：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `stdio` server 无工具 | 命令不存在 / 缺依赖 | 手动跑一次 `command args` 验证 |
| 401 / 403 | token 未注入 | 检查 `.env` 变量名与 `${...}` 占位是否一致 |
| 工具名重复 | 多个 server 同名工具 | 打开 `tool_name_prefix` 或改用白名单 |
| 启动变慢 | 每个 stdio server 都要拉起进程 | 只启用需要的 server |
