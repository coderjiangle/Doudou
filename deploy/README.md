# deploy/

部署相关文件。

| 目录 | 内容 |
| --- | --- |
| `k8s/` | Kubernetes 部署清单（Deployment / Service / ConfigMap / Secret 模板） |

镜像构建见仓库根 `Dockerfile`，本地编排见根 `docker-compose.yml`。

## 生产部署要点

1. **密钥**：`DOUDOU_CHANNELS_FEISHU_APP_SECRET`、`DOUDOU_API_TOKEN`、模型 API Key 通过 Secret 注入，不要写进 ConfigMap。
2. **配置**：`config/config.yaml` 用 ConfigMap 挂载到 `/app/config`（只读）；
   镜像与 Compose/K8s 已设置 `DOUDOU_CONFIG_DIR=/app/config`。本地默认目录是
   `~/doudou`；MCP server 清单也在配置里（`mcp.servers`），不需要额外文件。
3. **Skill**：`skills/` 用 ConfigMap 或镜像内置挂载到 `/app/skills`。
   镜像默认安装 `feishu` / `anthropic` / `postgres` extras；如需语音或浏览器，
   在 `Dockerfile` 的 `uv sync` 命令追加 `--extra voice` / `--extra browser`。
4. **持久化**：工作区与数据库挂 PVC 到 `/data`；数据库改用 Postgres 时把 `DOUDOU_STORAGE_DATABASE_URL` 换成 `asyncpg`。
5. **探针**：liveness 用 `/api/v1/health`，readiness 用 `/api/v1/health/ready`；均不调用模型。
6. **进程与副本数**：保持 **1 worker、1 副本**。历史与会话绑定可落库，但会话锁、消息总线、
   待确认请求在进程内。更换 Postgres 也不能解决跨进程协调问题；多副本需先实现分布式锁、
   队列及确认路由。`server.workers` 只接受 `1`，`doudou serve` 显式把它传给 uvicorn。
7. **超时**：飞书回调需 3 秒内响应，Ingress/网关的读超时应 ≥ 流式接口所需时长，SSE 接口需关闭缓冲。

生产入口使用 `make serve-prod` 或 `uv run doudou serve --host 0.0.0.0 --port 8000`。
不要另用 `uvicorn --workers N` 启动多个进程。Docker / Compose / K8s 模板都按单进程部署，
并关闭 `runtime.debug`。环境变量名统一对应配置路径，例如 `DOUDOU_RUNTIME_ENV`、
`DOUDOU_SERVER_HOST`、`DOUDOU_FILESYSTEM_ROOT_DIR`、`DOUDOU_STORAGE_DATABASE_URL`。

Compose 默认使用持久卷 `/data/doudou.db`。启用 PostgreSQL 时，在 `.env` 设置
`DOUDOU_STORAGE_DATABASE_URL=postgresql+asyncpg://doudou:<密码>@postgres:5432/doudou`，
再运行 `docker compose --profile postgres up -d --build`；数据库密码与 postgres 服务配置需一致。
镜像包含 PostgreSQL 驱动，Compose 会使用 `.env` 中的地址而不会强制覆盖成 SQLite。

`config/` 为只读挂载，启用 `remember` 写回时应把第一个 `agent.memory_files` 指向 `/data`
中的可写文件（例如 `/data/AGENTS.md`），并按需预置初始内容。
