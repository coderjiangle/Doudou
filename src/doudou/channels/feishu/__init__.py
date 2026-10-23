"""Feishu (Lark) channel package.

当前实现 **websocket 长连接** 模式（无需公网地址）。``callback`` 模式需要的
验签解密（``crypto.py``）与公网回调路由放在后续里程碑，见 docs/channels.md。
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
