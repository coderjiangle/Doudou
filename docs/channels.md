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
| `attachments` | 附件（图片/文件） |
| `raw` | 原始事件（排查用） |
| `message_id` | 通道侧消息 ID，用于回复与去重 |

## 2. 飞书（Lark）通道

### 2.1 创建应用

1. 打开[飞书开放平台](https://open.feishu.cn/app) → 创建**企业自建应用**。
2. 「凭证与基础信息」记下 `App ID`、`App Secret`。
3. 「权限管理」开通：
   - `im:message`（读取消息）
   - `im:message:send_as_bot`（以应用身份发消息）
   - `im:chat:readonly`（读群信息，用于群聊 @ 判断）
   - `im:resource`（图片/文件资源，如需收发附件）
4. 「事件订阅」：
   - 请求地址填 `https://<你的域名>/api/v1/webhooks/feishu/events`
   - 记下 `Verification Token`，如启用加密再记下 `Encrypt Key`
   - 订阅事件：`im.message.receive_v1`（接收消息）、`im.message.reaction.created_v1`（可选）
5. 本地开发时用内网穿透把回调暴露出去，或改用长连接模式（见 2.4）。

### 2.2 事件回调时序

```
飞书平台 ──POST/加密──▶ /api/v1/webhooks/feishu/events
                         │
                         ├─ crypto.verify_signature / decrypt
                         ├─ 首次配置：url_verification 握手 → 回 challenge
                         ├─ events.events 解析 → 按 event_id 去重
                         ├─ 立即 200（飞书要求 3s 内）
                         └─ 入 gateway.bus 异步处理
                                  │
                                  ▼
                    gateway.runner → agent 流式执行
                                  │
                    cards.build_* → 卡片创建/更新（PATCH）
                                  │
                                  ▼
                    飞书客户端显示增量内容
```

**为什么必须立即 200**：飞书对超时事件会重推，重推会导致重复回答。
`channels/feishu/events.py` 用 `event_id` + TTL 做幂等去重。

### 2.3 回复形态

| 模式 | 行为 | 适用 |
| --- | --- | --- |
| `reply_mode: card` | 先发一张「思考中」卡片，随后按 `card_update_interval_ms` 节流更新卡片内容 | 推荐，体验接近流式 |
| `reply_mode: text` | 等 agent 跑完，一次性发文本 | 简单场景、卡片权限不足时 |

卡片相关能力集中在 `channels/feishu/cards.py`：

- 流式文本卡片（带「生成中」状态，结束后去掉）
- 工具调用过程卡片（可选，显示正在调用哪个工具）
- 人工确认卡片（配合 `interrupt_on`，提供「同意 / 拒绝」按钮回传）

### 2.4 长连接模式（本地开发友好）

若不想暴露公网地址，可使用飞书 SDK 的 WebSocket 长连接接收事件，
无需配置回调地址。切换方式：在 `config.yaml` 的 `channels.feishu` 下加

```yaml
mode: websocket        # callback（默认） | websocket
```

长连接模式下不经过 `/webhooks/feishu/events`，但仍复用同一套
`events` 解析、`gateway` 路由与 `cards` 渲染逻辑。

### 2.5 群聊策略

- `group_require_at: true`：群聊中只有 @ 机器人的消息才触发，避免噪音。
- `allowed_chats` / `allowed_users` 非空时构成白名单，其余一律忽略。
- 同一群聊默认共享一个 thread；如需每人独立上下文，把 `session` 的隔离粒度配置为 `per_user`。

### 2.6 去重与限流

| 项 | 做法 |
| --- | --- |
| 事件重复投递 | `event_id` 去重，TTL `dedup_ttl_seconds` |
| 卡片更新过于频繁 | 按 `card_update_interval_ms` 节流合并 |
| 用户刷屏 | 每会话一个执行队列，排队而非并发 |
| 回复过长 | 超过 `max_reply_chars` 自动截断并附完整内容落盘 |

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
