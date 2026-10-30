"""Channel identity and binding persistence."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from doudou.observability.logging import get_logger
from doudou.storage.database import affected_rows
from doudou.storage.models import SessionBindingModel, utcnow

logger = get_logger(__name__)


class SessionBindingRepository:
    """``session_bindings`` 表：渠道会话 → thread 的持久映射。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, channel: str, conversation_id: str) -> str | None:
        row = await self.session.get(SessionBindingModel, (channel, conversation_id))
        return row.thread_id if row else None

    async def bind(self, channel: str, conversation_id: str, thread_id: str) -> None:
        """把会话指向某个 thread；已存在则改指向。"""
        row = await self.session.get(SessionBindingModel, (channel, conversation_id))
        if row is None:
            self.session.add(
                SessionBindingModel(
                    channel=channel, conversation_id=conversation_id, thread_id=thread_id
                )
            )
        else:
            row.thread_id = thread_id
            row.updated_at = utcnow()
        await self.session.flush()
        logger.debug(
            "storage.binding_set",
            channel=channel,
            conversation_id=conversation_id,
            thread_id=thread_id,
        )

    async def resolve(self, channel: str, conversation_id: str) -> str | None:
        """取绑定的 thread。

        **不校验 thread 是否还存在** —— 悬空绑定的补救在
        ``SqlHistoryStore.resolve_thread`` 里做（发现 thread 行已删就重新分配）。
        """
        return await self.get(channel, conversation_id)

    async def unbind(self, channel: str, conversation_id: str) -> bool:
        result = await self.session.execute(
            delete(SessionBindingModel).where(
                SessionBindingModel.channel == channel,
                SessionBindingModel.conversation_id == conversation_id,
            )
        )
        return affected_rows(result) > 0

    async def list_for_thread(self, thread_id: str) -> list[SessionBindingModel]:
        statement = select(SessionBindingModel).where(SessionBindingModel.thread_id == thread_id)
        return list((await self.session.execute(statement)).scalars().all())

    async def delete_for_thread(self, thread_id: str) -> int:
        """删除指向某条会话的全部绑定（删会话时调用，避免留下悬空绑定）。"""
        result = await self.session.execute(
            delete(SessionBindingModel).where(SessionBindingModel.thread_id == thread_id)
        )
        return affected_rows(result)


__all__ = ["SessionBindingRepository"]
