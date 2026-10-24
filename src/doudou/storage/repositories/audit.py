"""Audit event persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from doudou.observability.logging import get_logger
from doudou.storage.database import affected_rows
from doudou.storage.models import AuditModel

logger = get_logger(__name__)


class AuditRepository:
    """``audit_events`` 表的读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self,
        *,
        audit_id: str,
        kind: str,
        thread_id: str,
        status: str,
        channel: str | None = None,
        conversation_id: str | None = None,
        user_id: str | None = None,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        step: int = 0,
        args: dict[str, Any] | None = None,
        args_truncated: int = 0,
        result_preview: str | None = None,
        result_chars: int = 0,
        approved: bool | None = None,
        elapsed: float | None = None,
        error: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> AuditModel:
        """写入或更新一条审计记录。

        工具调用会写两次（先建、后补结果），用 upsert 才能保证「一次调用一行」。
        """
        row = await self.session.get(AuditModel, audit_id)
        if row is None:
            row = AuditModel(
                audit_id=audit_id,
                kind=kind,
                thread_id=thread_id,
                status=status,
                channel=channel,
                conversation_id=conversation_id,
                user_id=user_id,
                tool_name=tool_name or "",
                tool_call_id=tool_call_id,
                step=step,
                args=args or {},
                args_truncated=args_truncated,
                result_preview=result_preview,
                result_chars=result_chars,
                approved=approved,
                elapsed=elapsed,
                error=error,
                created_at=created_at,
                updated_at=updated_at,
            )
            self.session.add(row)
        else:
            row.status = status
            if args is not None:
                row.args = args
            if result_preview is not None:
                row.result_preview = result_preview
            if result_chars:
                row.result_chars = result_chars
            if approved is not None:
                row.approved = approved
            if elapsed is not None:
                row.elapsed = elapsed
            if error is not None:
                row.error = error
            row.args_truncated = max(row.args_truncated, args_truncated)
            if updated_at is not None:
                row.updated_at = updated_at
        await self.session.flush()
        return row

    async def get(self, audit_id: str) -> AuditModel | None:
        return await self.session.get(AuditModel, audit_id)

    async def list_recent(
        self, *, limit: int = 100, thread_id: str | None = None
    ) -> list[AuditModel]:
        statement = select(AuditModel)
        if thread_id:
            statement = statement.where(AuditModel.thread_id == thread_id)
        statement = statement.order_by(AuditModel.created_at.desc(), AuditModel.audit_id).limit(
            limit
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def count(self) -> int:
        statement = select(func.count(AuditModel.audit_id))
        return int((await self.session.execute(statement)).scalar_one())

    async def trim(self, *, keep: int) -> int:
        """只保留最近 ``keep`` 条，返回删除条数。

        审计表是**每次工具调用**都写的东西，不设上限就是一条缓慢的磁盘泄漏。
        用「保留最近 N 条」而不是「按天清理」是因为前者的上限是确定的：
        一天调用 10 万次也不会把磁盘撑爆。
        """
        if keep <= 0:
            return 0
        subquery = (
            select(AuditModel.audit_id)
            .order_by(AuditModel.created_at.desc(), AuditModel.audit_id)
            .limit(keep)
        )
        result = await self.session.execute(
            delete(AuditModel).where(AuditModel.audit_id.not_in(subquery))
        )
        return affected_rows(result)

    async def delete_all(self) -> int:
        result = await self.session.execute(delete(AuditModel))
        return affected_rows(result)


__all__ = ["AuditRepository"]
