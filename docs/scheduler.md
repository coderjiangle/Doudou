# 定时任务

在聊天里让 Agent 建任务，到点由服务过一遍 LLM，把结果推到目标会话。

```bash
uv run doudou serve        # 定时任务由常驻服务托管（和飞书长连接一样）
```

> ⚠️ **必须在跑 `doudou serve`**。只在 `doudou chat` 里建任务、没起服务，是不会触发的。

## 1. 用法

直接在对话里说「每天 9 点提醒我看邮件」「半小时后提醒我一下」之类，Agent 会调用：

| 工具 | 作用 |
| --- | --- |
| `schedule_create(name, prompt, kind, expr, timezone)` | 建任务；到点按 `prompt` 跑一遍 |
| `schedule_list()` | 列出任务与状态（只读） |
| `schedule_cancel(task_id)` | 取消（删除）任务 |

`kind` / `expr` 三种写法：

| kind | expr | 例子 |
| --- | --- | --- |
| `every` | 间隔：`90`/`30s`/`5m`/`2h`/`1d`（最小 30s） | `30m` |
| `at` | 一次性，ISO 时间 | `2026-11-01T09:00:00` |
| `cron` | 5 段 crontab | `0 9 * * *`（每天 9 点） |

`timezone` 填 IANA 名（如 `Asia/Shanghai`），留空用 `runtime.timezone`。

也可以用只读接口看当前任务：`GET /api/v1/scheduled`。

## 2. 投递到哪（需求 2）

**任务不绑定任何会话**，只记「投递目标」＝ `channel` + `conversation_id`：

| 建任务的地方 | 推给谁 |
| --- | --- |
| 飞书**群** | 那个群 |
| 飞书**私聊** | 那条私聊 |
| **CLI / Web** | **飞书私聊**（建的时候还不知道是哪条，触发时再解析） |

CLI / Web 任务的「飞书私聊」靠一张绑定表（`channel_targets`）解析：用户**私聊过机器人**
之后会记下他的 p2p `chat_id`。解析规则：

* 配了 `scheduler.owner_user_id` → 用该用户的绑定；
* 否则只有**唯一一条**绑定才能确定；零条或多条 → 解析不到。

## 3. 触发时做什么（需求 3 / 4 / 5）

```
poller（每 tick_seconds 扫一次）
  └─ 到期任务：先推进 next_run_at（长运行不叠触发）
       ├─ 解析投递目标 → 解析不到 ⇒ 跳过本次（不跑模型、不推送）
       └─ 合成一条消息 → MessageRouter.run_now → 完整 ReAct 循环
            └─ 用**目标会话的同一个 thread**，消息并入该会话上下文（需求 5）
                 └─ 最终答复渲染成卡片/文本推到目标会话（需求 4）
```

* **需求 4**：每个任务触发都过一遍 LLM，没有「模板化静态提醒」这种东西。
* **需求 5**：触发的运行落在**目标会话**的同一个 thread 上，历史自然衔接 ——
  用户在群里收到推送后，接着在群里问「刚才那个再多说点」，模型看得到上下文。
* **需求 3（跳过）**：触发时**拿不到可推送目标**就跳过整次（对应 `last_status: skipped`）：
  渠道没启用 / CLI-Web 任务解析不到飞书私聊 / 目标通道没在运行。
  另外 `bus` 忙、运行超时、运行报错都会记在 `last_status`（`skipped`/`failed`）里，
  不会把调度循环带崩。

触发的消息以 `[定时任务] <name>：<prompt>` 的形式写进会话历史 —— **可见、可追溯**。

## 4. 时间点与重启

* 任务的唯一真相源是 `scheduled_tasks` 表（DB），进程只做轮询 —— **重启天然安全**。
* 停机期间错过的触发点默认**不补跑**（`scheduler.catch_up: false`）：只把时间点顺延，
  避免一重启就涌出一堆过期推送。要补跑就把它设成 `true`。
* 时间表达用 `apscheduler` 的 `CronTrigger` 解析（只借它算时间，**不用它的调度器** ——
  否则 DB 与内存里各存一份 job，重启还要重同步）。

## 5. 配置

```yaml
scheduler:
  enabled: true
  tick_seconds: 30.0           # 轮询间隔；到点任务的最小精度
  timezone: null               # 解释 cron/间隔；null 继承 runtime.timezone
  max_tasks: 200
  run_timeout_seconds: 600.0
  owner_user_id: null          # CLI/Web 任务的飞书私聊归属（open_id）
  catch_up: false
```

环境变量：`DOUDOU_SCHEDULER_*`（见 `.env.example`）。

## 6. 已知限制

* **CLI 建的任务要落库**才有意义：CLI 会按 `storage.database_url` 单独开一个库
  （连不上就安静降级、不注册 `schedule_*` 工具）。若 `storage.history: memory`，
  任务存在内存里，`serve` 实例看不到 —— 想跨进程请用 `database`。
* 单进程单副本：`tick` 与触发都在 `serve` 进程内。多副本会重复触发
  （需要分布式锁 + 抢占，暂未实现）。
* 「投递目标」只支持飞书：`creation_target` 固定把 CLI/Web 的任务指向飞书私聊。
* 任务的 `prompt` 会进入目标会话历史，因此**受上下文预算约束**（见 `docs/observability.md`）。
