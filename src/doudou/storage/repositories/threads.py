"""Thread and session persistence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from doudou.observability.logging import get_logger
from doudou.storage.database import affected_rows
from doudou.storage.models import MessageModel, ThreadModel, utcnow

logger = get_logger(__name__)


class ThreadRepository:
    """``threads`` 表的读写，含消息计数聚合。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure(
        self,
        thread_id: str,
        *,
        channel: str | None = None,
        conversation_id: str | None = None,
    ) -> ThreadModel:
        """确保线程存在；已存在时只补齐归属字段。"""
        thread = await self.session.get(ThreadModel, thread_id)
        if thread is None:
            thread = ThreadModel(
                thread_id=thread_id, channel=channel, conversation_id=conversation_id
            )
            self.session.add(thread)
            await self.session.flush()
            logger.debug(
                "storage.thread_created",
                thread_id=thread_id,
                channel=channel,
                conversation_id=conversation_id,
            )
            return thread

        changed = False
        if channel and thread.channel != channel:
            thread.channel = channel
            changed = True
        if conversation_id and thread.conversation_id != conversation_id:
            thread.conversation_id = conversation_id
            changed = True
        if changed:
            thread.updated_at = utcnow()
            await self.session.flush()
        return thread

    async def get(self, thread_id: str) -> ThreadModel | None:
        return await self.session.get(ThreadModel, thread_id)

    async def touch(self, thread_id: str) -> None:
        """刷新 ``updated_at``（追加消息后调用）。"""
        thread = await self.session.get(ThreadModel, thread_id)
        if thread is not None:
            thread.updated_at = utcnow()

    async def list_recent(self, *, limit: int = 50) -> list[tuple[ThreadModel, int]]:
        """按最近更新倒序列出线程，并带上消息条数。"""
        statement = (
            select(ThreadModel, func.count(MessageModel.id))
            .outerjoin(MessageModel, MessageModel.thread_id == ThreadModel.thread_id)
            .group_by(ThreadModel.thread_id)
            .order_by(ThreadModel.updated_at.desc())
            .limit(limit)
        )
        rows = await self.session.execute(statement)
        return [(thread, count) for thread, count in rows.all()]

    async def message_count(self, thread_id: str) -> int:
        statement = select(func.count(MessageModel.id)).where(MessageModel.thread_id == thread_id)
        return int((await self.session.execute(statement)).scalar_one())

    async def delete(self, thread_id: str) -> bool:
        result = await self.session.execute(
            delete(ThreadModel).where(ThreadModel.thread_id == thread_id)
        )
        deleted = affected_rows(result) > 0
        if deleted:
            logger.info("storage.thread_deleted", thread_id=thread_id)
        return deleted

    async def created_at(self, thread_id: str) -> datetime | None:
        thread = await self.session.get(ThreadModel, thread_id)
        return thread.created_at if thread else None


__all__ = ["ThreadRepository"]
