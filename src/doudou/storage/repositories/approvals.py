"""Approval request and decision persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from doudou.observability.logging import get_logger
from doudou.storage.models import ApprovalModel

logger = get_logger(__name__)


class ApprovalRepository:
    """``approvals`` 表的读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self,
        *,
        approval_id: str,
        tool_name: str,
        status: str,
        call_id: str | None = None,
        thread_id: str | None = None,
        args: dict[str, Any] | None = None,
        step: int = 0,
        reason: str | None = None,
        created_at: datetime | None = None,
        decided_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> ApprovalModel:
        """写入或更新一条确认记录。

        用 upsert 而不是 insert：同一条请求会经历「登记 → 决策」两次写入，
        第二次只应更新状态，而不是造出两条记录。
        """
        row = await self.session.get(ApprovalModel, approval_id)
        if row is None:
            row = ApprovalModel(
                approval_id=approval_id,
                tool_name=tool_name,
                status=status,
                call_id=call_id,
                thread_id=thread_id,
                args=args or {},
                step=step,
                reason=reason,
                created_at=created_at,
                decided_at=decided_at,
                expires_at=expires_at,
            )
            self.session.add(row)
        else:
            row.status = status
            if args is not None:
                row.args = args
            if reason is not None:
                row.reason = reason
            if decided_at is not None:
                row.decided_at = decided_at
        await self.session.flush()
        return row

    async def get_by_call_id(self, call_id: str) -> ApprovalModel | None:
        """按 ``tool_call_id`` 找最近一条。"""
        statement = (
            select(ApprovalModel)
            .where(ApprovalModel.call_id == call_id)
            .order_by(ApprovalModel.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(statement)).scalars().first()

    async def get(self, approval_id: str) -> ApprovalModel | None:
        return await self.session.get(ApprovalModel, approval_id)

    async def list_recent(self, *, limit: int = 50) -> list[ApprovalModel]:
        statement = select(ApprovalModel).order_by(ApprovalModel.created_at.desc()).limit(limit)
        return list((await self.session.execute(statement)).scalars().all())

    async def count_pending(self) -> int:
        statement = select(func.count(ApprovalModel.approval_id)).where(
            ApprovalModel.status == "pending"
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def delete_all(self) -> int:
        from doudou.storage.database import affected_rows

        result = await self.session.execute(delete(ApprovalModel))
        return affected_rows(result)

    async def delete_for_thread(self, thread_id: str) -> int:
        """删除某条会话的全部确认记录（删会话时调用）。"""
        from doudou.storage.database import affected_rows

        result = await self.session.execute(
            delete(ApprovalModel).where(ApprovalModel.thread_id == thread_id)
        )
        return affected_rows(result)


__all__ = ["ApprovalRepository"]
