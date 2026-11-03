"""Channel protocol: inbound normalization and outbound sending.

跨层共享的数据结构放在这里：``gateway`` 依赖本模块的协议，但**不依赖任何具体通道实现**
（见 AGENTS.md 的分层约定）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from doudou.agent.events import RunEvent
from doudou.agent.messages import ImageInput


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
    #: 已校验的内嵌图片；CLI 不提供图片输入。
    images: tuple[ImageInput, ...] = ()

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


@runtime_checkable
class EventSink(Protocol):
    """能消费「一次运行的事件流」并渲染到某个通道。

    ``gateway.MessageRouter`` 只依赖这个协议，因此它对「渲染成终端文本还是飞书卡片」
    一无所知 —— 渲染方式与执行链路彻底解耦。
    """

    name: str

    async def send(self, message: OutboundMessage) -> None:
        """发送一条独立消息（例如「上一条还在处理中」）。"""
        ...

    async def deliver(self, message: InboundMessage, events: AsyncIterator[RunEvent]) -> None:
        """消费一次运行的事件流。

        实现方负责节流、聚合与错误兜底；**不应**把异常抛回给 router ——
        这里已经在通道内部，抛出去没人能处理。
        """
        ...


__all__ = ["Channel", "EventSink", "InboundMessage", "OutboundMessage"]


#: 当前正在被处理的那条入站消息。
#:
#: 为什么需要它：确认回调只拿到「工具名 + 参数」，但飞书卡片需要知道**发到哪个会话**。
#: 让 tool / agent 层一路透传会话 ID 会把通道概念污染进 Agent；
#: 而「正在服务谁」本来就是通道层的环境状态，用 ContextVar 最省事 ——
#: 它会自动随 await 链传递，不需要任何显式参数。
_current_inbound: ContextVar[InboundMessage | None] = ContextVar(
    "doudou_current_inbound", default=None
)


def bind_inbound(message: InboundMessage) -> Token[InboundMessage | None]:
    """标记「开始处理这条消息」，返回供 :func:`unbind_inbound` 还原的 token。"""
    return _current_inbound.set(message)


def unbind_inbound(token: Token[InboundMessage | None]) -> None:
    """还原上一次的绑定。必须放在 ``finally`` 里，否则并发处理会串味。"""
    _current_inbound.reset(token)


def current_inbound() -> InboundMessage | None:
    """取当前正在处理的消息；不在任何处理链路里时返回 ``None``。"""
    return _current_inbound.get()
