"""SQL-backed usage store."""

from __future__ import annotations

from doudou.observability.cost import UsageRecord, UsageStore
from doudou.observability.logging import get_logger
from doudou.storage.database import Database
from doudou.storage.repositories import UsageRepository

logger = get_logger(__name__)


class SqlUsageStore(UsageStore):
    """把 token / 费用累计进 ``usage_daily``。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def add_many(self, records: list[UsageRecord]) -> None:
        """一批累加只开一个会话、只提交一次（原因同 ``SqlAuditStore``）。"""
        if not records:
            return
        async with self.database.session() as session:
            repository = UsageRepository(session)
            for record in records:
                await repository.add(record)

    async def add(self, record: UsageRecord) -> None:
        await self.add_many([record])

    async def by_thread(self, *, days: int) -> list[UsageRecord]:
        async with self.database.session() as session:
            return await UsageRepository(session).by_thread(days=days)

    async def by_day(self, *, days: int) -> list[UsageRecord]:
        async with self.database.session() as session:
            return await UsageRepository(session).by_day(days=days)

    async def total(
        self, *, day: str | None = None, include_pending: bool = False
    ) -> tuple[int, int, float | None]:
        async with self.database.session() as session:
            return await UsageRepository(session).total(day=day)


__all__ = ["SqlUsageStore"]
