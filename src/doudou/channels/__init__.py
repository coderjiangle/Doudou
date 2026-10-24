"""Messaging channel package.

**这个 ``__init__`` 必须保持轻量**：任何 ``import doudou.channels.x`` 都会先执行它，
而 ``gateway.router`` 只依赖 ``channels.base``。一旦在这里 import ``registry``，
就会拖进 ``channels.feishu.channel`` —— 而后者又反向依赖 ``gateway.router``，
形成循环导入（表现为「导入顺序不同就报 ImportError」，极难排查）。

因此这里只导出协议与数据结构；要构造通道请显式
``from doudou.channels.registry import build_enabled_channels``。
"""

from __future__ import annotations

from doudou.channels.base import (
    Channel,
    EventSink,
    InboundMessage,
    OutboundMessage,
)

__all__ = [
    "Channel",
    "EventSink",
    "InboundMessage",
    "OutboundMessage",
]
