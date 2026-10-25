# config/

声明式配置目录。**可以提交到仓库**，但真实密钥一律走 `.env`。

| 文件 | 是否提交 | 说明 |
| --- | --- | --- |
| `config.example.yaml` | ✅ | 主配置模板，复制为 `config.yaml` 使用 |
| `config.yaml` | ❌（可选提交） | 实际生效配置；本地差异建议放 `config.local.yaml` |
| `config.local.yaml` | ❌ | 本地覆盖，最后合并，优先级最高（仅次于环境变量） |
| `mcp.servers.example.yaml` | ✅ | MCP server 清单模板 |
| `mcp.servers.yaml` | ❌ | 实际 MCP 清单（可能含 server 地址与 token 占位） |
| `agents.yaml` | ✅ | 主 Agent 附加指令 + 子 Agent 定义 |
| `prompts/` | ✅ | 提示词文件，被 `agent.system_prompt_file` 引用 |

## 加载与合并顺序

```
代码内默认值  →  config/config.yaml  →  config/config.local.yaml  →  环境变量(.env)
                                                    优先级：右 > 左
```

实现位置：`src/doudou/config/loader.py`；环境变量统一为
`DOUDOU_` + 配置路径（下划线连接），例如 `DOUDOU_SERVER_PORT=8080`、
`DOUDOU_MODEL_MAX_TOKENS=4096`。

## 校验

启动时会用 `src/doudou/config/schema.py` 的 Pydantic 模型做严格校验，
字段缺失或类型不符会直接启动失败（fail fast），并打印可读的错误定位。

```bash
uv run doudou config show    # 打印合并后的生效配置（密钥自动脱敏）
uv run doudou config check   # 仅校验，不启动服务
```
