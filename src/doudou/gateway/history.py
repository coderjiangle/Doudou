"""会话历史的存储抽象。

``AgentRunner`` 只依赖 :class:`HistoryStore` 协议，因此「历史放内存」还是「历史落库」
对上层完全透明：

* :class:`InMemoryHistoryStore` —— 进程内（CLI 默认，零依赖）；
* ``doudou.storage.history.SqlHistoryStore`` —— 落库（HTTP API 默认，重启可续）。

之所以把协议放在 ``gateway`` 而不是 ``storage``：``agent`` ↔ ``gateway`` ↔ ``storage``
的依赖方向要保持单向，实现方（storage）只需要结构上满足协议，不必反向 import。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from langchain_core.messages import BaseMessage

from doudou.gateway.session import SessionStore
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

#: 单个线程默认保留的消息条数上限。
DEFAULT_MAX_MESSAGES = 60


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    """线程的元信息（不含消息本体）。"""

    thread_id: str
    channel: str | None = None
    conversation_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    message_count: int = 0


@runtime_checkable
class HistoryStore(Protocol):
    """会话历史的读写协议。"""

    async def resolve_thread(self, conversation_id: str, *, channel: str, new: bool = False) -> str:
        """取（或创建）该渠道会话对应的 thread_id；``new=True`` 时强制新建。"""
        ...

    async def ensure_thread(
        self,
        thread_id: str,
        *,
        channel: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        """确保线程存在，并补齐归属信息。"""
        ...

    async def load(self, thread_id: str) -> list[BaseMessage]:
        """按时间顺序取出线程内的消息。"""
        ...

    async def append(
        self,
        thread_id: str,
        messages: Sequence[BaseMessage],
        *,
        channel: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        """追加消息，并按存储自身的策略裁剪过旧内容。

        ``channel`` / ``conversation_id`` 只在首次写入时用于补齐线程归属，
        这样调用方不必先显式建线程。
        """
        ...

    async def clear(self, thread_id: str) -> bool:
        """清空线程历史；返回是否确有删除。"""
        ...

    async def get_thread(self, thread_id: str) -> ThreadInfo | None: ...

    async def list_threads(self, *, limit: int = 50) -> list[ThreadInfo]: ...


@dataclass(slots=True)
class _MemoryThread:
    messages: list[BaseMessage] = field(default_factory=list)
    channel: str | None = None
    conversation_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class InMemoryHistoryStore:
    """进程内历史存储。CLI 使用；进程退出即丢失。"""

    def __init__(
        self,
        *,
        sessions: SessionStore | None = None,
        max_messages: int = DEFAULT_MAX_MESSAGES,
    ) -> None:
        self.sessions = sessions or SessionStore()
        self.max_messages = max_messages
        self._threads: dict[str, _MemoryThread] = {}

    # ---------------------------------------------------------------- 线程

    async def resolve_thread(
        self, conversation_id: str, *, channel: str = "cli", new: bool = False
    ) -> str:
        if new:
            thread_id = self.sessions.create(conversation_id, channel=channel)
        else:
            thread_id = self.sessions.get_or_create(conversation_id, channel=channel)
        await self.ensure_thread(thread_id, channel=channel, conversation_id=conversation_id)
        return thread_id

    async def ensure_thread(
        self,
        thread_id: str,
        *,
        channel: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        record = self._threads.get(thread_id)
        if record is None:
            self._threads[thread_id] = _MemoryThread(
                channel=channel, conversation_id=conversation_id
            )
            return
        if channel:
            record.channel = channel
        if conversation_id:
            record.conversation_id = conversation_id

    # ---------------------------------------------------------------- 消息

    async def load(self, thread_id: str) -> list[BaseMessage]:
        record = self._threads.get(thread_id)
        return list(record.messages) if record else []

    async def append(
        self,
        thread_id: str,
        messages: Sequence[BaseMessage],
        *,
        channel: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        if not messages:
            return
        await self.ensure_thread(thread_id, channel=channel, conversation_id=conversation_id)
        record = self._threads[thread_id]
        record.messages.extend(messages)
        record.updated_at = datetime.now(UTC)
        if self.max_messages > 0 and len(record.messages) > self.max_messages:
            record.messages = trim_history(record.messages, self.max_messages)

    async def clear(self, thread_id: str) -> bool:
        record = self._threads.pop(thread_id, None)
        return record is not None

    # ---------------------------------------------------------------- 元信息

    async def get_thread(self, thread_id: str) -> ThreadInfo | None:
        record = self._threads.get(thread_id)
        if record is None:
            return None
        return ThreadInfo(
            thread_id=thread_id,
            channel=record.channel,
            conversation_id=record.conversation_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
            message_count=len(record.messages),
        )

    async def list_threads(self, *, limit: int = 50) -> list[ThreadInfo]:
        infos = [
            ThreadInfo(
                thread_id=thread_id,
                channel=record.channel,
                conversation_id=record.conversation_id,
                created_at=record.created_at,
                updated_at=record.updated_at,
                message_count=len(record.messages),
            )
            for thread_id, record in self._threads.items()
        ]
        infos.sort(
            key=lambda item: item.updated_at or datetime.min.replace(tzinfo=UTC), reverse=True
        )
        return infos[:limit]


def is_tool_message(message: BaseMessage) -> bool:
    """是否是工具结果消息（type == "tool"）。

    用 ``type`` 而不是 ``isinstance``：LangChain 的工具消息有 ``ToolMessage`` 与
    ``FunctionMessage`` 两种历史形态，按类型名判断更省心。
    """
    return message.type == "tool"


def trim_history(messages: list[BaseMessage], limit: int) -> list[BaseMessage]:
    """从头部裁剪历史，且不把切入点落在工具调用组中间。

    按条数裁剪；按 token 预算的裁剪在 ``agent.context`` 里做。
    """
    if limit <= 0 or len(messages) <= limit:
        return messages

    start = len(messages) - limit
    while start < len(messages) and is_tool_message(messages[start]):
        start += 1
    return messages[start:]


__all__ = [
    "DEFAULT_MAX_MESSAGES",
    "HistoryStore",
    "InMemoryHistoryStore",
    "ThreadInfo",
    "is_tool_message",
    "trim_history",
]
