# 工具开发规范

## 1. 工具来源

| 来源 | 位置 | 状态 |
| --- | --- | --- |
| 内置工具 | `src/doudou/tools/builtin/` | ✅ `clock` / `files` / `http` / `web_search` / `shell` / `skills` / `memory` / `delegation` / `self_config` |
| MCP 工具 | `config/mcp.servers.yaml` | ✅ 通过 `build_tools(extra=...)` 注入 |
| deepagents / LangGraph 内置工具 | 框架提供 | ❌ 不采用：自研循环已覆盖 Skill / 上下文 / 子 Agent / 确认 |
| 沙箱执行 | 容器 / 沙箱后端 | ⏳ 待评估；当前 `shell_exec` 自带，默认禁用 |

全部在 `tools/registry.py` 汇总后交给 Agent。

## 2. 两条硬约定

### 2.1 每个工具都必须 `annotate()`

```python
from doudou.tools.decorators import annotate

annotate(my_tool, group="web", side_effects=False, timeout=None)
```

元数据挂在 `BaseTool.metadata["doudou"]` 下，包含：

| 字段 | 作用 |
| --- | --- |
| `group` | 分组名，用于 `tools.enabled_groups` 开关与清单展示 |
| `side_effects` | 是否会产生副作用。**只读工具必须显式写 `False`** |
| `timeout` | 单次调用超时；留空用全局默认 |

**未注解的工具会被当作「有副作用」** —— 这是安全默认（忘了声明的写操作不该静默放行），
代价是只读工具会被要求逐一确认。因此 `build_tools` 遇到未注解的工具会打
`tools.missing_annotations` 告警，让遗漏立刻可见。

`side_effects=True` 的工具在默认策略下调用前需要人工确认，
不需要再到 `agent.interrupt_on` 里逐个罗列（配置可以覆盖，见 §4）。

### 2.2 依赖不满足就**不注册**，而不是注册一个永远失败的

| 情况 | 行为 |
| --- | --- |
| 没有 `DOUDOU_TOOLS_WEB_SEARCH_API_KEY` | `web_search` 整组不出现 |
| `tools.http.allowed_domains` 为空 | `http_request` 不出现 |
| `config.yaml` 里 `tools.disabled: [x]` | `x` 不出现 |

理由：一个永远失败的工具只会占提示词、诱导模型反复乱试。
`doudou tools list` 与 `doudou config check` 会用
`registry.describe_groups()` 说明**每个分组为什么在/不在**列表里。

### 2.3 web_search 的 provider

`tools.web_search.provider` 可选 `tavily`（默认）或 `deepseek`：

| provider | 说明 |
| --- | --- |
| `tavily` | 支持 `search_depth` / `include_answer`，可返回综合答案 |
| `deepseek` | DeepSeek 自研搜索，结果优先取 `summary`；`need_content` 控制是否取网页正文 |

凭据统一放在 `tools.web_search.api_key`（环境变量 `DOUDOU_TOOLS_WEB_SEARCH_API_KEY`），
provider 只决定调哪家服务，换 provider 不必换字段名。

### 2.4 shell_exec 的沙箱

`tools.shell.sandbox: bwrap`（仅 Linux）会把命令包进 [bubblewrap](https://github.com/containers/bubblewrap)：

| 范围 | 行为 |
| --- | --- |
| 工作区 | 读写绑定（唯一可写目录） |
| 工作区的父目录 | tmpfs 遮住 —— `config/`、`.env`、数据库都不可见 |
| 系统路径 | `/usr` 只读，`/bin`、`/lib*`、DNS、CA 证书等尽力只读挂载 |
| `/proc`、`/dev`、`/tmp` | 新实例，独立 pid namespace；`HOME` 指向 `/tmp` |
| 网络 | **不隔离**（装依赖、`curl` 仍可用） |

需要系统安装 bubblewrap（Debian/Ubuntu：`apt install bubblewrap`）。不满足时 `shell`
分组整组不注册，`doudou tools list` 会说明原因。沙箱只隔离文件系统，不替代容器。

### 2.5 my：只读的配置自检

`meta` 分组只有一个工具 `my`，用来回答「你用的什么模型」「工作区在哪」「密钥配好了吗」
这类关于**自身配置**的问题：

| 参数 | 行为 |
| --- | --- |
| 省略 / `all` / `overview` | 关键配置概览（模型、步数、超时、服务地址、已装载工具名…） |
| 段落名（`model`、`agent`、`tools`、`storage`…） | 展开该段 |
| 点号路径（`agent.max_steps`） | 只给这一个值 |

三条边界：

- **只读**：没有 setter，签名里只有 `section` 一个参数，`side_effects=False`；
  改配置只能由用户改 `config.yaml` / `.env` 后重启。
- **密钥只报状态**：`credentials` 里是 `true/false`，不返回明文；其余字段命中
  `api_key` / `secret` / `token` 名字的值统一打码，数据库 URL 里的密码也会遮掉。
- **输出截断**：单次最多 8000 字符，避免「把全部配置贴一遍」撑爆上下文。

它在 `registry` 里最后构造，因为要把**实际装载**的工具清单填进 `tools.loaded`。

## 3. 写一个内置工具

1. 位于 `src/doudou/tools/builtin/<主题>.py`，导出 `build_tools(settings) -> Sequence[BaseTool]`。
2. 在 `registry.BUILTIN_PROVIDERS` 里登记 `(分组名, 提供方)`。
3. 用 LangChain 的 `@tool` 装饰；函数名即工具名。
4. **docstring 是给模型看的**，必须写清：做什么、参数含义、返回值结构、何时不该用。
   首行要是一句话摘要（`tools list` 只显示首行）。
5. 出网工具要能注入 `httpx.AsyncClient`（`build_tools(settings, client=...)`），
   这样单测可以用 `httpx.MockTransport` 完全离线跑。
6. 失败时抛 `ValueError` 子类（如 `HttpToolError`），消息给出**可操作的下一步** ——
   它会原样回填给模型，模型据此纠正或向用户说明。
7. 阻塞调用用 `asyncio.to_thread` 下沉；重试统一走 `utils.async_utils.retry_async`。

### 命名与分组

| 前缀 / 名 | 用途 |
| --- | --- |
| `web_` | 联网检索（`web_search`） |
| `workspace_` | 工作区文件读写（前缀即边界说明；也让第三方工具撞名的风险降到最低） |
| `http_` | 原始出站请求 |
| `now` | 当前日期与时间 |
| `shell_exec` | 命令执行（默认不注册） |
| `read_skill` / `list_skill_files` / `skill_read_file` | 技能按需加载 |
| `remember` | 往记忆文件里追加（有副作用） |
| `task` | 委派给子 Agent |
| `my` | 查询自身配置（只读） |

`doudou tools list` 会按分组聚合并说明「为什么某个分组不在列表里」
（`registry.describe_groups()`），因此排查「工具有没有生效」不必猜。

### 子 Agent（`task` 工具）

子 Agent 的价值是**上下文隔离**：主 Agent 把检索/比对/试错丢出去，只取回一段结论。
定义写在 `config/agents.yaml` 的 `subagents` 段（见该文件的注释）。

三条刻意的约束：

- **子 Agent 拿不到 `task`**（`FORBIDDEN_TOOLS`），否则递归深度不可控；
- **白名单优先**：`tools` 非空时只有列出的工具可见。职责越窄，跑偏空间越小；
- **确认策略按子 Agent 自己的工具清单重算**（由 `registry` 以回调注入）。
  直接抄主 Agent 的 `interrupt_on` 会让子 Agent 的写操作静默免确认 —— 那是个真实的绕过。

白名单里写了不存在的工具会打 `subagents.unknown_tools` 告警（不报错）——
静默忽略会让「我明明配了工具」变成谜。工具名请用 `doudou tools list` 核对。

## 4. 副作用与人工确认

三档策略：

| 档位 | 例子 | 默认行为 |
| --- | --- | --- |
| 只读 | 检索、抓取、读文件、查时间 | 直接执行 |
| 可逆写 | 写工作区文件、改文件内容 | `side_effects=True` → 需要确认 |
| 不可逆 / 对外 | 删文件、执行命令、发消息、提交代码 | 需要确认 + `agent.permissions` 兜底 |

确认回调由入口层提供，语义各不相同：

| 入口 | 行为 |
| --- | --- |
| `doudou chat` / `doudou ask` | 终端询问「是否执行工具 xxx？」 |
| 加 `--no-confirm` | **一律同意**（用户明确表示不想被打断），每次都打 `cli.tool_auto_approved` 日志 |
| 加 `--quiet` | **不提供回调 → 拒绝执行**。无人在场就不能替用户同意写操作 |
| 飞书 | 推带「同意 / 拒绝」按钮的卡片，点击回到同一个 broker |
| HTTP API | `GET /api/v1/approvals` + `POST /api/v1/approvals/{id}`；超时按拒绝 |

后两个入口走的是同一个 `agent/approvals.py` 里的 `ApprovalBroker`（见
`docs/security.md` §6）：Agent 只等一个决策，谁送来都行。

写类工具除了确认，还要过路径权限规则：

```python
@tool
def workspace_write(path: str, content: str) -> str:
    """把内容写入工作区里的文件（新建或整体覆盖）。"""
    target = _resolve(root, path)  # 硬边界：跑不出工作区
    policy.require_target("write", target, root=root)  # 细粒度规则 + containment 兜底
    ...
```

**两道防线各管一件事**：确认决定「要不要做」，权限决定「能不能碰这个路径」，
硬边界保证「跑不出工作区」。三层都过了才真的落盘。

配置可以覆盖自动拦截：

```yaml
agent:
  interrupt_on:
    workspace_write: false    # 显式关掉某个工具的确认
    "*": true                 # 或全部都要确认
  require_approval_for_side_effects: false   # 或整体关掉「有副作用就拦」
  approval_timeout_seconds: 300              # 等不到决策就按拒绝处理
```

## 5. 出网工具的安全要求

`http_request` 是本仓库最容易变成 SSRF 入口的地方，策略是**默认拒绝**：

1. 允许列表为空 → 工具不注册；
2. 域名必须命中允许列表（`example.com` 含子域，`*.example.com` 只含子域）；
3. **解析出来的 IP 若是内网/回环/链路本地/保留地址一律拒绝** ——
   光校验域名挡不住「公网域名解析到 10.0.0.1」；
4. 只允许 `GET` / `HEAD`，限制重定向次数、响应体大小与超时；
5. 非文本类型（PDF、图片）直接说明「无法展示」，不把二进制塞进上下文。

新增出网工具时请复用这些约束，不要另起一套。

## 6. 测试

- 出网工具：注入 `httpx.MockTransport` 客户端，**不依赖真实网络**。
  DNS 相关的判定只对 `localhost` 这类 hosts 文件可解析的名字做真实查询。
- 写操作工具：用 `tmp_path` 做根目录，避免污染仓库里的 `data/`。
- 新增工具后补一条「工具清单快照」断言，防止工具面无意变化。
- 工具的**拒绝路径**（越权、超大文件、非法参数）必须有测试。

## 7. 清单

```bash
uv run doudou tools list           # 名称 + 分组 + 来源 + 副作用 + 需确认 + 说明
uv run doudou tools list --args    # 附带参数列表
uv run doudou tools show now       # 单个工具的完整信息（JSON）
```

也可通过 `GET /api/v1/tools` 查看（含 `side_effects` 与 `requires_approval`）。
