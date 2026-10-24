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
- **包 `__init__.py` 必须保持轻量**（`agent` / `tools` / `channels` 都适用）：
  任何 `import doudou.<pkg>.x` 都会先执行它。这几个包互相依赖
  （`tools.registry` → `agent.memory`；`agent.react` → `tools.decorators`；
  `gateway.router` 只依赖 `channels.base`），在 `__init__` 里做重导出会立刻成环，
  表现是「导入顺序不同就报 ImportError」，极难排查。
  现状：`agent/__init__` 只导出 `events`，`tools/__init__` 只导出 `decorators`，
  `channels/__init__` 只导出协议。其余一律显式导入具体模块
  （`from doudou.tools.registry import build_tools`）。
  反过来，**能证明没人从包根导入**是加新成员时的检查项（`grep 'from doudou\.<pkg> import'`）。

## 编码约定

- Python 3.12+，全量类型注解；`mypy --strict` 必须通过。
- 行宽 100，格式化与 lint 由 `ruff` 负责（`ruff format` + `ruff check --fix`）。
- 异步优先：I/O 一律 async；CPU 密集或阻塞调用用 `asyncio.to_thread` 下沉。
- 配置读取**只**通过 `doudou.config.settings`，禁止散落 `os.environ`。
- 日志用 `structlog`（`doudou.observability.logging`），禁止裸 `print`（CLI 输出除外）。关键位置必须打印日志，包括出入参，敏感信息请模糊化处理。
- 出网调用统一走 `utils.async_utils.retry_async`，不要各写一套重试。
- 异常：领域异常定义在模块内并继承统一基类；API 层由 `api.errors` 转成响应。
- 关键逻辑处请添加注释。

## 工具 / MCP / Skill 的约定

- **内置工具**放 `src/doudou/tools/builtin/`，每个模块导出
  `build_tools(settings) -> Sequence[BaseTool]`，函数用 `@tool` 装饰并写清 docstring
  （首行是一句话摘要，模型与 `tools list` 都依赖它）。
- **每个工具必须调 `annotate(tool, group=..., side_effects=...)`**。
  未注解的工具会被当成「有副作用」而要求人工确认 —— 这是安全默认，
  且 `build_tools` 会打 `tools.missing_annotations` 告警让你立刻发现。
- **依赖不满足就不注册**（缺 API Key、白名单为空），不要注册一个永远失败的工具。
- **出网工具**必须支持注入 `httpx.AsyncClient`，这样单测能用 `httpx.MockTransport` 离线跑。
- **MCP** 只通过 `config/mcp.servers.yaml` 声明；代码里不出现硬编码的 server 地址。
- **Skill** 放仓库根 `skills/`，每个 Skill 一个目录 + `SKILL.md`（YAML frontmatter：`name`、`description`）。
- 有副作用的工具必须声明 `side_effects=True`；无确认回调时它们会被拒绝执行
  （见 `docs/tools.md` §4）。
- **碰文件路径的工具**（写/改/删）除了确认，还要调
  `PermissionPolicy.require_target(op, target, root=...)`：它同时做「必须在工作区内」
  与细粒度规则匹配。两道防线的分工是「确认 = 要不要做 / 权限 = 能不能碰这个路径」。
- **不要自己实现「问用户」**：确认统一走 `agent/approvals.py` 的 `ApprovalBroker`，
  它负责登记、超时按拒绝、决策复用与落库；各通道只负责把决策送回来。
- **破坏性能力默认关闭**：`shell_exec` 只有在 `tools.shell.enabled: true` 时才注册。
  新加这类「没有约束机制」的出口时请照此办理。
- **工具名不要与预期重名**：`tools.registry` 对重名是「留前者、丢后者」并只打告警，
  静默失效比报错更难查（`workspace_*` 就是为了避开 M9 deepagents 的 `read_file`）。
- **子 Agent 定义**放 `config/agents.yaml` 的 `subagents` 段；`tools` 白名单里请写
  **实际装载**的工具名（用 `doudou tools list` 核对），写错只告警不报错。

## 测试

- `tests/unit/`：组件级测试，不依赖网络与真实模型，用 `doudou.agent.testing` 的确定性模型。
- `tests/integration/`：走公共入口的离线端到端测试（CLI / gateway），CI 会跑。
- 需要真实模型、网络或数据库的测试必须打 `@pytest.mark.integration`，默认被 `addopts` 排除，
  用 `make test-all` 显式启用。
- 新增模块时同步补测试。

## 提交规范

Conventional Commits：`feat|fix|docs|refactor|test|chore|build|ci(scope): 描述`。
每个 PR 需通过 `make lint` 与 `make test`。
