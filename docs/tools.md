# 工具开发规范

## 1. 工具来源

| 来源 | 位置 | 状态 |
| --- | --- | --- |
| 内置工具 | `src/doudou/tools/builtin/` | ✅ `clock`（`now` / `days_between`） |
| MCP 工具 | `config/mcp.servers.yaml` | ⏳ M5，届时通过 `build_tools(extra=...)` 注入 |
| deepagents 内置 | 框架提供 | ⏳ M9（`ls` / `read_file` / `write_file` / `edit_file` / `delete` / `glob` / `grep` / `write_todos` / `task`） |
| 沙箱执行 | 沙箱后端 | ⏳ `execute`（shell），需显式启用 |

全部在 `tools/registry.py` 汇总后交给 Agent。M1 的 ReAct 循环收到的就是这里的工具列表。

## 2. 写一个内置工具

要点（不写实现，只定规范）：

1. 位于 `src/doudou/tools/builtin/<主题>.py`，一个模块一个主题。
2. 用 LangChain 的 `@tool` 装饰；函数名即工具名（蛇形小写）。
3. **docstring 是给模型看的**，必须写清：做什么、参数含义、返回值结构、何时不该用。
4. 参数用类型注解 + 默认值；复杂入参用 Pydantic 模型。
5. 返回字符串或可序列化结构；大结果要截断并说明截断（配合上下文卸载）。
6. 阻塞 I/O 用 async；同步 SDK 用 `anyio.to_thread.run_sync` 包一层。
7. 失败时**抛异常**并在消息里给出可操作的提示，不要返回 `"error"` 字符串让模型瞎猜。

### 命名与分组

| 前缀 | 用途 |
| --- | --- |
| `web_` | 联网检索（`web_search`、`web_fetch`） |
| `fs_` / 直接动词 | 文件操作 |
| `note_` | 备忘与待办 |
| `<平台>_` | 第三方平台（`github_create_issue`、`feishu_send_card`） |

## 3. 副作用与人工确认

把工具分成三档，在 `config/config.yaml` 的 `agent.interrupt_on` 里登记需要确认的：

| 档位 | 例子 | 策略 |
| --- | --- | --- |
| 只读 | 搜索、读文件、查库 | 直接执行 |
| 可逆写 | 写文件到 `/workspace/` | 记录日志，可选确认 |
| 不可逆 / 对外 | 删文件、发消息、提交代码、支付 | **必须** `interrupt_on` |

`interrupt_on` 生效需要 checkpointer（见 `agent/checkpointer.py`），
在飞书通道下确认请求会渲染成互动卡片按钮。

## 4. 注册与开关

- `registry.py` 负责：收集 → 过滤（按配置启用哪些分组）→ 重名检测 → 排序。
- 单个工具或分组可通过配置关闭（如 `tools.disabled: [shell_exec]`）。
- 需要 api key 的工具（`web_search`）在缺少 key 时应**自动不注册**，而不是运行时报错。

## 5. 测试

- `tests/unit/`：用 fake / respx 打桩外部依赖，验证入参校验与错误分支。
- 不要为工具写真实网络调用测试；放到 `tests/integration/` 并打 `@pytest.mark.integration`。
- 新增工具后补一条「工具清单快照测试」，防止工具面无意变化。

## 6. 清单

```powershell
uv run doudou tools list           # 名称 + 分组 + 来源 + 是否需要确认 + 说明
uv run doudou tools list --args    # 附带参数列表
uv run doudou tools show now       # 单个工具的完整信息（JSON）
```

也可通过 `GET /api/v1/tools` 查看（M2 落地）。

工具的 `group` 与 `source` 在 provider 处就确定（如 `group=clock`、`source=builtin:clock`），
MCP 工具会标成 `group=mcp`，无需事后反查。
