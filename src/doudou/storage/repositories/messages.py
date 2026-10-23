"""Message history persistence."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from doudou.observability.logging import get_logger
from doudou.storage.database import affected_rows
from doudou.storage.models import MessageModel

logger = get_logger(__name__)

#: 角色取值。
ROLE_HUMAN = "human"
ROLE_AI = "ai"
ROLE_TOOL = "tool"
ROLE_SYSTEM = "system"


class MessageRepository:
    """``messages`` 表的读写。

    写入按 ``seq`` 自增，读取按 ``seq`` 升序 —— 顺序即模型看到上下文的顺序。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(
        self,
        thread_id: str,
        *,
        role: str,
        content: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        status: str | None = None,
    ) -> MessageModel:
        """追加一条消息，``seq`` 由当前最大值 +1 得到。"""
        next_seq = await self._next_seq(thread_id)
        row = MessageModel(
            thread_id=thread_id,
            seq=next_seq,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            name=name,
            status=status,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list(self, thread_id: str) -> list[MessageModel]:
        statement = (
            select(MessageModel)
            .where(MessageModel.thread_id == thread_id)
            .order_by(MessageModel.seq.asc())
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def count(self, thread_id: str) -> int:
        statement = select(func.count(MessageModel.id)).where(MessageModel.thread_id == thread_id)
        return int((await self.session.execute(statement)).scalar_one())

    async def delete_all(self, thread_id: str) -> int:
        result = await self.session.execute(
            delete(MessageModel).where(MessageModel.thread_id == thread_id)
        )
        return affected_rows(result)

    async def trim(self, thread_id: str, keep: int) -> int:
        """只保留最新的 ``keep`` 条，返回删除条数。

        裁剪点不能落在 ``tool`` 角色上 —— 那样会留下一个「找不到对应 tool_call」的
        孤立结果，下一次请求会被 provider 拒绝。因此切点落在 tool 上时向后顺延。
        """
        if keep <= 0:
            return 0

        rows = await self.session.execute(
            select(MessageModel.seq, MessageModel.role)
            .where(MessageModel.thread_id == thread_id)
            .order_by(MessageModel.seq.desc())
            .limit(keep + 64)
        )
        window = list(reversed(rows.all()))
        if len(window) <= keep:
            return 0

        start = len(window) - keep
        while start < len(window) and window[start].role == ROLE_TOOL:
            start += 1
        if start >= len(window):  # 理论上不会发生，保守起见什么都不删
            return 0

        cutoff = window[start].seq
        result = await self.session.execute(
            delete(MessageModel).where(
                MessageModel.thread_id == thread_id, MessageModel.seq < cutoff
            )
        )
        removed = affected_rows(result)
        if removed:
            logger.debug("storage.messages_trimmed", thread_id=thread_id, removed=removed)
        return removed

    async def _next_seq(self, thread_id: str) -> int:
        statement = select(func.max(MessageModel.seq)).where(MessageModel.thread_id == thread_id)
        current = (await self.session.execute(statement)).scalar()
        return int(current or 0) + 1


__all__ = [
    "ROLE_AI",
    "ROLE_HUMAN",
    "ROLE_SYSTEM",
    "ROLE_TOOL",
    "MessageRepository",
]
