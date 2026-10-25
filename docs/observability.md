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
3. `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 齐备

```yaml
observability:
  tracing:
    enabled: true
    host: https://cloud.langfuse.com   # 自建填自己的地址
    environment: prod
    sample_rate: 1.0
```

把对话内容发到第三方必须是一次**明确的决定**，所以宁可多一个开关，
也不做「装了包就发」。

发出的结构：

```
run:feishu                    input=用户输入, output=最终答复, usage=input/output
├── step 1
│   ├── generation:model      output=这一步的文本
│   └── tool:web_search       input=参数, output=结果摘要, level=WARNING(失败/被拒)
└── step 2
    └── tool:workspace_write  approved=false → level=WARNING
```

两个刻意的设计：

* **运行结束时整批发出**，不是边跑边发。SDK 是 OTEL 风格（span 必须成对 `with`），
  而我们的观察者是回调式的；跨回调持有 span 会带来悬挂 span 与并发交错的问题。
  代价是 trace 晚几秒出现，换来的是「不会漏 end」。攒的事件有上限（500 条）。
* **被拒绝的工具调用标成 WARNING**：它是「模型想做、人没让做」，
  排查「为什么没写文件」时这条比任何日志都直接。

已知的表达力限制：`RunEvent` 里的 token 用量是**整轮累计**的，没有按 step 拆分，
因此 `usage_details` 只挂在根节点上。要按 step 拆就得改事件粒度 ——
那属于「为追踪改产品代码」，需要时再做（见 §6）。

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
