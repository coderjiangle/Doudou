"""Scheduled-task and channel-target persistence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from doudou.observability.logging import get_logger
from doudou.storage.database import affected_rows
from doudou.storage.models import ChannelTargetModel, ScheduledTaskModel, utcnow

logger = get_logger(__name__)


class ScheduledTaskRepository:
    """``scheduled_tasks`` 表的读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, task: ScheduledTaskModel) -> None:
        self.session.add(task)
        await self.session.flush()

    async def get(self, task_id: str) -> ScheduledTaskModel | None:
        return await self.session.get(ScheduledTaskModel, task_id)

    async def list_all(self) -> list[ScheduledTaskModel]:
        statement = select(ScheduledTaskModel).order_by(ScheduledTaskModel.next_run_at)
        return list((await self.session.execute(statement)).scalars().all())

    async def list_due(self, *, now: datetime, limit: int = 50) -> list[ScheduledTaskModel]:
        statement = (
            select(ScheduledTaskModel)
            .where(
                ScheduledTaskModel.enabled.is_(True),
                ScheduledTaskModel.next_run_at <= now,
            )
            .order_by(ScheduledTaskModel.next_run_at)
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def reschedule(
        self, task_id: str, *, next_run_at: datetime | None, enabled: bool = True
    ) -> None:
        values: dict[str, object] = {"enabled": enabled, "updated_at": utcnow()}
        if next_run_at is not None:
            values["next_run_at"] = next_run_at
        await self.session.execute(
            update(ScheduledTaskModel).where(ScheduledTaskModel.task_id == task_id).values(**values)
        )

    async def record_result(
        self, task_id: str, *, last_run_at: datetime, status: str, error: str | None
    ) -> None:
        await self.session.execute(
            update(ScheduledTaskModel)
            .where(ScheduledTaskModel.task_id == task_id)
            .values(
                last_run_at=last_run_at,
                last_status=status,
                last_error=error,
                run_count=ScheduledTaskModel.run_count + 1,
                updated_at=utcnow(),
            )
        )

    async def set_enabled(self, task_id: str, enabled: bool) -> bool:
        result = await self.session.execute(
            update(ScheduledTaskModel)
            .where(ScheduledTaskModel.task_id == task_id)
            .values(enabled=enabled, updated_at=utcnow())
        )
        return affected_rows(result) > 0

    async def delete(self, task_id: str) -> bool:
        result = await self.session.execute(
            delete(ScheduledTaskModel).where(ScheduledTaskModel.task_id == task_id)
        )
        return affected_rows(result) > 0

    async def count(self) -> int:
        statement = select(func.count(ScheduledTaskModel.task_id))
        return int((await self.session.execute(statement)).scalar_one())


class ChannelTargetRepository:
    """``channel_targets`` 表：用户 → 该用户在渠道里的会话。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(self, *, channel: str, user_id: str, conversation_id: str) -> None:
        row = await self.session.get(ChannelTargetModel, (channel, user_id))
        if row is None:
            self.session.add(
                ChannelTargetModel(
                    channel=channel, user_id=user_id, conversation_id=conversation_id
                )
            )
        else:
            row.conversation_id = conversation_id
            row.updated_at = utcnow()
        await self.session.flush()

    async def resolve(self, *, channel: str, owner_user_id: str | None) -> str | None:
        """解析投递会话。

        配了 owner 就用它；否则只有**唯一一条**绑定才能确定，多条/零条返回 ``None``
        （调用方据此跳过本次触发，而不是猜一个发出去）。
        """
        if owner_user_id:
            row = await self.session.get(ChannelTargetModel, (channel, owner_user_id))
            return row.conversation_id if row else None
        statement = select(ChannelTargetModel.conversation_id).where(
            ChannelTargetModel.channel == channel
        )
        values = list((await self.session.execute(statement)).scalars().all())
        unique = set(values)
        if len(unique) == 1:
            return values[0]
        return None


__all__ = ["ChannelTargetRepository", "ScheduledTaskRepository"]
