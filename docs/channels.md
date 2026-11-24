# 通道接入

## 1. 通道抽象

所有通道实现同一协议（`src/doudou/channels/base.py`）：

```python
class Channel(Protocol):
    name: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, message: OutboundMessage) -> None: ...

    # 入站消息统一转成 InboundMessage 交给 gateway
```

`InboundMessage` 关键字段：

| 字段 | 说明 |
| --- | --- |
| `channel` | 通道名，如 `feishu` |
| `conversation_id` | 会话标识（飞书为 `chat_id`；私聊用 `open_id`） |
| `thread_key` | 进一步区分话题的键（群内话题/回复链） |
| `user_id` / `user_name` | 发送者 |
| `text` | 纯文本内容 |
| `raw` | 原始事件（排查用） |
| `message_id` | 通道侧消息 ID，用于回复与去重 |
| `images` | 已校验的内嵌图片；飞书图片下载完成后填充 |

图片通过 `images` 传入模型；普通文件与音频尚未接入。

## 2. 飞书（Lark）通道

> 飞书依赖是可选的：先运行 `uv sync --extra feishu`（发布包用
> `pip install "doudou-agent[feishu]"`）。启用 `channels.feishu` 但缺少依赖时，
> `doudou config check` 会报错，服务日志也会给出安装提示。

**当前实现：WebSocket 长连接**（`channels.feishu.mode: websocket`）。
好处是不需要公网地址、不需要内网穿透，本地开发与内网部署都能直接跑。

### 2.1 为什么长连接模式不需要 `crypto.py`

事件回调模式要用 `Encrypt Key` 做 AES 解密、用 `Verification Token` 校验，
是因为那个 HTTP 端点是**公网可访问**的，必须证明请求真的来自飞书。
长连接是我们的进程主动连出去、并由 app_id/app_secret 完成鉴权，
事件经 SDK 解析后交给我们，因此**没有验签解密这一层**。
`crypto.py` 与 `api/routes/webhooks.py` 留到需要公网回调时再补。

### 2.2 创建应用

1. [飞书开放平台](https://open.feishu.cn/app) → 创建**企业自建应用**。
2. 「凭证与基础信息」记下 `App ID`、`App Secret`。
3. 「权限管理」开通 —— 这一步最容易配错：

   | 权限 | 用途 | 说明 |
   | --- | --- | --- |
   | `im:message.p2p_msg:readonly` | 私聊收消息 | 获取用户发给机器人的单聊消息（旧版名 `im:message.p2p_msg`） |
   | `im:message.group_at_msg:readonly` | 群里 @ 收消息 | 获取群组中 @ 机器人的消息（旧版名 `im:message.group_at_msg`） |
   | `im:message:send_as_bot` | 发消息 | 以应用身份发消息 |
   | `im:chat:readonly` | 读群信息 | 群聊判断，以及启动时自动查 `bot_open_id` |

   > **`im:message`（「读取消息」）不够。** `im.message.receive_v1` 是**按消息类权限过滤推送**的：
   > 权限没覆盖到的消息既不推送、也不报错。只开 `im:message` 的表现是
   > 「长连接正常、控制台验证也成功，但一条消息都收不到」——排查顺序见 §2.11。

4. 「事件订阅」**选择「使用长连接接收事件」**，订阅 `im.message.receive_v1`。
   （不需要填请求地址，也不需要 `Verification Token`。）
   权限或事件变更后**必须重新创建版本并发布**，否则生效的还是旧版本。
5. 把机器人拉进群，或在私聊里找它说话。

### 2.3 配置

```dotenv
# .env —— 环境变量 = DOUDOU_ + config.yaml 路径
DOUDOU_CHANNELS_FEISHU_ENABLED=true
DOUDOU_CHANNELS_FEISHU_MODE=websocket
DOUDOU_CHANNELS_FEISHU_APP_ID=cli_xxxxxxxx
DOUDOU_CHANNELS_FEISHU_APP_SECRET=xxxxxxxx
```

```yaml
# ~/.doudou/config/config.yaml
channels:
  feishu:
    enabled: true
    mode: websocket
    bot_name: Doudou          # 用于群聊 @ 判定，需与飞书里的显示名一致
    # bot_open_id: ou_xxx     # 更可靠的 @ 判定；留空时会自动去查
    group_require_at: true    # 群里必须 @ 机器人才响应
    isolate_by_thread: false  # false = 同一个群共享一个上下文
    stream_reply: true
    card_update_interval_ms: 800
    allowed_chats: []         # 非空即启用白名单
```

启动：`uv run doudou serve`（长连接随 HTTP 服务一起托管）。

### 2.4 会话隔离：同一个群共享一个上下文

`isolate_by_thread: false`（默认）时，会话标识就是 `chat_id`，
也就是**一个群 = 一个 thread = 一段连续上下文**，群里的助手像一个同事。

代价与已知行为：

- 同一个群里不同成员的提问会**互相看得见**（都在同一段历史里）；
- 想改成「每人独立」需要把会话标识改成 `chat_id + open_id`，目前没有这个开关；
- 想改成「每个话题独立」则打开 `isolate_by_thread: true`（按 `thread_id` 拆）。

### 2.5 回复形态：整卡 patch

收到消息后立刻发一张「正在处理」卡片，随后**整卡更新**（节流
`card_update_interval_ms`），最终覆盖成答案卡片。卡片上会保留工具调用痕迹
（`✓ web_search — 找到了 3 条`）—— 这是检索类回答建立信任的关键：
只给答案不给过程，用户无法判断可信度。

**不回显用户原话**：卡片就紧跟在用户消息下面，再抄一遍只是噪音（早期版本会
把问题加粗放在卡片顶部，看起来像消息被重复推送）。需要上下文场景的是确认卡片
（§2.9），那里会带上「你问的是什么」。

**长度**：飞书对卡片消息的硬限制是 **30 KB**（错误码 `230025`），不是字符数。
所以 `answer_card` 先按 8000 字粗筛，再按整卡序列化后的字节数（`MAX_CARD_BYTES`
= 28 KB，留 2 KB 给 JSON 转义与工具痕迹）精筛；只有真的放不下才截断，并且
只留一处「已截断 N 字」说明（以前会同时出现「（已截断 N 字）」和
「内容过长已截断」两条，像是出了两个问题）。

飞书另有 cardkit 增量更新接口（`cardkit.v1.card_element.content`），只推变化的元素、
流量更省。这里选整卡 patch 是因为 API 面小、行为可预测，配合节流已经足够像流式；
增量更新作为后续优化。

失败降级：卡片发不出去 / 更新失败时**改发一条纯文本**，保证用户一定看得到结果。

### 2.6 群聊 @ 判定

优先看事件的 `mentioned_type == "bot"`（飞书会填这个字段）；
取不到时退回按 `bot_open_id`（启动时自动获取，或配置里显式指定）与
`bot_name` 比对。三条都落空会记警告 —— 静默失效比报错更难排查。

### 2.7 幂等

飞书在重连或抖动时可能重推同一事件。因此按 `event_id` + TTL 去重
（`dedup_ttl_seconds`），并对条目数设上限（`OrderedDict` 按插入顺序淘汰），
保证长时间运行不会把内存吃光。

### 2.8 一个必须记下的坑：lark 的模块级事件循环

`lark_oapi.ws.client` 在**模块导入时**就执行了 `loop = asyncio.get_event_loop()`，
`ws.Client.start()` 会 `loop.run_until_complete(_select())` 一直阻塞。

问题在于：如果 lark 已经在主线程被导入过（构造 `lark.Client` 就会导入它），
那个模块级 `loop` 就是**主事件循环**，于是 `start()` 里的
`run_until_complete` 会抛 `This event loop is already running` ——
**长连接根本连不上，而异常被线程吞掉，表面看服务一切正常**。

处理方式（见 `channels/feishu/channel.py` 的 `_run`）：
在专属 daemon 线程里新建 loop、`set_event_loop`，并把 lark 的模块级 `loop` 指过去。
停止时向该 loop 投递 `stop`。这是它当前设计下唯一可靠的做法。

### 2.9 人工确认的卡片按钮

需要人确认的工具（写文件、执行命令…）会往对应会话推一张**带「同意 / 拒绝」按钮的卡片**，
点击回到 `ApprovalBroker`，卡片随即换成结果态。要点：

- 按钮里带的是 `approval_id`（**这一次**调用），不是工具名 ——
  把「同意写文件」当成一次性授权，会让后续每次写文件都静默通过；
- 打开 `agent.approval_timeout_seconds` 到了还没人点，按**拒绝**处理（悬着的确认等于没有确认）；
- 卡片被点两次只认第一次（幂等）；
- 为此代码注册了两个长连接处理器：消息 `im.message.receive_v1` 与
  **卡片回传交互 `card.action.trigger`** —— 开放平台那边两个都要订阅，
  否则按钮点了没反应（长连接模式下这一项最容易漏）。

> 说明：`card.action.trigger` 这条路径**没有用真实飞书应用验证过**
> （开发期间一直没有可用的 App 凭据）。代码路径有单测覆盖（假 lark 客户端 +
> 假点击对象），但「开放平台是否需要额外勾选」请以控制台实际提示为准。

### 2.10 图片输入

私聊中可以直接发送图片；富文本 `post` 支持文字与图片混合，也支持多张图片。
通道通过消息资源接口下载图片，再交给视觉模型。图片 key 本身不会作为图片内容发给模型。

- 在飞书开放平台开通 `im:resource`（获取与上传图片或文件资源）权限并重新发布应用。
- 群聊仍遵守 `group_require_at`：默认需要在同一条富文本消息中 @机器人并插入图片。
  单独发送一张没有 @ 的群图片不会触发助手。
- 每条最多 4 张，每张不超过 5 MiB，支持 PNG、JPEG、GIF、WebP；CLI 保持纯文字输入。
- 任意一张下载失败、格式不符或超限时，会回复错误提示，不会丢掉图片后仅处理配文。
- 模型需支持视觉输入；图片会进入会话历史，Web 历史页面也可查看原图。

接口参考：[获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message-resource/get)。

### 2.11 尚未实现

| 项 | 说明 |
| --- | --- |
| `callback` 模式 | 需要 `crypto.py`（AES 解密 + 验签）与公网回调路由 |
| 文件 / 音频消息 | 当前支持 `text`、`post`、`image`；普通文件和语音尚未接入 |
| 群成员身份映射 | 只透传 `open_id`，没有解析真实姓名 |

### 2.12 收不到消息：按这个顺序排查

长连接、事件订阅、消息权限是三件独立的事，「连上了」**不代表**「会推事件」。
按下面顺序看，每一步都有可观察的证据：

| # | 检查 | 通过的样子 | 不通过的症状 |
| --- | --- | --- | --- |
| 1 | 长连接是否建立 | 启动日志有 `feishu.ws_connecting`，之后**没有** `feishu.ws_crashed` | `feishu.ws_crashed`（凭据错 / 握手失败）；连 `ws_connecting` 都没有（线程没起来） |
| 2 | 事件订阅方式 | 「事件与回调 → 事件配置」订阅方式 = **使用长连接接收事件** | 填过请求地址 → 事件全走 HTTP，长连接一条都收不到 |
| 3 | 已订阅事件 | 已添加 `im.message.receive_v1` | 没添加 → 什么都不推 |
| 4 | 消息类权限 | 已开通 §2.2 的 `im:message.p2p_msg:readonly`（私聊）/ `im:message.group_at_msg:readonly`（群 @） | 只开 `im:message` → **静默不推**（飞书按权限过滤推送） |
| 5 | 版本是否生效 | 权限/事件变更后重新发布，状态为已发布/已生效 | 「审核中 / 待管理员审核」→ 线上生效的仍是旧版本 |
| 6 | 消息是否被过滤 | 入站日志 `feishu.inbound` | `feishu.event_skipped`（debug 级，默认看不到）：文件/音频、群里没 @、白名单外 |
| 7 | 会话是否正确 | 私聊机器人，或在群里 @ 它 | 在工作台点开的是「应用主页」，或 @ 的是另一个应用 |

两条最容易误判的：

- 控制台的「**验证长连接**」只验证**通道**，不验证推送过滤 —— 消息权限不够时它照样显示「连接成功」。
- `bot/v3/info`、`im/v1/chats` 能调通只说明**应用已发布、权限已生效**，不代表消息事件会推给你。

## 3. 通用 Webhook 通道

`channels/webhook/` 提供一个泛化入站通道，便于对接任意系统：

```
POST /api/v1/webhooks/generic
Header: X-Doudou-Token: <token>
Body:   { "conversation_id": "...", "user_id": "...", "text": "..." }
```

响应可同步返回结果，也可 `?async=true` 只接受入队。

## 4. CLI 通道

`uv run doudou chat` 启动交互式多轮对话，支持：

| 命令 | 作用 |
| --- | --- |
| `/new` | 开启新会话（换新的 thread，清空上下文） |
| `/history` | 查看当前会话的 thread 与历史条数 |
| `/tools` | 列出已装载的工具（含「需确认」标记） |
| `/help` | 显示帮助 |
| `/exit` | 退出 |

单轮场景用 `uv run doudou ask "问题"`；`--quiet` 只输出最终答复（便于脚本调用），
`--no-stream` 关闭流式，`--no-confirm` 关闭工具执行前的人工确认。

## 5. 新增通道步骤

1. 在 `src/doudou/channels/` 建子包，实现 `Channel` 协议。
2. 在 `channels/registry.py` 注册构造逻辑（按配置启用）。
3. 在 `config/schema.py` 的通道配置里加对应段落。
4. 在 `api/routes/webhooks.py` 增加回调路由（如需要）。
5. 补 `tests/unit/` 解析与去重测试。

`gateway` 与 `agent` 无需任何改动。
