"""Feishu (Lark) channel package.

当前实现 **WebSocket 长连接** 接入（无需公网地址与回调验签解密），见 docs/channels.md。
"""

from __future__ import annotations

from doudou.channels.feishu.channel import FeishuChannel, FeishuWebSocketRunner
from doudou.channels.feishu.client import FeishuClient, FeishuError
from doudou.channels.feishu.events import EventDedup, ParsedEvent, parse_message_event

__all__ = [
    "EventDedup",
    "FeishuChannel",
    "FeishuClient",
    "FeishuError",
    "FeishuWebSocketRunner",
    "ParsedEvent",
    "parse_message_event",
]
