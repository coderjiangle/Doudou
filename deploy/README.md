# deploy/

部署相关文件。

| 目录 | 内容 |
| --- | --- |
| `k8s/` | Kubernetes 部署清单（Deployment / Service / ConfigMap / Secret 模板） |

镜像构建见仓库根 `Dockerfile`，本地编排见根 `docker-compose.yml`。

## 生产部署要点

1. **密钥**：`FEISHU_APP_SECRET`、`DOUDOU_API_TOKEN`、模型 API Key 通过 Secret 注入，不要写进 ConfigMap。
2. **配置**：`config/config.yaml` 与 `config/mcp.servers.yaml` 用 ConfigMap 挂载到 `/app/config`（只读）。
3. **Skill**：`skills/` 用 ConfigMap 或镜像内置挂载到 `/app/skills`。
4. **持久化**：工作区与数据库挂 PVC 到 `/data`；数据库改用 Postgres 时把 `DOUDOU_DATABASE_URL` 换成 `asyncpg`。
5. **探针**：readiness/liveness 用 `/api/v1/health`（应实现为不触发模型调用的轻量检查）。
6. **副本数**：状态在进程内（消息总线、会话映射），多副本前需先把会话与队列下沉到外部存储。
7. **超时**：飞书回调需 3 秒内响应，Ingress/网关的读超时应 ≥ 流式接口所需时长，SSE 接口需关闭缓冲。
