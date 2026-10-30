"""SQL-backed scheduler store."""

from __future__ import annotations

from datetime import datetime

from doudou.observability.logging import get_logger
from doudou.scheduler.store import SchedulerStore
from doudou.scheduler.types import ScheduledTask
from doudou.storage.database import Database
from doudou.storage.models import ScheduledTaskModel, as_utc
from doudou.storage.repositories import ChannelTargetRepository, ScheduledTaskRepository

logger = get_logger(__name__)


class SqlSchedulerStore(SchedulerStore):
    """把定时任务与投递目标落到数据库。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    # ---------------------------------------------------------------- 任务

    async def create(self, task: ScheduledTask) -> None:
        async with self.database.session() as session:
            await ScheduledTaskRepository(session).create(
                ScheduledTaskModel(
                    task_id=task.task_id,
                    name=task.name,
                    prompt=task.prompt,
                    kind=task.kind,
                    expr=task.expr,
                    timezone=task.timezone,
                    channel=task.channel,
                    conversation_id=task.conversation_id,
                    created_by=task.created_by,
                    enabled=task.enabled,
                    next_run_at=task.next_run_at,
                    last_run_at=task.last_run_at,
                    last_status=task.last_status,
                    last_error=task.last_error,
                    run_count=task.run_count,
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                )
            )

    async def get(self, task_id: str) -> ScheduledTask | None:
        async with self.database.session() as session:
            row = await ScheduledTaskRepository(session).get(task_id)
        return _to_task(row) if row is not None else None

    async def list_all(self) -> list[ScheduledTask]:
        async with self.database.session() as session:
            rows = await ScheduledTaskRepository(session).list_all()
        return [_to_task(row) for row in rows]

    async def list_due(self, *, now: datetime, limit: int = 50) -> list[ScheduledTask]:
        async with self.database.session() as session:
            rows = await ScheduledTaskRepository(session).list_due(now=now, limit=limit)
        return [_to_task(row) for row in rows]

    async def reschedule(
        self, task_id: str, *, next_run_at: datetime | None, enabled: bool = True
    ) -> None:
        async with self.database.session() as session:
            await ScheduledTaskRepository(session).reschedule(
                task_id, next_run_at=next_run_at, enabled=enabled
            )

    async def record_result(
        self, task_id: str, *, last_run_at: datetime, status: str, error: str | None
    ) -> None:
        async with self.database.session() as session:
            await ScheduledTaskRepository(session).record_result(
                task_id, last_run_at=last_run_at, status=status, error=error
            )

    async def set_enabled(self, task_id: str, enabled: bool) -> bool:
        async with self.database.session() as session:
            return await ScheduledTaskRepository(session).set_enabled(task_id, enabled)

    async def delete(self, task_id: str) -> bool:
        async with self.database.session() as session:
            return await ScheduledTaskRepository(session).delete(task_id)

    async def count(self) -> int:
        async with self.database.session() as session:
            return await ScheduledTaskRepository(session).count()

    # ---------------------------------------------------------------- 目标

    async def remember_target(self, *, channel: str, user_id: str, conversation_id: str) -> None:
        async with self.database.session() as session:
            await ChannelTargetRepository(session).upsert(
                channel=channel, user_id=user_id, conversation_id=conversation_id
            )

    async def resolve_target(self, *, channel: str, owner_user_id: str | None) -> str | None:
        async with self.database.session() as session:
            return await ChannelTargetRepository(session).resolve(
                channel=channel, owner_user_id=owner_user_id
            )


def _to_task(row: ScheduledTaskModel) -> ScheduledTask:
    return ScheduledTask(
        task_id=row.task_id,
        name=row.name,
        prompt=row.prompt,
        kind=row.kind,  # type: ignore[arg-type]
        expr=row.expr,
        timezone=row.timezone,
        channel=row.channel,
        conversation_id=row.conversation_id,
        created_by=row.created_by,
        enabled=row.enabled,
        next_run_at=as_utc(row.next_run_at),
        last_run_at=as_utc(row.last_run_at) if row.last_run_at else None,
        last_status=row.last_status,
        last_error=row.last_error,
        run_count=row.run_count,
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


__all__ = ["SqlSchedulerStore"]
