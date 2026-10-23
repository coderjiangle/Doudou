"""Database-backed history store.

实现 :class:`doudou.gateway.history.HistoryStore` 协议：把 LangChain 消息与 ORM 行互相转换，
并借用三个仓库完成读写。上层（gateway / api）只看到协议，看不到 SQLAlchemy。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from doudou.gateway.history import DEFAULT_MAX_MESSAGES, HistoryStore, ThreadInfo
from doudou.gateway.session import SessionStore
from doudou.observability.logging import get_logger
from doudou.storage.database import Database
from doudou.storage.models import MessageModel, as_utc, utcnow
from doudou.storage.repositories import (
    MessageRepository,
    SessionBindingRepository,
    ThreadRepository,
)

logger = get_logger(__name__)


class SqlHistoryStore(HistoryStore):
    """把会话历史落到数据库。

    ``thread_id`` 的生成仍然复用 :class:`SessionStore`（保持与 CLI 一致的命名），
    但「会话 → thread」的绑定会写进 ``session_bindings``，因此**重启后仍能续上同一个会话**。
    """

    def __init__(
        self,
        database: Database,
        *,
        sessions: SessionStore | None = None,
        max_messages: int = DEFAULT_MAX_MESSAGES,
    ) -> None:
        self.database = database
        self.sessions = sessions or SessionStore()
        self.max_messages = max_messages

    # ---------------------------------------------------------------- 线程

    async def resolve_thread(
        self, conversation_id: str, *, channel: str = "cli", new: bool = False
    ) -> str:
        if not new:
            async with self.database.session() as session:
                bound = await SessionBindingRepository(session).get(channel, conversation_id)
                if bound is not None and await ThreadRepository(session).get(bound) is not None:
                    return bound
                if bound is not None:
                    # thread 行已不存在（例如被删过），绑定失效，重新分配。
                    logger.warning(
                        "storage.binding_stale", channel=channel, conversation_id=conversation_id
                    )
                    await SessionBindingRepository(session).unbind(channel, conversation_id)

        thread_id = self.sessions.create(conversation_id, channel=channel)
        async with self.database.session() as session:
            await SessionBindingRepository(session).bind(channel, conversation_id, thread_id)
            await ThreadRepository(session).ensure(
                thread_id, channel=channel, conversation_id=conversation_id
            )
        return thread_id

    async def ensure_thread(
        self,
        thread_id: str,
        *,
        channel: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        async with self.database.session() as session:
            await ThreadRepository(session).ensure(
                thread_id, channel=channel, conversation_id=conversation_id
            )

    # ---------------------------------------------------------------- 消息

    async def load(self, thread_id: str) -> list[BaseMessage]:
        async with self.database.session() as session:
            rows = await MessageRepository(session).list(thread_id)
        return [message for row in rows if (message := _to_message(row)) is not None]

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
        async with self.database.session() as session:
            threads = ThreadRepository(session)
            repository = MessageRepository(session)
            await threads.ensure(thread_id, channel=channel, conversation_id=conversation_id)
            for message in messages:
                payload = _to_row_payload(message)
                await repository.append(thread_id, **payload)
            await repository.trim(thread_id, self.max_messages)
            await threads.touch(thread_id)

    async def clear(self, thread_id: str) -> bool:
        async with self.database.session() as session:
            removed = await MessageRepository(session).delete_all(thread_id)
            deleted = await ThreadRepository(session).delete(thread_id)
        return bool(removed or deleted)

    # ---------------------------------------------------------------- 元信息

    async def get_thread(self, thread_id: str) -> ThreadInfo | None:
        async with self.database.session() as session:
            thread = await ThreadRepository(session).get(thread_id)
            if thread is None:
                return None
            count = await ThreadRepository(session).message_count(thread_id)
            return ThreadInfo(
                thread_id=thread.thread_id,
                channel=thread.channel,
                conversation_id=thread.conversation_id,
                created_at=as_utc(thread.created_at),
                updated_at=as_utc(thread.updated_at),
                message_count=count,
            )

    async def list_threads(self, *, limit: int = 50) -> list[ThreadInfo]:
        async with self.database.session() as session:
            rows = await ThreadRepository(session).list_recent(limit=limit)
        return [
            ThreadInfo(
                thread_id=thread.thread_id,
                channel=thread.channel,
                conversation_id=thread.conversation_id,
                created_at=as_utc(thread.created_at),
                updated_at=as_utc(thread.updated_at),
                message_count=count,
            )
            for thread, count in rows
        ]


# --------------------------------------------------------------------------- 消息编解码


def _to_row_payload(message: BaseMessage) -> dict[str, Any]:
    """把 LangChain 消息转成 ``MessageRepository.append`` 的入参。

    角色直接取 ``message.type``（``human`` / ``ai`` / ``tool`` / ``system``），
    这样新增消息类型时不必改这里。
    """
    payload: dict[str, Any] = {
        "role": message.type,
        "content": _flatten_content(message.content),
    }
    if isinstance(message, AIMessage) and message.tool_calls:
        payload["tool_calls"] = [dict(call) for call in message.tool_calls]
    if isinstance(message, ToolMessage):
        payload["tool_call_id"] = message.tool_call_id
        payload["name"] = message.name
        payload["status"] = message.status or "success"
    return payload


def _to_message(row: MessageModel) -> BaseMessage | None:
    """把 ORM 行还原成 LangChain 消息；数据不完整时返回 ``None`` 并告警。"""
    content = row.content or ""
    if row.role == "human":
        return HumanMessage(content=content)
    if row.role == "ai":
        calls = [dict(call) for call in (row.tool_calls or [])]
        return AIMessage(content=content, tool_calls=calls)
    if row.role == "system":
        return SystemMessage(content=content)
    if row.role == "tool":
        if not row.tool_call_id:
            # 没有 tool_call_id 的 tool 消息是坏数据：喂给模型会被 provider 拒绝。
            logger.warning(
                "storage.tool_message_without_call_id", thread_id=row.thread_id, seq=row.seq
            )
            return None
        return ToolMessage(
            content=content,
            tool_call_id=row.tool_call_id,
            name=row.name,
            status=row.status or "success",
        )
    logger.warning("storage.unknown_role", thread_id=row.thread_id, seq=row.seq, role=row.role)
    return None


def _flatten_content(content: str | list[Any] | None) -> str:
    """多模态内容目前只落文本部分。

    M4 打通图片链路时，这里需要改成保留完整的 content blocks（参见
    docs/configuration.md 的「多模态」一节）。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    from doudou.utils.text import content_to_text

    return content_to_text(content)


__all__ = ["SqlHistoryStore", "utcnow"]
