"""Token and cost usage persistence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from doudou.observability.cost import UsageRecord
from doudou.observability.logging import get_logger
from doudou.storage.database import affected_rows
from doudou.storage.models import UsageModel, as_utc, utcnow

logger = get_logger(__name__)


class UsageRepository:
    """``usage_daily`` 表的读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, record: UsageRecord) -> None:
        """把一条用量累加进 ``(day, thread_id)`` 行。"""
        row = await self.session.get(UsageModel, (record.day, record.thread_id))
        if row is None:
            self.session.add(
                UsageModel(
                    day=record.day,
                    thread_id=record.thread_id,
                    channel=record.channel,
                    model=record.model,
                    runs=record.runs,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    tool_calls=record.tool_calls,
                    approvals=record.approvals,
                    rejections=record.rejections,
                    cost=record.cost,
                    last_at=record.last_at,
                )
            )
        else:
            row.runs += record.runs
            row.input_tokens += record.input_tokens
            row.output_tokens += record.output_tokens
            row.tool_calls += record.tool_calls
            row.approvals += record.approvals
            row.rejections += record.rejections
            row.cost = _add_cost(row.cost, record.cost)
            row.model = record.model or row.model
            row.channel = record.channel or row.channel
            row.last_at = record.last_at
        await self.session.flush()

    async def by_thread(self, *, days: int, limit: int = 50) -> list[UsageRecord]:
        """按会话汇总最近 ``days`` 天。"""
        rows = (
            await self.session.execute(
                select(
                    UsageModel.thread_id,
                    func.sum(UsageModel.runs),
                    func.sum(UsageModel.input_tokens),
                    func.sum(UsageModel.output_tokens),
                    func.sum(UsageModel.tool_calls),
                    func.sum(UsageModel.approvals),
                    func.sum(UsageModel.rejections),
                    # 费用用 sum；未知价格的行是 NULL，sum 会忽略它们，
                    # 因此另取一个 count 判断「有没有未知」。
                    func.sum(UsageModel.cost),
                    func.count(UsageModel.cost),
                    func.count(UsageModel.day),
                    func.max(UsageModel.model),
                    func.max(UsageModel.channel),
                    func.max(UsageModel.last_at),
                )
                .where(UsageModel.day >= _day_cutoff(days))
                .group_by(UsageModel.thread_id)
                .order_by(func.sum(UsageModel.input_tokens + UsageModel.output_tokens).desc())
                .limit(limit)
            )
        ).all()

        return [
            UsageRecord(
                day="*",
                thread_id=row[0],
                channel=row[11] or "",
                model=row[10] or "",
                runs=int(row[1] or 0),
                input_tokens=int(row[2] or 0),
                output_tokens=int(row[3] or 0),
                tool_calls=int(row[4] or 0),
                approvals=int(row[5] or 0),
                rejections=int(row[6] or 0),
                # 有行没有 cost（价格未知）→ 汇总值记为未知，而不是少算。
                cost=None if int(row[8] or 0) < int(row[9] or 0) else row[7],
                last_at=as_utc(row[12]) if isinstance(row[12], datetime) else utcnow(),
            )
            for row in rows
        ]

    async def by_day(
        self, *, days: int, limit: int = 60, thread_id: str | None = None
    ) -> list[UsageRecord]:
        statement = (
            select(
                UsageModel.day,
                func.sum(UsageModel.runs),
                func.sum(UsageModel.input_tokens),
                func.sum(UsageModel.output_tokens),
                func.sum(UsageModel.tool_calls),
                func.sum(UsageModel.cost),
                func.count(UsageModel.cost),
                func.count(UsageModel.day),
                func.max(UsageModel.last_at),
            )
            .where(UsageModel.day >= _day_cutoff(days))
            .group_by(UsageModel.day)
            .order_by(UsageModel.day.desc())
            .limit(limit)
        )
        if thread_id is not None:
            statement = statement.where(UsageModel.thread_id == thread_id)
        rows = (await self.session.execute(statement)).all()

        return [
            UsageRecord(
                day=row[0],
                thread_id="*",
                runs=int(row[1] or 0),
                input_tokens=int(row[2] or 0),
                output_tokens=int(row[3] or 0),
                tool_calls=int(row[4] or 0),
                cost=None if int(row[6] or 0) < int(row[7] or 0) else row[5],
                last_at=as_utc(row[8]) if isinstance(row[8], datetime) else utcnow(),
            )
            for row in rows
        ]

    async def total(self, *, day: str | None = None) -> tuple[int, int, float | None]:
        """``(input, output, cost)``；``cost`` 为 ``None`` 表示存在未知价格的运行。"""
        statement = select(
            func.sum(UsageModel.input_tokens),
            func.sum(UsageModel.output_tokens),
            func.sum(UsageModel.cost),
            func.count(UsageModel.cost),
            func.count(UsageModel.day),
        )
        if day is not None:
            statement = statement.where(UsageModel.day == day)
        row = (await self.session.execute(statement)).one()
        known = int(row[3] or 0)
        rows = int(row[4] or 0)
        return int(row[0] or 0), int(row[1] or 0), (None if known < rows else row[2])

    async def delete_for_thread(self, thread_id: str) -> int:
        """删除某条会话的全部用量行（删会话时调用）。"""
        result = await self.session.execute(
            delete(UsageModel).where(UsageModel.thread_id == thread_id)
        )
        return affected_rows(result)


def _add_cost(left: float | None, right: float | None) -> float | None:
    """合并费用：任何一边未知，结果就是未知（``None`` 不等于 0）。"""
    if left is None or right is None:
        return None
    return round(left + right, 6)


def _day_cutoff(days: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC).date() - timedelta(days=max(1, days))).isoformat()


__all__ = ["UsageRepository"]
