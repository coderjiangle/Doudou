"""Inbound event parsing and deduplication by event_id.

两件事：

1. **归一化**：把 lark 的事件对象转成 :class:`~doudou.channels.base.InboundMessage`，
   并顺手做粗过滤（自己发的消息、非文本消息、不在白名单的会话）。
2. **幂等**：飞书在长连接重连或抖动时可能重复投递同一事件，
   按 ``event_id`` + TTL 去重，避免同一条消息被回答两次。

解析函数只依赖 lark 事件对象的**字段**（鸭子类型），因此在测试里可以直接用
``P2ImMessageReceiveV1({...})`` 构造，不需要真的连上飞书。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from doudou.channels.base import InboundMessage
from doudou.channels.feishu.schemas import parse_content
from doudou.config.schema import FeishuChannelConfig
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

CHANNEL_NAME = "feishu"

#: 支持的消息类型。图片/文件要等 M8 之后与多模态一起做。
SUPPORTED_MESSAGE_TYPES = ("text", "post")

#: 私聊对话类型。
P2P_CHAT_TYPES = ("p2p",)


@dataclass(frozen=True, slots=True)
class SkipReason:
    """为什么没有处理这条消息（用于日志与统计）。"""

    code: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    """解析结果：要么得到一条消息，要么得到了「跳过」的原因。"""

    message: InboundMessage | None = None
    skip: SkipReason | None = None

    @property
    def ok(self) -> bool:
        return self.message is not None


# --------------------------------------------------------------------------- 事件字段访问


def _get(obj: Any, name: str) -> Any:
    """按名字取字段，兼容 lark 的模型对象与普通字典。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def event_id_of(event: Any) -> str | None:
    """取事件的 ``event_id``（``header.event_id``）。"""
    raw = _get(_get(event, "header"), "event_id")
    return str(raw) if raw else None


# --------------------------------------------------------------------------- 解析


def parse_message_event(
    event: Any,
    config: FeishuChannelConfig,
    *,
    event_id: str | None = None,
) -> ParsedEvent:
    """把 ``im.message.receive_v1`` 事件转成入站消息。

    Args:
        event: lark 的 ``P2ImMessageReceiveV1``（或同构的 dict）。
        config: 飞书通道配置（白名单、@ 策略、机器人名）。
        event_id: 显式覆盖事件 id（测试用）。

    Returns:
        :class:`ParsedEvent`；被过滤掉时 ``skip`` 里带着原因。
    """
    payload = _get(event, "event") or event
    message = _get(payload, "message")
    sender = _get(payload, "sender")
    if message is None:
        return ParsedEvent(skip=SkipReason("no_message", "事件里没有 message 字段"))

    sender_type = str(_get(sender, "sender_type") or "")
    if sender_type == "app":
        # 机器人自己发的消息（包括我们发出的卡片）会回流，必须丢掉，
        # 否则会自问自答形成死循环。
        return ParsedEvent(skip=SkipReason("from_bot", f"sender_type={sender_type}"))

    message_type = str(_get(message, "message_type") or "")
    chat_id = str(_get(message, "chat_id") or "")
    if not chat_id:
        return ParsedEvent(skip=SkipReason("no_chat_id", "事件里没有 chat_id"))

    chat_type = str(_get(message, "chat_type") or "p2p")
    open_id = _get(_get(sender, "sender_id"), "open_id")
    user_id = str(open_id) if open_id else None

    if not message_allowed(chat_id, user_id, config):
        return ParsedEvent(
            skip=SkipReason(
                "not_allowed",
                f"chat_id={chat_id} user_id={user_id} 不在白名单内",
            )
        )

    if message_type not in SUPPORTED_MESSAGE_TYPES:
        return ParsedEvent(
            skip=SkipReason("unsupported_type", f"暂不支持 message_type={message_type}")
        )

    content = parse_content(message_type, _get(message, "content"))
    text = content.text.strip()

    mentions = list(_get(message, "mentions") or [])
    if chat_type not in P2P_CHAT_TYPES and config.group_require_at:
        if not _mentions_bot(mentions, config):
            return ParsedEvent(
                skip=SkipReason(
                    "not_mentioned",
                    f"群聊消息未 @ 机器人（chat_type={chat_type}）",
                )
            )
        # 把 @机器人的占位符从正文里去掉，模型不需要看到 "@_user_1"。
        text = strip_mention_keys(text, mentions).strip()

    if not text:
        return ParsedEvent(skip=SkipReason("empty_text", "消息正文为空"))

    thread_key = None
    if config.isolate_by_thread:
        # 默认按「群」共享上下文（用户的选择）；开启后按话题（thread_id）隔离。
        thread_key = str(_get(message, "thread_id") or "") or None

    return ParsedEvent(
        message=InboundMessage(
            channel=CHANNEL_NAME,
            conversation_id=chat_id,
            text=text,
            user_id=user_id,
            message_id=str(_get(message, "message_id") or "") or None,
            thread_key=thread_key,
            raw={
                "event_id": event_id or event_id_of(event),
                "chat_type": chat_type,
                "message_type": message_type,
                "mentions": [str(_get(item, "name") or "") for item in mentions],
                "links": content.links,
            },
        )
    )


def _mentions_bot(mentions: list[Any], config: FeishuChannelConfig) -> bool:
    """判断是否 @ 了机器人。

    优先看 ``mentioned_type == "bot"``（飞书会填这个字段）；取不到时退回按
    ``bot_name`` 或配置的 ``bot_open_id`` 比对。两条都落空会记警告，
    提示用户去配 ``bot_open_id`` —— 静默失效比报错更难排查。
    """
    for mention in mentions:
        if str(_get(mention, "mentioned_type") or "") == "bot":
            return True
        open_id = _get(_get(mention, "id"), "open_id")
        if config.bot_open_id and str(open_id or "") == config.bot_open_id:
            return True
        name = str(_get(mention, "name") or "").strip()
        if name and name.casefold() == config.bot_name.strip().casefold():
            return True
    logger.debug("feishu.mention_not_detected", mention_count=len(mentions))
    return False


def strip_mention_keys(text: str, mentions: list[Any]) -> str:
    """把正文里的 ``@_user_1`` 之类占位符删掉。"""
    cleaned = text
    for mention in mentions:
        key = str(_get(mention, "key") or "")
        if key:
            cleaned = cleaned.replace(key, "")
    return cleaned


def message_allowed(chat_id: str, user_id: str | None, config: FeishuChannelConfig) -> bool:
    """白名单校验：两个列表都为空表示不限制。"""
    if config.allowed_chats and chat_id not in config.allowed_chats:
        return False
    if config.allowed_users and (user_id or "") not in config.allowed_users:
        return False
    return True


# --------------------------------------------------------------------------- 幂等


class EventDedup:
    """按 ``event_id`` 去重，容量与 TTL 双重约束。

    TTL 覆盖「重连后重推」的时间窗口；容量上限保证长时间运行不会把内存吃光
    （``OrderedDict`` 按插入顺序淘汰最旧的）。
    """

    def __init__(self, *, ttl_seconds: float = 600, max_entries: int = 4096) -> None:
        self.ttl = max(1.0, ttl_seconds)
        self.max_entries = max(1, max_entries)
        self._seen: OrderedDict[str, float] = OrderedDict()

    def seen(self, event_id: str | None) -> bool:
        """是否已处理过；``None`` 一律视为新事件。"""
        if not event_id:
            return False
        self._evict()
        return event_id in self._seen

    def remember(self, event_id: str | None) -> None:
        """记录已处理。重复调用会刷新时间戳。"""
        if not event_id:
            return
        self._evict()
        self._seen[event_id] = time.monotonic()
        self._seen.move_to_end(event_id)
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)

    def check_and_mark(self, event_id: str | None) -> bool:
        """原子地「检查 + 记录」。

        Returns:
            ``True`` 表示这是新事件（可以处理），``False`` 表示重复。
        """
        if self.seen(event_id):
            return False
        self.remember(event_id)
        return True

    def __len__(self) -> int:
        return len(self._seen)

    def clear(self) -> None:
        self._seen.clear()

    def _evict(self) -> None:
        if not self._seen:
            return
        deadline = time.monotonic() - self.ttl
        # 插入顺序即时间顺序，所以从头开始删就能停。
        while self._seen:
            oldest = next(iter(self._seen.items()))
            if oldest[1] >= deadline:
                break
            self._seen.popitem(last=False)


__all__ = [
    "CHANNEL_NAME",
    "P2P_CHAT_TYPES",
    "SUPPORTED_MESSAGE_TYPES",
    "EventDedup",
    "ParsedEvent",
    "SkipReason",
    "event_id_of",
    "message_allowed",
    "parse_message_event",
    "strip_mention_keys",
]
