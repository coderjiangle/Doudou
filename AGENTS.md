# AGENTS.md

本文件是 Doudou Agent 的**长期记忆**（deepagents `memory=` 参数读取的文件），
同时也可作为 AI 编码助手（Claude Code / OpenCode / Cursor 等）在本仓库工作时的约定说明。

---

## 项目定位

Doudou 是一个个人助手 Agent，内核是 `deepagents.create_deep_agent`。
它需要支持：自定义 Tool、MCP server、Agent Skill、飞书通道，以及 FastAPI HTTP 服务。

**当前状态：骨架阶段。** 大部分模块只有模块级 docstring 与 `# TODO` 标记，功能待实现。

## 分层与依赖方向

```
channels ─┐
api ──────┼──▶ gateway ──▶ agent ──▶ (tools / mcp / skills)
          │                     │
          └──▶ config          └──▶ storage / observability / security
```

**依赖只能自上而下**，禁止反向引用：

- `agent` **不得** import `channels` 或 `api`
- `gateway` **不得** import 具体通道实现（`channels.feishu` 等），只能依赖 `channels.base`
- `config` 不得 import 其他业务模块
- `RunEvent`（Agent 的输出）定义在 `agent/events.py`：它被上层消费，所以归 agent 层
- `InboundMessage` / `OutboundMessage`（通道的输入输出）定义在 `channels/base.py`

## 编码约定

- Python 3.12+，全量类型注解；`mypy --strict` 必须通过。
- 行宽 100，格式化与 lint 由 `ruff` 负责（`ruff format` + `ruff check --fix`）。
- 异步优先：I/O 一律 async；CPU 密集或阻塞调用用 `anyio.to_thread` 下沉。
- 配置读取**只**通过 `doudou.config.settings`，禁止散落 `os.environ`。
- 日志用 `structlog`（`doudou.observability.logging`），禁止裸 `print`（CLI 输出除外）。关键位置必须打印日志，包括出入参，敏感信息请模糊化处理。
- 异常：领域异常定义在模块内并继承统一基类；API 层由 `api.errors` 转成响应。
- 关键逻辑处请添加注释。

## 工具 / MCP / Skill 的约定

- **内置工具**放 `src/doudou/tools/builtin/`，每个模块一个主题，函数用 `@tool` 装饰并写清 docstring（LLM 依赖它理解用途）。
- **MCP** 只通过 `config/mcp.servers.yaml` 声明；代码里不出现硬编码的 server 地址。
- **Skill** 放仓库根 `skills/`，每个 Skill 一个目录 + `SKILL.md`（YAML frontmatter：`name`、`description`）。
- 有副作用的工具（写文件、发消息、调用外部写接口）需要在配置里登记为需人工确认（`interrupt_on`）。

## 测试

- `tests/unit/`：组件级测试，不依赖网络与真实模型，用 `doudou.agent.testing` 的确定性模型。
- `tests/integration/`：走公共入口的离线端到端测试（CLI / gateway），CI 会跑。
- 需要真实模型、网络或数据库的测试必须打 `@pytest.mark.integration`，默认被 `addopts` 排除，
  用 `make test-all` 显式启用。
- 新增模块时同步补测试。

## 提交规范

Conventional Commits：`feat|fix|docs|refactor|test|chore|build|ci(scope): 描述`。
每个 PR 需通过 `make lint` 与 `make test`。
