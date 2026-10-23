"""Internal envelope types shared by channels and the gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from doudou.channels.base import InboundMessage


@dataclass(frozen=True, slots=True)
class RunRequest:
    """一次 Agent 运行的输入。"""

    text: str
    conversation_id: str
    channel: str = "cli"
    user_id: str | None = None
    #: 显式指定 thread；留空则由 :class:`~doudou.gateway.session.SessionStore` 映射。
    thread_id: str | None = None
    #: 是否开启新会话（``/new``）。
    new_thread: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_inbound(cls, message: InboundMessage, **extra: Any) -> RunRequest:
        return cls(
            text=message.text,
            conversation_id=message.session_key,
            channel=message.channel,
            user_id=message.user_id,
            metadata=dict(extra),
        )


__all__ = ["RunRequest"]
