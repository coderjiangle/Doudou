"""Channel protocol: inbound normalization and outbound sending.

跨层共享的数据结构放在这里：``gateway`` 依赖本模块的协议，但**不依赖任何具体通道实现**
（见 AGENTS.md 的分层约定）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """通道归一化后的入站消息。"""

    channel: str
    #: 会话标识：飞书为 ``chat_id``，私聊可用 ``open_id``，CLI 为固定值。
    conversation_id: str
    text: str
    user_id: str | None = None
    user_name: str | None = None
    #: 通道侧消息 ID，用于回复引用与去重。
    message_id: str | None = None
    #: 进一步区分话题的键（群内话题 / 回复链）。
    thread_key: str | None = None
    #: 原始事件，排查问题时使用。
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        """用于映射 agent thread 的稳定键。"""
        if self.thread_key:
            return f"{self.conversation_id}#{self.thread_key}"
        return self.conversation_id


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """要发回通道的消息。"""

    conversation_id: str
    text: str
    reply_to: str | None = None
    #: 是否是本次运行的最终消息（流式场景下用于决定是否收尾）。
    is_final: bool = True


@runtime_checkable
class Channel(Protocol):
    """通道协议。

    实现者只需要负责「收消息 / 发消息」，编排逻辑一律交给 ``gateway``。
    """

    #: 通道名，与配置 ``channels.<name>`` 对应。
    name: str

    async def start(self) -> None:
        """启动通道（建立连接、注册回调等）。"""
        ...

    async def stop(self) -> None:
        """停止通道并释放资源。"""
        ...

    async def send(self, message: OutboundMessage) -> None:
        """向通道发送消息。"""
        ...


__all__ = ["Channel", "InboundMessage", "OutboundMessage"]
