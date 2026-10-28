# 可观测性

回答三个问题：**出了什么事**（审计）、**花了多少钱**（成本）、**这一轮到底怎么跑的**（追踪）。

三者的数据来源是**同一个**东西：ReAct 循环吐出的 `RunEvent` 流。
`observability/observers.py` 定义了一层很薄的协议（`RunObserver`），
`gateway.runner` 按顺序喂事件，各实现自己决定写到哪。

```
AgentRunner.astream
   ├─ AuditObserver   → 审计表（谁/何时/什么工具/参数摘要/结果/是否确认）
   ├─ UsageObserver   → 用量表（按天 × 会话的 token 与费用）+ 预算告警
   └─ TracingObserver → Langfuse（一次运行一条 trace）
```

## 1. 三条硬约束

1. **观察者绝不能弄坏运行**：每次回调都被 `try/except` 包住，失败只打
   `observability.observer_failed`。审计系统坏了不该让聊天变不可用。
2. **只读**：观察者不修改事件、不拦截执行。要拦请用权限规则或确认回调。
3. **零配置零开销**：没打开的分组根本不构造观察者，runner 里的循环不跑。

### 1.1 日志时间戳走哪个时区

structlog 的时间戳用 `runtime.timezone`（默认 `Asia/Shanghai`），**不是 UTC**：
日志是给人对着墙上的钟看的，UTC 在 UTC+8 的机器上看起来「比现实晚 8 小时」，
每次排查都得心算一遍。需要机器可读的统一时间就开 `runtime.log_json`（JSON 里带完整偏移量，
例如 `2026-10-27T16:01:00.357372+08:00`）。

时区名写错时**只告警、回落到系统本地时区**，不会让服务起不来。
另外：进程刚起来、还没读配置前的那几行（如 `api.app_created`）用的是 structlog 默认格式
（本地时间、不含偏移量），配上之后才统一成带偏移量的 ISO。

## 2. 审计

`GET /api/v1/audit?thread_id=&limit=`（只读，没有写接口 —— 能改的审计不算审计）

一次工具调用**一行**（先建、后补结果，用 `audit_id` upsert）。
`approved` 字段是三态：

* `true` —— 需要确认，人同意了；
* `false` —— 需要确认，被拒绝（`status: rejected`）；
* `null` —— **这个工具不需要确认**（根本没问过）。

第三种必须和「同意了」区分开，否则审计会报告「全都经过人确认」，那是假的。

参数是**先打码再截断**的摘要：

* 打码用 `security.secrets.redact`（`api_key` / `token` / `password` 之类）；
* `AuditEntry.__post_init__` 里再兜一道 —— 让「审计表不含明文密钥」成为**不可能违反**的规则，
  而不是「记得调那个函数」；
* 逐字段截断（默认 2000 字/字段），并记录 `args_truncated` 说明截掉了多少 ——
  看到摘要的人必须知道这里有缺口；
* 结果只留开头一段（默认 500 字）+ `result_chars` 全长；全文在会话历史与日志里。

「为什么批准/拒绝」的细节在 `approvals` 表（`GET /api/v1/approvals`），
两张表用 `tool_call_id` 对得上。

## 3. 成本

`GET /api/v1/usage?days=7`

按「天 × 会话」累计 token 与费用，超日预算只**告警**（每天一次），不拦截 ——
拦了就是「用户的消息没人回」，那不是省钱的正确做法。

价格表是配置项，不是常量：

```yaml
observability:
  cost:
    daily_budget: 5.0          # 0 = 不设
    prices:
      default: {input: 0.0, output: 0.0}
      openai:gpt-x: {input: 3.0, output: 15.0}   # 每 100 万 token
```

**查不到价格时 `cost` 是 `null`，不是 0**，并把模型列进 `unknown_models`。
编一个数字出来比不显示更糟：它会让人以为成本在掌控之中。
同理，「已知 + 未知」合并后仍然是未知。

## 4. 追踪（Langfuse）

默认**关闭**。打开需要三件事同时成立（缺一个就安静降级并告警）：

1. `observability.tracing.enabled: true`
2. 装了 SDK：`uv sync --extra tracing`（或 `--all-extras`）
3. `DOUDOU_OBSERVABILITY_TRACING_PUBLIC_KEY` / `_SECRET_KEY` 齐备

```yaml
observability:
  tracing:
    enabled: true
    host: https://cloud.langfuse.com   # 自建填自己的地址（v2 自建默认 :3000）
    environment: prod
    sample_rate: 1.0
```

把对话内容发到第三方必须是一次**明确的决定**，所以宁可多一个开关，
也不做「装了包就发」。

### 4.1 SDK 版本：为什么钉在 2.x

| | 依赖 | 上报方式 | 服务端要求 |
| --- | --- | --- | --- |
| **本仓库** | `langfuse>=2.60,<3` | 旧版 ingestion 接口 | v2 / v3 都能收 |
| v3 SDK（未采用） | `langfuse>=3` | OTEL：`POST /api/public/otel/v1/traces` | **只对 v3 服务端有效** |

自建实例常见的是 2.95.x，它**没有** `/api/public/otel` 端点（`GET` 它是 404）—— 用 v3 SDK
会表现为「key 配了、什么都没有、也没有报错」（SDK 内部吞掉上报错误）。2.x SDK 两边都能对接，
所以钉 2.x；以后服务端升到 v3 也可以继续用它。

### 4.2 发出的结构

v2 的 API 是命令式的：`client.trace()` 起步，之后逐层 `span()` / `generation()`，各自 `end()`。
`release` / `environment` 由 SDK 在**构造客户端**时注入到每条 trace。

```
run:feishu                    input=用户输入, output=最终答复, session=thread_id, tags=[channel]
├── step 1
│   ├── generation:model      output=这一步的文本
│   └── tool:web_search       input=参数, output=结果摘要, level=WARNING(失败/被拒)
└── step 2                    ← 整轮累计的 usage 挂在这一步的 generation 上
    └── tool:workspace_write  approved=false → level=WARNING
```

两个刻意的设计：

* **运行结束时整批发出**，不是边跑边发。观察者是回调式的（`on_run_*` 在不同时刻），
  跨回调持有 span 会带来悬挂 span 与并发交错的问题；攒到最后一次性建完、`end()` 完更安全。
  代价是 trace 晚几秒出现。攒的事件有上限（500 条）。
* **被拒绝的工具调用标成 WARNING**：它是「模型想做、人没让做」，
  排查「为什么没写文件」时这条比任何日志都直接。
* 失败在 v2 里没有 trace 级的 `level`/`status_message`，改用一条 `error`（ERROR）事件标出来。

### 4.3 已知的表达力限制

`RunEvent` 里的 token 用量是**整轮累计**的（`run_end.usage`），没有按 step 拆分；v2 的用量又
只能挂在 generation 上。因此它被挂在**最后一个** generation 上 —— Langfuse 里 trace 级的用量
合计仍然正确，但看不出「哪一步贵」。要按 step 拆就得改事件粒度，那属于「为追踪改产品代码」，
需要时再做（见 §6）。

## 5. 攒批写的取舍

审计与用量是**写后缓冲**的（默认 50 条一批）。原因见 `docs/performance.md`：
SQLite 同一时刻只允许一个写事务，而一轮运行会产生五六次提交，
逐条提交把吞吐压到了个位数。

* 缓冲区满、读接口被调用、进程正常关闭 → 都会 flush；
* 进程被 `SIGKILL` → 可能丢掉最后不足 `batch_size` 条；
* 要严格逐条持久化：`observability.audit.batch_size: 1`。

## 6. 还没做的

* **按 step 的 token 拆分**（见 §4 的限制）；
* **Prometheus / OTel 导出**：`observability.metrics` 目前是进程内计数器，
  `/health` 里能看到，但没有对外的指标端点；
* **审计的导出**：现在只能通过 `GET /api/v1/audit` 拉 JSON，没有落 S3 / 推 SIEM 的通道。
