# 可观测性

回答三个问题：**出了什么事**（审计）、**花了多少钱**（成本）、**这一轮到底怎么跑的**（轨迹）。

三者的数据来源是**同一个**东西：ReAct 循环吐出的 `RunEvent` 流。
`observability/observers.py` 定义了一层很薄的协议（`RunObserver`），
`gateway.runner` 按顺序喂事件，各实现自己决定写到哪。

```
AgentRunner.astream
   ├─ AuditObserver   → 审计表（谁/何时/什么工具/参数摘要/结果/是否确认）
   ├─ UsageObserver   → 用量表（按天 × 会话的 token 与费用）+ 预算告警
   └─ TraceObserver   → 轨迹表 run_traces（一次运行一行，步骤明细存 JSON）
```

自建的统计页（`GET /stats`）把这三份数据拼在一起，见 §4。

## 1. 三条硬约束

1. **观察者绝不能弄坏运行**：每次回调都被 `try/except` 包住，失败只打
   `observability.observer_failed`。审计系统坏了不该让聊天变不可用。
2. **只读**：观察者不修改事件、不拦截执行。要拦请用权限规则或确认回调。
3. **零配置零开销**：没打开的分组根本不构造观察者，runner 里的循环不跑。

### 1.1 删除会话 = 删除它的全部痕迹

`DELETE /api/v1/threads/{id}` 是**彻底删除**，一次清掉该会话在七张表里的行：

| 表 | 内容 |
| --- | --- |
| `messages` / `threads` | 会话本身与历史消息 |
| `session_bindings` | 渠道会话 → thread 的绑定（不留悬空映射） |
| `audit_events` | 该会话的审计记录 |
| `usage_daily` | 该会话的 token 与费用 |
| `run_traces` | 该会话的运行轨迹 |
| `approvals` | 该会话的确认请求与决策 |

响应里的 `purged` 会回显各表删了多少行。**注意**：删除会连带抹掉审计与账本 ——
需要不可删的合规留痕时，先把 `/api/v1/audit`、`/api/v1/usage` 导出到外部存储
（见 §6）。攒批写的缓冲区也会被一并清空（尚未落盘的记录不会在下次 flush 时复活）。

### 1.2 日志时间戳走哪个时区

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

## 4. 轨迹与统计页

轨迹是「这一轮到底怎么跑的」的答案，**本地落库**（`run_traces` 表），
不再依赖任何第三方追踪服务。默认**开启**——它只是写本地库，不把对话发到外部。

```yaml
observability:
  traces:
    enabled: true
    max_rows: 20000        # 行数上限，超出按时间清理
    max_text_chars: 1000   # 单段文本（步骤文本、工具参数/结果）截断长度
    batch_size: 50         # 攒批写：1 = 逐条严格持久化
```

**一次运行一行**：标量列（渠道、步数、工具数、确认数、token、耗时、成败）
用于列表与聚合，步骤明细以 JSON 存 `steps_detail` —— 点进单次运行才展开。
轨迹是**派生数据**（对话历史 + 审计足以重建），所以可以放心用 `max_rows` 压制增长、
用 `max_text_chars` 截断文本，它不是任何东西的唯一真相源。

```bash
uv run doudou serve
# 打开 http://127.0.0.1:8000/stats —— 内置统计页（无构建、无外部 CDN）
```

### 4.1 接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/v1/traces` | 运行列表（可按 `thread_id` / `channel` / `days` / `only_failed` 筛选） |
| `GET /api/v1/traces/{run_id}` | 单次运行：逐步的推理文本 + 工具调用（含参数、结果、是否被拒） |
| `GET /api/v1/stats` | 统计页数据源（下面五块一次给全） |

`/traces` 用 **`offset` + `limit` 分页**：`limit` 是单页大小（1–500，默认 50），
`offset` 是起始位置，返回里带 `total`（符合条件的总条数）与 `has_more`，因此可以显示
「第 N / 共 M 页」并跳页。排序是 `(started_at DESC, run_id DESC)`。统计页底部提供
上一页 / 下一页 / 页码跳转，顶部可选单页大小（20/50/100/200）。

`/traces` 与 `/stats` 都支持 `?thread_id=<id>`：前者筛运行列表，后者把**三份数据**
都收敛到那一条会话（默认是全部会话的汇总）。统计页顶部有「全部会话 / 具体会话」下拉。

### 4.2 统计页覆盖什么

五个数据源各管一段，**每个指标只有一个真相源**：

| 指标 | 数据源 |
| --- | --- |
| 运行量 / 成功率 / 平均与 P95 耗时 | `run_traces`（本模块） |
| 工具调用分布（次数 / 失败 / 被拒 / 平均耗时） | `audit_events`（本来就是「一次调用一行」） |
| 人工确认比例 | `audit_events`（`approved` 三态） |
| token 与费用趋势 | `usage_daily` |
| 单次运行下钻回放 | `run_traces.steps_detail`（JSON） |

刻意**不**把工具维度塞进轨迹表的 JSON 再做 SQL 聚合：审计表本就是一行一次调用，
从它聚合最直接，也避免在 JSON 上做低效查询。

### 4.3 已知的表达力限制

* `RunEvent` 里的 token 用量是**整轮累计**的（`run_end.usage`），没有按 step 拆分，
  因此统计只能给到「一次运行一共多少 token」，看不出「哪一步贵」。要按 step 拆就得改
  ReAct 循环的事件粒度，那属于「为追踪改产品代码」，需要时再做（见 §6）。
* 被拒绝的工具调用在回放里标为「被拒」——它是「模型想做、人没让做」，
  排查「为什么没写文件」时这条比任何日志都直接。

## 5. 攒批写的取舍

审计、用量与轨迹都是**写后缓冲**的（默认 50 条一批）。原因见 `docs/performance.md`：
SQLite 同一时刻只允许一个写事务，而一轮运行会产生五六次提交，
逐条提交把吞吐压到了个位数。

* 缓冲区满、读接口被调用、进程正常关闭 → 都会 flush；
* 进程被 `SIGKILL` → 可能丢掉最后不足 `batch_size` 条；
* 要严格逐条持久化：`observability.audit.batch_size: 1`（`traces.batch_size` 同理）。

## 6. 还没做的

* **按 step 的 token 拆分**（见 §4.3 的限制）；
* **Prometheus / OTel 导出**：`observability.metrics` 目前是进程内计数器，
  `/health` 里能看到，但没有对外的指标端点；
* **审计的导出**：现在只能通过 `GET /api/v1/audit` 拉 JSON，没有落 S3 / 推 SIEM 的通道；
* **轨迹的抽样**：`RunEvent` 全量落库；量很大时可考虑按比例采样（暂未做）。
